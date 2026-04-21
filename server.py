"""
Vitodata Streaming Dictation Server
====================================
Real-time speech-to-text with sliding window Whisper and self-correction.

Architecture:
  Browser (mic PCM chunks) → Socket.IO → Audio Buffer → VAD → Sliding Window → Whisper API → Diff Engine → Client

Runs alongside existing Whisper container on p2aisv01.
Whisper API expected at: http://172.17.16.150:8000/v1/audio/transcriptions

Usage:
  pip install fastapi uvicorn python-socketio aiohttp numpy torch torchaudio --break-system-packages
  python server.py

  # Or with gunicorn for production:
  # gunicorn -k uvicorn.workers.UvicornWorker -w 1 -b 0.0.0.0:8003 server:app
"""

import asyncio
import io
import json
import logging
import os
import re
import time
import unicodedata
import uuid
import wave
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import aiohttp
import socketio
import uvicorn
from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv

# Load environment variables from .env for local runs (no-op if file is absent).
load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WHISPER_API_URL = os.environ.get("WHISPER_API_URL", "http://localhost:8000/v1/audio/transcriptions")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-large-v3-turbo")
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "de")

# VAD — set VAD_ENABLED=false to skip loading torch/Silero at startup (useful locally)
VAD_ENABLED = os.environ.get("VAD_ENABLED", "true").lower() not in ("false", "0", "no")

# LLM correction via GPUStack (Mistral Small 3.2 24B on GPUs 6+7)
LLM_API_URL = os.environ.get("LLM_API_URL", "http://localhost:8000/v1/chat/completions")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")  # GPUStack API key
LLM_MODEL = os.environ.get("LLM_MODEL", "Mistral-Small-3.2-24B-Instruct-2506")
LLM_CORRECTION_ENABLED = os.environ.get("LLM_CORRECTION_ENABLED", "true").lower() == "true"

# Comparison model (Model B) — for side-by-side correction diff
LLM_MODEL_B = os.environ.get("LLM_MODEL_B", "")  # e.g. Qwen2.5-72B-Instruct-AWQ
LLM_MODEL_B_NAME = os.environ.get("LLM_MODEL_B_NAME", "")  # display name

# RAGFlow agents — JSON list of {id, name} objects
RAGFLOW_BASE_URL = os.environ.get("RAGFLOW_BASE_URL", "")  # e.g. http://172.17.16.150
RAGFLOW_API_KEY = os.environ.get("RAGFLOW_API_KEY", "")
_ragflow_agents_raw = os.environ.get("RAGFLOW_AGENTS", "[]")
try:
    RAGFLOW_AGENTS: list[dict] = json.loads(_ragflow_agents_raw)
except Exception:
    RAGFLOW_AGENTS = []

# RAGAS project directory — for saving gold standards that Streamlit picks up
RAGAS_PROJECT_DIR = os.environ.get("RAGAS_PROJECT_DIR", "")

# ---------------------------------------------------------------------------
# LLM Prompts — MDR-compliant: fix ASR errors only, no medical inference
# ---------------------------------------------------------------------------

# Per-window correction: reconciles two overlapping Whisper windows using context.
# Called on every transcription step to resolve conflicts in the overlap region.
LLM_WINDOW_CORRECTION_PROMPT = """Du bist ein Echtzeit-Korrektursystem für medizinische Sprachdiktate (Schweiz).

Du erhältst aufeinanderfolgende Transkriptionsfenster von Whisper, die sich zeitlich überlappen.
Die letzten Wörter von VORHERIGES_FENSTER und die ersten Wörter von AKTUELLES_FENSTER
wurden aus demselben Audiomaterial erzeugt — Unterschiede dort sind ASR-Fehler.

DEINE AUFGABE:
Gib eine korrigierte Version von AKTUELLES_FENSTER zurück, indem du:

1. KONTEXT verwendest, um das medizinische Thema zu verstehen
2. Den Überlappungsbereich analysierst
3. Phonetische ASR-Fehler korrigierst — auch wenn die Ähnlichkeit 
    nicht offensichtlich ist. Nutze den medizinischen Kontext 
    (Diagnose, Symptome, Fachgebiet), um zu erkennen welches 
    Medikament/welcher Fachbegriff tatsächlich gesprochen wurde.
    Beispiele:
    - "ein Gobi" / "Weg Ovi" → "Wegovy" (GLP-1-Agonist, Kontext: Adipositas/Diabetes)
    - "Tafalgan" → "Dafalgan" (Paracetamol, phonetisch ähnlich)
    - "Gesinsaal" / "sexy Sal" → "Xyzal" (Antihistamin, phonetisch verzerrt)
    - "Nofalgin" → "Novalgin" (Metamizol)
    - "Panto Pro Soll" → "Pantoprazol" (Protonenpumpenhemmer)
    - "Oh Mepra Soll" → "Omeprazol" (Protonenpumpenhemmer)
    - "atihstamn" → "Antihistamin"
4. Schweizer Orthographie verwendest (ss statt ß, z.B. "heisst", "muss", "strasse")
5. Den Telegrammstil beibehältst — keine Umformulierung in vollständige Sätze
6. Fehlende Satzzeichen ergänzt wo klar erkennbar

STRIKTE GRENZEN (MDR-Compliance):
- NIEMALS Diagnosen ableiten oder hinzufügen
- NIEMALS Medikamentendosierungen verändern
- NIEMALS Markennamen in Generika umwandeln (oder umgekehrt)
- NIEMALS Informationen hinzufügen, die nicht im Original stehen
- Bei echter Unsicherheit: Original beibehalten

Gib NUR den korrigierten Text von AKTUELLES_FENSTER zurück. Keine Erklärungen, keine Kommentare."""

# Final correction: called once on stop or silence timeout with the full accumulated text.
LLM_FINAL_CORRECTION_PROMPT = """Du bist ein Nachkorrektur-System für medizinische Sprachdiktate (Schweiz).

Du erhältst den vollständigen Rohtext eines abgeschlossenen Diktats von Whisper.

AUFGABE: Führe eine abschliessende Bereinigung durch:
1. Korrigiere verbleibende phonetische ASR-Fehler bei Medikamenten- und Fachbegriffen
2. Stelle semantische Kohärenz sicher — ersetze Wortsequenzen die keinen Sinn ergeben
   durch die wahrscheinlich gemeinte medizinische Formulierung, wenn der Kontext eindeutig ist
3. Vereinheitliche Satzzeichen und Grossschreibung
4. Schweizer Orthographie (ss statt ß)
5. Behalte den Telegrammstil bei

STRIKTE GRENZEN (MDR-Compliance):
- NIEMALS Diagnosen ableiten oder hinzufügen
- NIEMALS Dosierungen verändern
- NIEMALS Markennamen in Generika umwandeln (oder umgekehrt)
- NIEMALS Informationen hinzufügen die nicht im Original stehen
- Bei Unsicherheit: Original beibehalten

Gib NUR den korrigierten Text zurück. Keine Erklärungen, keine Kommentare."""

# Overlap-aware per-window correction with correction diffs.
# Replaces LLM_WINDOW_CORRECTION_PROMPT for the normal single-model mode.
CORRECTION_PROMPT_V2 = """Du bist ein medizinischer ASR-Korrektor für Schweizer Arztdiktate.

## AUFGABE
Du erhältst bereits bestätigten Text (COMMITTED TEXT) und das aktuelle Whisper-Fenster.
Gib NUR den Text zurück, der im aktuellen Fenster NEU ist — alles, was noch nicht im COMMITTED TEXT steht.

Regeln:
- Beginne direkt nach dem letzten Wort des committed text
- Wiederhole NICHTS aus dem committed text — auch nicht paraphrasiert
- Korrigiere ASR-Fehler bei Medikamenten und Fachbegriffen
- Schweizer Orthographie (ss statt ß)
- Telegrammstil beibehalten

Optional — Korrekturen:
Wenn ein Wort im committed text offensichtlich falsch transkribiert wurde, gib max. 2 Korrekturen an.
Nur bei hoher Sicherheit. Niemals Diagnosen, Dosierungen oder Markennamen ändern.

## AUSGABEFORMAT (JSON, immer)
{"new": "nur der neue text hier", "corrections": []}
corrections darf leer bleiben: []"""

SAMPLE_RATE = 16000          # 16 kHz
CHANNELS = 1                 # mono
SAMPLE_WIDTH = 2             # 16-bit = 2 bytes per sample

# Sliding window parameters
WINDOW_SIZE_S = 12           # seconds of audio to send to Whisper each time
STEP_INTERVAL_S = 2.0        # how often to run Whisper (in seconds of new audio) — provisional updates
LLM_STEP_INTERVAL_S = 10.0   # how often to run LLM correction (in seconds of new audio) — committed text
MIN_AUDIO_S = 0.5            # minimum audio before first transcription

# VAD parameters
VAD_THRESHOLD = 0.3          # speech probability threshold
SILENCE_TIMEOUT_S = 3.0      # seconds of silence before finalizing segment

# Diff / self-correction
STABLE_WORD_COUNT = 3        # words from the end that are considered "provisional"

# ---------------------------------------------------------------------------
# Voice Commands — spoken words replaced with punctuation/formatting
# ---------------------------------------------------------------------------

VOICE_COMMANDS: dict[str, str] = {
    "punkt":          ".",
    "komma":          ",",
    "doppelpunkt":    ":",
    "semikolon":      ";",
    "fragezeichen":   "?",
    "ausrufezeichen": "!",
    "neue zeile":     "\n",
    "neuer absatz":   "\n\n",
    "bindestrich":    "-",
    "schrägstrich":   "/",
    "klammer auf":    "(",
    "klammer zu":     ")",
}


def _build_vc_pattern(commands: dict[str, str]) -> re.Pattern:
    return re.compile(
        r"\b(" + "|".join(
            re.escape(cmd) for cmd in sorted(commands.keys(), key=len, reverse=True)
        ) + r")\b",
        re.IGNORECASE,
    )


_vc_pattern = _build_vc_pattern(VOICE_COMMANDS)


def apply_voice_commands(text: str, extra: dict[str, str] | None = None) -> str:
    """Replace spoken voice commands with their punctuation equivalents.

    extra: per-session custom commands (merged with built-ins, custom takes priority).
    """
    if extra:
        commands = {**VOICE_COMMANDS, **extra}
        pattern = _build_vc_pattern(commands)
    else:
        commands = VOICE_COMMANDS
        pattern = _vc_pattern

    def _replace(m: re.Match) -> str:
        return commands.get(m.group(1).lower(), m.group(0))

    result = pattern.sub(_replace, text)
    result = re.sub(r"\s+([.,;:?!)])", r"\1", result)
    result = re.sub(r"\(\s+", "(", result)
    return result


# ---------------------------------------------------------------------------
# Action Commands — spoken phrases that trigger server-side actions
# (stripped from transcript, never reach the LLM)
# ---------------------------------------------------------------------------

ACTION_COMMANDS: dict[str, str] = {
    "mach einen bericht":    "soap",
    "erstelle bericht":      "soap",
    "bericht erstellen":     "soap",
    "generiere bericht":     "soap",
    "soap erstellen":        "soap",
    "soap note erstellen":   "soap",
    "zeige soap":            "soap",
}

_ac_pattern = re.compile(
    r"\b(" + "|".join(
        re.escape(cmd) for cmd in sorted(ACTION_COMMANDS.keys(), key=len, reverse=True)
    ) + r")\b",
    re.IGNORECASE,
)

ACTION_COOLDOWN_S = 10.0  # Minimum seconds between the same action firing again


def extract_action_commands(text: str) -> tuple[str, list[str]]:
    """Detect and remove action phrases from text.

    Returns (clean_text, list_of_triggered_action_names).
    The phrase is stripped so it never appears in the transcript.
    """
    actions: list[str] = []

    def _replace(m: re.Match) -> str:
        actions.append(ACTION_COMMANDS[m.group(1).lower()])
        return ""

    clean = _ac_pattern.sub(_replace, text)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean, actions

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dictation")

# ---------------------------------------------------------------------------
# VAD (Voice Activity Detection) using Silero
# ---------------------------------------------------------------------------

class SileroVAD:
    """Lightweight VAD using Silero model (runs on CPU)."""

    def __init__(self, threshold: float = VAD_THRESHOLD):
        import torch
        self.threshold = threshold
        self.model, self.utils = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            trust_repo=True,
        )
        self.model.eval()
        log.info("Silero VAD loaded (threshold=%.2f)", threshold)

    def is_speech(self, audio_chunk: np.ndarray) -> bool:
        """Check if a chunk of audio contains speech. Expects float32 numpy array."""
        import torch
        # Silero expects 16kHz mono float32, 512 samples (32ms) works best
        # but also accepts larger chunks — we just take the max probability
        tensor = torch.from_numpy(audio_chunk).float()
        if tensor.dim() == 1:
            tensor = tensor.unsqueeze(0)
        # Process in 512-sample frames
        probs = []
        frame_size = 512
        for i in range(0, tensor.shape[1] - frame_size + 1, frame_size):
            frame = tensor[:, i:i + frame_size]
            prob = self.model(frame, SAMPLE_RATE).item()
            probs.append(prob)
        if not probs:
            return False
        return max(probs) > self.threshold

    def reset(self):
        """Reset VAD state between sessions."""
        self.model.reset_states()


# ---------------------------------------------------------------------------
# Audio Buffer
# ---------------------------------------------------------------------------

@dataclass
class AudioSession:
    """Holds audio state for one dictation session."""
    # Raw audio samples as float32 numpy arrays
    chunks: list = field(default_factory=list)
    total_samples: int = 0

    # Transcription state (Model A — primary)
    last_transcription: str = ""
    confirmed_text: str = ""
    provisional_text: str = ""
    full_text: str = ""          # Accumulated full transcript across all windows
    _prev_confirmed: str = ""    # Previous window confirmed, for delta detection
    _soap_text_len: int = 0      # Length of full_text at last SOAP generation

    # Transcription state (Model B — comparison)
    last_transcription_b: str = ""
    confirmed_text_b: str = ""
    provisional_text_b: str = ""
    compare_mode: bool = False
    whisper_only: bool = False
    live_soap: bool = False

    # Custom per-session voice commands (spoken_lower → replacement)
    custom_voice_commands: dict = field(default_factory=dict)
    # Cooldown tracking for action commands (action_name → last_fired monotonic time)
    _last_action_time: dict = field(default_factory=dict)

    # SOAP refinement — stores current field content + dirty set for LLM context
    soap_fields: dict = field(default_factory=lambda: {
        "subjective": "", "objective": "", "assessment": "", "plan": "",
    })
    dirty_fields: set = field(default_factory=set)
    _soap_unlock_pending: bool = False

    # Transcript deduplication — sliding window state
    prev_raw_text: str = ""          # raw Whisper output of previous window
    prev_words: list = field(default_factory=list)  # word timestamps of previous window
    pending_corrections: list = field(default_factory=list)  # unapplied LLM correction diffs

    # Timing
    last_whisper_call: float = 0.0
    samples_at_last_call: int = 0
    samples_at_last_llm_call: int = 0
    last_speech_time: float = 0.0
    is_active: bool = False

    # For finalization
    silence_start: Optional[float] = None

    def add_audio(self, pcm_bytes: bytes):
        """Add raw PCM 16-bit audio bytes to the buffer."""
        # Convert PCM 16-bit to float32 normalized [-1, 1]
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        self.chunks.append(samples)
        self.total_samples += len(samples)

    def get_window(self, window_seconds: float = WINDOW_SIZE_S) -> np.ndarray:
        """Get the last N seconds of audio as a single numpy array."""
        if not self.chunks:
            return np.array([], dtype=np.float32)
        max_samples = int(window_seconds * SAMPLE_RATE)
        # Walk backwards through chunks, collecting only what we need.
        # This avoids an O(total_audio) allocation when only the last window matters.
        needed: list[np.ndarray] = []
        count = 0
        for chunk in reversed(self.chunks):
            needed.append(chunk)
            count += len(chunk)
            if count >= max_samples:
                break
        needed.reverse()
        result = np.concatenate(needed)
        if len(result) > max_samples:
            return result[-max_samples:]
        return result

    def get_all_audio(self) -> np.ndarray:
        """Get all accumulated audio."""
        if not self.chunks:
            return np.array([], dtype=np.float32)
        return np.concatenate(self.chunks)

    @property
    def duration_s(self) -> float:
        return self.total_samples / SAMPLE_RATE

    @property
    def new_audio_since_last_call_s(self) -> float:
        return (self.total_samples - self.samples_at_last_call) / SAMPLE_RATE

    @property
    def new_audio_since_last_llm_call_s(self) -> float:
        return (self.total_samples - self.samples_at_last_llm_call) / SAMPLE_RATE

    def clear(self):
        """Reset the session."""
        self.chunks.clear()
        self.total_samples = 0
        self.last_transcription = ""
        self.confirmed_text = ""
        self.provisional_text = ""
        self.full_text = ""
        self._prev_confirmed = ""
        self._soap_text_len = 0
        self.last_transcription_b = ""
        self.confirmed_text_b = ""
        self.provisional_text_b = ""
        self.whisper_only = False
        # Note: compare_mode is NOT reset — it persists across sessions
        self.last_whisper_call = 0.0
        self.samples_at_last_call = 0
        self.samples_at_last_llm_call = 0
        self.last_speech_time = 0.0
        self.is_active = False
        self.silence_start = None
        self.prev_raw_text = ""
        self.prev_words = []
        self.pending_corrections = []


# ---------------------------------------------------------------------------
# Diff Engine — self-correction logic
# ---------------------------------------------------------------------------

def compute_stable_text(current: str, stable_tail: int = STABLE_WORD_COUNT) -> tuple[str, str]:
    """
    Split a Whisper transcription into confirmed and provisional parts.

    The last `stable_tail` words are provisional — they may still change in the
    next overlapping window.  Everything before them is considered confirmed.
    """
    if not current.strip():
        return "", ""

    curr_words = current.split()

    if len(curr_words) <= stable_tail:
        return "", current

    # The confirmed portion is everything except the last stable_tail words
    confirmed = " ".join(curr_words[:-stable_tail])
    provisional = " ".join(curr_words[-stable_tail:])
    return confirmed, provisional


def _word_fuzzy_eq(a: str, b: str) -> bool:
    """Fuzzy word equality for overlap detection.

    Handles case differences, trailing punctuation, and number prefixes
    (e.g. '7150' matches '7150696' because Whisper may truncate numbers
    depending on the audio window boundary).
    """
    if a == b:
        return True
    a_c = a.rstrip(".,;:!?").lower()
    b_c = b.rstrip(".,;:!?").lower()
    if a_c == b_c:
        return True
    # Number prefix tolerance: "7150" ≈ "7150696"
    if a_c.isdigit() and b_c.isdigit():
        return a_c.startswith(b_c) or b_c.startswith(a_c)
    return False


def _accumulate_full_text(session: AudioSession, confirmed: str) -> None:
    """Accumulate confirmed text into session.full_text as the window slides.

    Compares the new confirmed text against the previous window's confirmed text
    to detect new words that have slid out of the window — those become permanent.
    Uses fuzzy word matching to tolerate Whisper inconsistencies (case, punctuation,
    number truncation).
    """
    if not confirmed:
        return

    if not session._prev_confirmed:
        # First window — full_text IS the confirmed text
        session.full_text = confirmed
    else:
        prev_words = session._prev_confirmed.split()
        curr_words = confirmed.split()

        # Find overlap: longest suffix of prev that matches a prefix of curr
        max_check = min(len(prev_words), len(curr_words), 40)
        overlap = 0
        for k in range(max_check, 0, -1):
            if all(_word_fuzzy_eq(a, b) for a, b in zip(prev_words[-k:], curr_words[:k])):
                overlap = k
                break

        if overlap > 0:
            new_words = curr_words[overlap:]
        else:
            # No overlap — the window jumped; treat all confirmed as new
            new_words = curr_words

        if new_words:
            if session.full_text:
                session.full_text += " " + " ".join(new_words)
            else:
                session.full_text = " ".join(new_words)

        # Dedup: catch repeated phrase runs that slipped through overlap detection.
        # Scans for the longest repeated suffix (3-15 words) and removes the duplicate.
        ft_words = session.full_text.split()
        if len(ft_words) >= 6:
            max_phrase = min(15, len(ft_words) // 2)
            for plen in range(max_phrase, 2, -1):
                tail = ft_words[-plen:]
                # Check if the same phrase appears right before the tail
                preceding = ft_words[-(2 * plen):-plen]
                if len(preceding) == plen and all(
                    _word_fuzzy_eq(a, b) for a, b in zip(preceding, tail)
                ):
                    session.full_text = " ".join(ft_words[:-plen])
                    log.debug("Dedup removed %d repeated words from full_text", plen)
                    break

    session._prev_confirmed = confirmed


# Minimum new text (chars) before triggering a live SOAP update
_LIVE_SOAP_MIN_CHARS = 100
# Lower threshold used after a field is unlocked — fills it in sooner
_LIVE_SOAP_UNLOCK_CHARS = 80


async def _maybe_live_soap(sid: str, session: AudioSession) -> None:
    """Emit a live SOAP update if enough new text has accumulated since last one."""
    if not session.live_soap:
        return
    text = session.full_text
    threshold = _LIVE_SOAP_UNLOCK_CHARS if session._soap_unlock_pending else _LIVE_SOAP_MIN_CHARS
    new_chars = len(text) - session._soap_text_len if text else 0
    if not text or new_chars < threshold:
        return

    log.info("Live SOAP triggered: %d new chars (threshold=%d, total=%d)", new_chars, threshold, len(text))
    session._soap_text_len = len(text)

    # Fire-and-forget: generate SOAP in background so it doesn't block the loop
    asyncio.create_task(_emit_live_soap(sid, session))


async def _emit_live_soap(sid: str, session: AudioSession) -> None:
    """Generate SOAP with refinement context and emit as a live update event."""
    session._soap_unlock_pending = False   # reset after firing regardless of outcome
    try:
        await sio.emit("soap_generating", {}, room=sid)
        log.info("Live SOAP: calling LLM with %d chars, dirty=%s", len(session.full_text), session.dirty_fields)
        t0 = time.perf_counter()
        soap = await _generate_soap_llm_with_context(
            session.full_text, session.soap_fields, session.dirty_fields,
        )
        duration_ms = round((time.perf_counter() - t0) * 1000)
        if soap:
            # Store the LLM output in the session for clean fields
            for field in ("subjective", "objective", "assessment", "plan"):
                if field not in session.dirty_fields:
                    session.soap_fields[field] = soap.get(field, "")
            log.info("Live SOAP emitted in %d ms", duration_ms)
            soap["duration_ms"] = duration_ms
            soap["dirty_protected"] = list(session.dirty_fields)
            await sio.emit("soap_update", soap, room=sid)
        else:
            log.warning("Live SOAP: LLM returned None after %d ms", duration_ms)
    except Exception as e:
        log.warning("Live SOAP generation failed: %s", e, exc_info=True)


# ---------------------------------------------------------------------------
# Whisper Client
# ---------------------------------------------------------------------------

async def call_whisper(audio: np.ndarray, session: aiohttp.ClientSession) -> tuple[str, list]:
    """Send audio to the Whisper API and return (text, []).

    Word timestamps are not requested — the GPUStack Whisper endpoint does not
    support verbose_json.  The second element is always an empty list so that
    callers can unpack uniformly as `text, words = await call_whisper(...)`.
    """
    # Convert float32 numpy array to WAV bytes
    wav_buffer = io.BytesIO()
    audio_int16 = (audio * 32768).clip(-32768, 32767).astype(np.int16)
    with wave.open(wav_buffer, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_int16.tobytes())
    wav_buffer.seek(0)

    form = aiohttp.FormData()
    form.add_field("file", wav_buffer, filename="audio.wav", content_type="audio/wav")
    form.add_field("model", WHISPER_MODEL)
    form.add_field("language", WHISPER_LANGUAGE)
    form.add_field("response_format", "json")

    headers = {}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"

    try:
        t0 = time.monotonic()
        async with session.post(
            WHISPER_API_URL, data=form, headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            elapsed = time.monotonic() - t0
            if resp.status == 200:
                result = await resp.json()
                text = result.get("text", "").strip()
                log.info("Whisper responded in %.2fs: '%s'", elapsed, text[:80])
                return text, []
            else:
                body = await resp.text()
                log.error("Whisper API error %d: %s", resp.status, body[:200])
                return "", []
    except Exception as e:
        log.error("Whisper API call failed: %s", e)
        return "", []


# ---------------------------------------------------------------------------
# LLM Text Correction (Mistral via GPUStack)
# ---------------------------------------------------------------------------

async def _call_llm(system_prompt: str, user_content: str,
                    http_session: aiohttp.ClientSession,
                    timeout: float = 15.0, label: str = "LLM",
                    model: str = "", max_tokens: int = 1024) -> str | None:
    """Internal helper — call an LLM and return the response text, or None on failure."""
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"

    payload = {
        "model": model or LLM_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }

    try:
        t0 = time.monotonic()
        async with http_session.post(
            LLM_API_URL, json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            elapsed = time.monotonic() - t0
            if resp.status == 200:
                result = await resp.json()
                text = result["choices"][0]["message"]["content"].strip()
                if text.startswith("```") and text.endswith("```"):
                    text = text[3:-3].strip()
                log.info("%s responded in %.2fs", label, elapsed)
                return text
            else:
                body = await resp.text()
                log.warning("%s failed %d: %s", label, resp.status, body[:200])
                return None
    except asyncio.TimeoutError:
        log.warning("%s timed out", label)
        return None
    except Exception as e:
        log.warning("%s error: %s", label, e)
        return None


def _extract_json(raw: str) -> dict | None:
    """Robustly extract a JSON object from an LLM response string.

    Handles common LLM output patterns:
      - plain JSON: {"new": "...", "corrections": [...]}
      - markdown fences: ```json\n{...}\n```
      - "json " prefix: json {"new": ...}
      - prose before JSON: "Here is the output:\n{...}"
    """
    text = raw.strip()

    # Strip markdown code fences
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    # Strip leading "json" word (e.g. "json {" or "JSON\n{")
    text = re.sub(r"^json\s*", "", text, flags=re.IGNORECASE)

    # Try direct parse first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Find the first {...} block using regex (handles prose before/after JSON)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass

    return None


async def _correct_window_simple(
    confirmed_so_far: str,
    previous_window: str,
    current_window: str,
    http_session: aiohttp.ClientSession,
    model: str = "",
) -> str:
    """Simple per-window correction (used in compare mode).

    Resolves conflicts in the overlapping region between two consecutive
    Whisper windows using semantic/medical context. Returns corrected string.
    """
    if not LLM_CORRECTION_ENABLED or not current_window.strip():
        return current_window

    user_content = (
        f"KONTEXT (bereits bestätigter Text):\n{confirmed_so_far or '(noch kein Text)'}\n\n"
        f"VORHERIGES_FENSTER:\n{previous_window or '(erstes Fenster)'}\n\n"
        f"AKTUELLES_FENSTER:\n{current_window}"
    )

    result = await _call_llm(
        LLM_WINDOW_CORRECTION_PROMPT, user_content, http_session,
        timeout=12.0, label=f"LLM-window[{model or LLM_MODEL}]",
        model=model,
    )
    if result:
        log.info("Window corrected [%s]: '%s' → '%s'", model or LLM_MODEL, current_window[:60], result[:60])
    return result or current_window


async def correct_window_with_llm(
    full_text: str,
    prev_raw_text: str,
    overlap_prev: str,
    overlap_curr: str,
    new_slice: str,
    curr_raw_text: str,
    http_session: aiohttp.ClientSession,
    model: str = "",
) -> dict:
    """Overlap-aware per-window correction. Returns {"new": str, "corrections": list}.

    Sends the committed text, both overlapping window transcriptions, and the new
    2s slice to the LLM.  The LLM outputs only the genuinely new content plus
    optional correction diffs for words that differed between the two windows.
    """
    if not LLM_CORRECTION_ENABLED or not curr_raw_text.strip():
        return {"new": curr_raw_text, "corrections": []}

    user_content = (
        f"## COMMITTED TEXT (bereits bestätigt — nicht wiederholen)\n"
        f"{full_text or '(noch kein Text)'}\n\n"
        f"## VORHERIGES WHISPER-FENSTER (Referenz)\n"
        f"{prev_raw_text or '(erstes Fenster)'}\n\n"
        f"## AKTUELLES WHISPER-FENSTER\n"
        f"{curr_raw_text}"
    )

    raw = await _call_llm(
        CORRECTION_PROMPT_V2, user_content, http_session,
        timeout=12.0, label="LLM-window-v2",
        model=model,
    )
    if not raw:
        return {"new": curr_raw_text, "corrections": []}

    parsed = _extract_json(raw)
    if parsed and isinstance(parsed, dict):
        return {
            "new": str(parsed.get("new", new_slice or "")),
            "corrections": parsed.get("corrections", []) if isinstance(parsed.get("corrections"), list) else [],
        }

    # Could not parse JSON — use raw output as new text only if it looks like prose
    # (not JSON garbage).  If it still contains '{', discard it to avoid polluting
    # the transcript with raw JSON.
    if "{" in raw:
        log.warning("LLM-window-v2 non-parseable JSON — discarding to avoid pollution: %r", raw[:120])
        return {"new": curr_raw_text, "corrections": []}

    log.warning("LLM-window-v2 returned plain text (no JSON): %r", raw[:120])
    return {"new": raw.strip(), "corrections": []}


async def correct_final_with_llm(text: str, http_session: aiohttp.ClientSession,
                                 model: str = "") -> str:
    """Final correction pass on the complete accumulated dictation text."""
    if not LLM_CORRECTION_ENABLED or not text.strip():
        return text

    result = await _call_llm(
        LLM_FINAL_CORRECTION_PROMPT, text, http_session,
        timeout=20.0, label=f"LLM-final[{model or LLM_MODEL}]",
        model=model,
    )
    if result:
        log.info("Final corrected [%s]: '%s' → '%s'", model or LLM_MODEL, text[:60], result[:60])
    return result or text


def apply_corrections(
    full_text: str,
    corrections: list[dict],
) -> tuple[str, list[dict]]:
    """Apply LLM correction diffs to full_text.

    Returns (updated_full_text, applied_corrections_with_offsets).
    Replaces only the last occurrence of each 'from' string (most recently spoken = most likely wrong).
    Caps at 2 corrections per call.  Skips corrections where 'from' is not found.
    """
    applied: list[dict] = []
    for c in corrections[:2]:
        original = c.get("from", "").strip()
        corrected = c.get("to", "").strip()
        if not original or not corrected or original.lower() == corrected.lower():
            continue
        idx = full_text.lower().rfind(original.lower())
        if idx == -1:
            continue
        full_text = full_text[:idx] + corrected + full_text[idx + len(original):]
        applied.append({
            "id": str(uuid.uuid4())[:8],
            "original": original,
            "corrected": corrected,
            "offset": idx,
        })
    return full_text, applied


async def _emit_final_transcription(sid: str, raw_text: str, session: AudioSession) -> None:
    """Run final LLM correction (with optional Model B) and emit results to client.

    Extracted to avoid duplication between stop_dictation, transcribe_file_stream,
    and silence-timeout finalization.
    """
    if session.whisper_only:
        # Skip LLM — emit raw Whisper output directly
        await sio.emit("transcription", {
            "confirmed": raw_text, "provisional": "", "is_final": True,
            "raw_whisper": raw_text,
        }, room=sid)
        return

    if session.compare_mode:
        corrected_a, corrected_b = await asyncio.gather(
            correct_final_with_llm(raw_text, http_session),
            correct_final_with_llm(raw_text, http_session, model=LLM_MODEL_B),
        )
        await sio.emit("transcription", {
            "confirmed": corrected_a, "provisional": "", "is_final": True,
            "raw_whisper": raw_text,
        }, room=sid)
        await sio.emit("transcription_compare", {
            "confirmed_a": corrected_a, "confirmed_b": corrected_b,
            "provisional_a": "", "provisional_b": "",
            "is_final": True, "raw_whisper": raw_text,
            "model_a": LLM_MODEL,
            "model_b": LLM_MODEL_B_NAME or LLM_MODEL_B,
        }, room=sid)
    else:
        corrected = await correct_final_with_llm(raw_text, http_session)
        await sio.emit("transcription", {
            "confirmed": corrected, "provisional": "", "is_final": True,
            "raw_whisper": raw_text,
        }, room=sid)


# ---------------------------------------------------------------------------
# Socket.IO + FastAPI App
# ---------------------------------------------------------------------------

# Create FastAPI app
fastapi_app = FastAPI(title="Vitodata Streaming Dictation")
# CORS — restrict in production via CORS_ORIGINS env var
_cors_origins = os.environ.get("CORS_ORIGINS", "*")
_cors_origins_list = [o.strip() for o in _cors_origins.split(",")] if _cors_origins != "*" else ["*"]

fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create Socket.IO server
# max_http_buffer_size raised to 100 MB to support large audio file uploads
sio = socketio.AsyncServer(
    async_mode="asgi",
    cors_allowed_origins=_cors_origins_list if _cors_origins != "*" else "*",
    max_http_buffer_size=100 * 1024 * 1024,
    logger=False,
    engineio_logger=False,
)

# Sessions per client
sessions: dict[str, AudioSession] = {}
vad: Optional[SileroVAD] = None
http_session: Optional[aiohttp.ClientSession] = None

# Background tasks per client
transcription_tasks: dict[str, asyncio.Task] = {}


@fastapi_app.on_event("startup")
async def startup():
    global vad, http_session
    log.info("Starting Vitodata Streaming Dictation Server...")
    log.info("Whisper API: %s", WHISPER_API_URL)
    log.info("LLM Correction: %s (model: %s, url: %s)",
             "ENABLED" if LLM_CORRECTION_ENABLED else "DISABLED",
             LLM_MODEL, LLM_API_URL)

    # Load VAD
    if not VAD_ENABLED:
        log.info("VAD disabled via VAD_ENABLED=false — skipping torch/Silero load")
    else:
        try:
            vad = SileroVAD(threshold=VAD_THRESHOLD)
        except Exception as e:
            log.warning("Could not load Silero VAD, running without VAD: %s", e)
            vad = None

    # Create aiohttp session for Whisper API calls
    http_session = aiohttp.ClientSession()
    log.info("Server ready.")


@fastapi_app.on_event("shutdown")
async def shutdown():
    global http_session
    if http_session:
        await http_session.close()
    # Cancel all background tasks
    for task in transcription_tasks.values():
        task.cancel()


# Read static index.html once at startup (non-blocking)
_static_index_html: str = ""
try:
    with open("static/index.html", "r", encoding="utf-8") as _f:
        _static_index_html = _f.read()
except FileNotFoundError:
    pass


@fastapi_app.get("/")
async def index():
    """Serve the test frontend."""
    if _static_index_html:
        return HTMLResponse(_static_index_html)
    return HTMLResponse("<h1>Vitodata Streaming Dictation Server</h1><p>Frontend not found.</p>")


@fastapi_app.get("/health")
async def health():
    return {"status": "ok", "whisper_url": WHISPER_API_URL}


@fastapi_app.get("/v1/compare-models")
async def list_compare_models():
    """Return the available LLM models for side-by-side comparison."""
    models = [{"id": LLM_MODEL, "name": LLM_MODEL}]
    if LLM_MODEL_B:
        models.append({"id": LLM_MODEL_B, "name": LLM_MODEL_B_NAME or LLM_MODEL_B})
    return {"models": models, "compare_enabled": bool(LLM_MODEL_B)}


# --- Socket.IO Events ---

@sio.event
async def connect(sid, environ):
    log.info("Client connected: %s", sid)
    sessions[sid] = AudioSession()


@sio.event
async def disconnect(sid):
    log.info("Client disconnected: %s", sid)
    sessions.pop(sid, None)
    task = transcription_tasks.pop(sid, None)
    if task:
        task.cancel()
    # Only reset VAD when no other session is actively recording
    if vad and not any(s.is_active for s in sessions.values()):
        vad.reset()


@sio.event
async def start_dictation(sid, data=None):
    """Client starts a dictation session."""
    compare = data.get("compare", False) if isinstance(data, dict) else False
    whisper_only = data.get("whisper_only", False) if isinstance(data, dict) else False
    log.info("Dictation started: %s (compare=%s, whisper_only=%s)", sid, compare, whisper_only)
    session = sessions.get(sid)
    if session:
        session.clear()
        session.compare_mode = bool(compare and LLM_MODEL_B)
        session.whisper_only = bool(whisper_only)
        session.is_active = True
        session.last_speech_time = time.monotonic()
    if vad:
        vad.reset()

    # Start the background transcription loop
    task = transcription_tasks.get(sid)
    if task:
        task.cancel()
    transcription_tasks[sid] = asyncio.create_task(transcription_loop(sid))

    await sio.emit("dictation_started", {}, room=sid)


@sio.event
async def set_compare_mode(sid, data=None):
    """Toggle comparison mode mid-session."""
    session = sessions.get(sid)
    if session:
        enabled = data.get("enabled", False) if isinstance(data, dict) else False
        session.compare_mode = bool(enabled and LLM_MODEL_B)
        log.info("Compare mode %s for %s", "ON" if session.compare_mode else "OFF", sid)


@sio.event
async def stop_dictation(sid, data=None):
    """Client stops dictation. Do one final transcription."""
    log.info("Dictation stopped: %s", sid)
    session = sessions.get(sid)
    if session:
        session.is_active = False

    # Cancel background loop
    task = transcription_tasks.pop(sid, None)
    if task:
        task.cancel()

    # Final transcription of all accumulated audio
    if session and session.total_samples > 0 and http_session:
        audio = session.get_all_audio()
        text, _ = await call_whisper(audio, http_session)
        text = apply_voice_commands(text, session.custom_voice_commands or None)
        text, _ = extract_action_commands(text)
        if text:
            await _emit_final_transcription(sid, text, session)


@sio.event
async def audio_chunk(sid, data):
    """
    Receive a chunk of PCM audio from the client.
    data: raw bytes (PCM 16-bit, 16kHz, mono)
    """
    session = sessions.get(sid)
    if not session or not session.is_active:
        return

    if isinstance(data, dict):
        # If sent as {chunk: ArrayBuffer} like Medicus protocol
        chunk_data = data.get("chunk", data.get("data", b""))
    else:
        chunk_data = data

    if not chunk_data:
        return

    session.add_audio(chunk_data)

    # Update speech timing with VAD
    if vad:
        # Check the last 32ms of audio for speech
        check_samples = min(512, session.total_samples)
        if check_samples > 0:
            recent = session.get_window(check_samples / SAMPLE_RATE)
            if len(recent) >= 512:
                if vad.is_speech(recent[-512:]):
                    session.last_speech_time = time.monotonic()
                    session.silence_start = None
                else:
                    if session.silence_start is None:
                        session.silence_start = time.monotonic()


async def transcription_loop(sid: str):
    """Background loop that periodically calls Whisper on the sliding window."""
    session = sessions.get(sid)
    if not session or not http_session:
        return

    try:
        while session.is_active:
            await asyncio.sleep(0.3)  # Check every 300ms

            # Check if we have enough new audio to warrant a Whisper call
            if session.duration_s < MIN_AUDIO_S:
                continue

            if session.new_audio_since_last_call_s < STEP_INTERVAL_S:
                continue

            # Get the sliding window
            audio_window = session.get_window(WINDOW_SIZE_S)
            if len(audio_window) < int(MIN_AUDIO_S * SAMPLE_RATE):
                continue

            # Mark that we're making a call
            session.samples_at_last_call = session.total_samples

            # Call Whisper
            raw_text, _ = await call_whisper(audio_window, http_session)
            raw_text = apply_voice_commands(raw_text, session.custom_voice_commands or None)
            raw_text, raw_actions = extract_action_commands(raw_text)

            # Fire action commands (with cooldown to avoid sliding-window re-fires)
            _now = time.monotonic()
            for _action in raw_actions:
                if _now - session._last_action_time.get(_action, 0) > ACTION_COOLDOWN_S:
                    session._last_action_time[_action] = _now
                    if _action == "soap" and session.full_text:
                        log.info("Action command 'soap' triggered for %s", sid)
                        asyncio.create_task(_emit_live_soap(sid, session))

            if not raw_text:
                continue

            if session.whisper_only:
                # ── Whisper-only: skip LLM, use raw Whisper text ──
                new_text = raw_text
                confirmed, provisional = compute_stable_text(new_text)
                session.last_transcription = new_text
                session.confirmed_text = confirmed
                session.provisional_text = provisional
                _accumulate_full_text(session, confirmed)
                await sio.emit("transcription", {
                    "confirmed": confirmed, "provisional": provisional,
                    "full_text": session.full_text,
                    "is_final": False, "raw_whisper": raw_text,
                }, room=sid)

                # Trigger live SOAP in whisper-only mode too
                await _maybe_live_soap(sid, session)

                if session.silence_start is not None:
                    silence_duration = time.monotonic() - session.silence_start
                    if silence_duration > SILENCE_TIMEOUT_S:
                        log.info("Silence timeout (whisper-only), finalizing")
                        await _emit_final_transcription(sid, new_text, session)
                        session.silence_start = None

            elif session.compare_mode:
                # ── Comparison mode: run both models in parallel ──
                new_text_a, new_text_b = await asyncio.gather(
                    _correct_window_simple(
                        session.confirmed_text, session.last_transcription,
                        raw_text, http_session,
                    ),
                    _correct_window_simple(
                        session.confirmed_text_b, session.last_transcription_b,
                        raw_text, http_session, model=LLM_MODEL_B,
                    ),
                )

                # Diff for Model A
                confirmed_a, provisional_a = compute_stable_text(new_text_a)
                session.last_transcription = new_text_a
                session.confirmed_text = confirmed_a
                session.provisional_text = provisional_a
                _accumulate_full_text(session, confirmed_a)

                # Diff for Model B
                confirmed_b, provisional_b = compute_stable_text(new_text_b)
                session.last_transcription_b = new_text_b
                session.confirmed_text_b = confirmed_b
                session.provisional_text_b = provisional_b

                # Emit primary transcription (Model A)
                await sio.emit("transcription", {
                    "confirmed": confirmed_a,
                    "provisional": provisional_a,
                    "full_text": session.full_text,
                    "is_final": False,
                    "raw_whisper": raw_text,
                }, room=sid)

                await _maybe_live_soap(sid, session)

                # Emit comparison event with both
                await sio.emit("transcription_compare", {
                    "confirmed_a": confirmed_a,
                    "confirmed_b": confirmed_b,
                    "provisional_a": provisional_a,
                    "provisional_b": provisional_b,
                    "is_final": False,
                    "raw_whisper": raw_text,
                    "model_a": LLM_MODEL,
                    "model_b": LLM_MODEL_B_NAME or LLM_MODEL_B,
                }, room=sid)

                # Silence timeout for compare mode
                if session.silence_start is not None:
                    silence_duration = time.monotonic() - session.silence_start
                    if silence_duration > SILENCE_TIMEOUT_S:
                        log.info("Silence timeout (compare), finalizing with both models")
                        await _emit_final_transcription(sid, raw_text, session)
                        session.silence_start = None

            else:
                # ── Normal single-model mode ──
                # Decoupled cadence:
                #   • Whisper fires every STEP_INTERVAL_S (2s) — shows raw tail as provisional
                #   • LLM fires every LLM_STEP_INTERVAL_S (10s) — commits clean corrected text
                # With a 10s LLM step and 12s window the overlap is only ~2s (≈4-6 words),
                # making exact word-level deduplication reliable.

                # Always update provisional from raw Whisper output
                raw_confirmed, raw_provisional = compute_stable_text(raw_text)
                session.provisional_text = raw_provisional

                if session.new_audio_since_last_llm_call_s >= LLM_STEP_INTERVAL_S:
                    # LLM correction cycle — commit clean text
                    session.samples_at_last_llm_call = session.total_samples

                    new_text = await _correct_window_simple(
                        session.confirmed_text, session.last_transcription,
                        raw_text, http_session,
                    )

                    session.prev_raw_text = raw_text
                    confirmed, provisional = compute_stable_text(new_text)
                    session.last_transcription = new_text
                    session.confirmed_text = confirmed
                    session.provisional_text = provisional
                    _accumulate_full_text(session, confirmed)

                    await sio.emit("transcription", {
                        "confirmed": confirmed,
                        "provisional": provisional,
                        "full_text": session.full_text,
                        "is_final": False,
                        "raw_whisper": raw_text,
                    }, room=sid)
                else:
                    # Provisional-only update — raw Whisper tail shown in amber
                    await sio.emit("transcription", {
                        "confirmed": session.confirmed_text,
                        "provisional": raw_provisional,
                        "full_text": session.full_text,
                        "is_final": False,
                        "raw_whisper": raw_text,
                    }, room=sid)

                # Check live SOAP after every Whisper step (fires only when full_text crosses threshold)
                await _maybe_live_soap(sid, session)

                # Check for silence timeout → auto-finalize with full final correction
                if session.silence_start is not None:
                    silence_duration = time.monotonic() - session.silence_start
                    if silence_duration > SILENCE_TIMEOUT_S:
                        log.info("Silence timeout, finalizing segment with LLM final correction")
                        await _emit_final_transcription(sid, raw_text, session)
                        session.silence_start = None

    except asyncio.CancelledError:
        log.info("Transcription loop cancelled for %s", sid)
    except Exception as e:
        log.error("Transcription loop error for %s: %s", sid, e, exc_info=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def resample_to_16k(audio: np.ndarray, orig_sr: int) -> np.ndarray:
    """Resample mono float32 audio to 16 kHz using torchaudio, numpy fallback."""
    if orig_sr == SAMPLE_RATE:
        return audio
    try:
        import torch, torchaudio
        t = torch.from_numpy(audio).unsqueeze(0).float()
        t = torchaudio.functional.resample(t, orig_sr, SAMPLE_RATE)
        return t.squeeze(0).numpy()
    except Exception:
        n_out = int(len(audio) * SAMPLE_RATE / orig_sr)
        return np.interp(
            np.linspace(0, len(audio) - 1, n_out),
            np.arange(len(audio)),
            audio,
        ).astype(np.float32)


def load_audio_bytes(data: bytes) -> tuple[np.ndarray, int]:
    """
    Decode audio bytes (WAV / MP3 / FLAC / OGG / M4A …).
    Returns (float32 mono numpy array, sample_rate).
    Tries: torchaudio → soundfile → stdlib wave (PCM WAV only).
    """
    buf = io.BytesIO(data if isinstance(data, bytes) else bytes(data))
    # --- Strategy 1: torchaudio (broadest format support) ---
    try:
        import torch, torchaudio
        waveform, sr = torchaudio.load(buf)          # (channels, samples)
        if waveform.shape[0] > 1:
            waveform = waveform.mean(dim=0, keepdim=True)
        return waveform.squeeze(0).numpy().astype(np.float32), sr
    except Exception:
        pass

    # --- Strategy 2: soundfile (WAV, FLAC, OGG) ---
    buf.seek(0)
    try:
        import soundfile as sf
        audio, sr = sf.read(buf, dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        return audio.astype(np.float32), sr
    except Exception:
        pass

    # --- Strategy 3: stdlib wave fallback (PCM WAV only) ---
    buf.seek(0)
    with wave.open(buf) as wf:
        sr         = wf.getframerate()
        n_ch       = wf.getnchannels()
        raw_bytes  = wf.readframes(wf.getnframes())
    audio_i16 = np.frombuffer(raw_bytes, dtype=np.int16)
    if n_ch > 1:
        audio_i16 = audio_i16.reshape(-1, n_ch).mean(axis=1).astype(np.int16)
    return audio_i16.astype(np.float32) / 32768.0, sr


# ---------------------------------------------------------------------------
# File-streaming Socket.IO event
# ---------------------------------------------------------------------------

@sio.event
async def transcribe_file_stream(sid, data):
    """
    Receive audio bytes (any format torchaudio supports) and push them through
    the same sliding-window Whisper + Mistral pipeline as the mic.

    Emits the same `transcription` events as live recording so the frontend
    needs no special handling.  Extra lifecycle events:
      file_stream_started  { duration: float }   — decode done, feeding begins
      file_stream_done     {}                     — final correction emitted
      transcription_error  { error: str }         — decode failure
    """
    session = sessions.get(sid)
    if not session or not http_session:
        return

    # ── Decode ──
    try:
        audio, orig_sr = load_audio_bytes(data)
    except Exception as e:
        log.error("transcribe_file_stream: decode failed for %s: %s", sid, e)
        await sio.emit("transcription_error", {"error": str(e)}, room=sid)
        return

    audio = resample_to_16k(audio, orig_sr)
    duration_s = len(audio) / SAMPLE_RATE
    log.info("file_stream [%s]: %.1fs, orig %d Hz", sid, duration_s, orig_sr)

    # ── Reset session and start transcription loop ──
    # Preserve mode settings from current session
    compare_mode = session.compare_mode
    whisper_only = session.whisper_only
    live_soap = session.live_soap
    session.clear()
    session.compare_mode   = compare_mode
    session.whisper_only   = whisper_only
    session.live_soap      = live_soap
    session.is_active      = True
    session.last_speech_time = time.monotonic()
    if vad:
        vad.reset()

    task = transcription_tasks.get(sid)
    if task:
        task.cancel()
    transcription_tasks[sid] = asyncio.create_task(transcription_loop(sid))

    await sio.emit("file_stream_started", {"duration": round(duration_s, 1)}, room=sid)

    # ── Whisper-only: single Whisper call, no streaming ──
    if session.whisper_only:
        log.info("file_stream [%s]: whisper-only mode, single call", sid)
        session.is_active = False
        task = transcription_tasks.pop(sid, None)
        if task:
            task.cancel()

        raw_text, _ = await call_whisper(audio, http_session)
        raw_text = apply_voice_commands(raw_text, session.custom_voice_commands or None)
        raw_text, _ = extract_action_commands(raw_text)
        if raw_text:
            await sio.emit("transcription", {
                "confirmed": raw_text, "provisional": "", "is_final": True,
                "raw_whisper": raw_text,
            }, room=sid)

        await sio.emit("file_stream_done", {}, room=sid)
        log.info("file_stream [%s]: whisper-only done", sid)
        return

    # ── Feed audio chunks at 2× real-time speed ──
    # Each slice = STEP_INTERVAL_S worth of samples; fed every STEP_INTERVAL_S/2 s
    chunk_samples = int(STEP_INTERVAL_S * SAMPLE_RATE)
    feed_delay_s  = STEP_INTERVAL_S / 2
    feed_start = time.monotonic()

    for i in range(0, len(audio), chunk_samples):
        if not session.is_active:
            break
        chunk_f32 = audio[i: i + chunk_samples]
        chunk_i16 = (chunk_f32 * 32768).clip(-32768, 32767).astype(np.int16)
        session.add_audio(chunk_i16.tobytes())

        # Emit progress so frontend can show elapsed / total
        elapsed_audio = (i + len(chunk_f32)) / SAMPLE_RATE
        await sio.emit("file_stream_progress", {
            "elapsed": round(min(elapsed_audio, duration_s), 1),
            "duration": round(duration_s, 1),
        }, room=sid)

        await asyncio.sleep(feed_delay_s)

    # Wait for the transcription loop to finish processing buffered audio.
    await sio.emit("file_stream_status", {"step": "waiting", "message": "Warte auf Transkriptions-Loop…"}, room=sid)
    for _ in range(int(WINDOW_SIZE_S * 2)):
        if session.samples_at_last_call >= session.total_samples:
            break
        await asyncio.sleep(0.5)

    # ── Stop loop ──
    session.is_active = False
    task = transcription_tasks.pop(sid, None)
    if task:
        task.cancel()

    # ── Final LLM correction on accumulated text ──
    accumulated = (session.full_text + " " + session.provisional_text).strip()
    final_text = accumulated or session.confirmed_text
    if final_text:
        await sio.emit("file_stream_status", {"step": "correcting", "message": "LLM-Schlusskorrektur…"}, room=sid)
        await _emit_final_transcription(sid, final_text, session)

    await sio.emit("file_stream_status", {"step": "done", "message": "Fertig"}, room=sid)

    await sio.emit("file_stream_done", {}, room=sid)
    log.info("file_stream [%s]: done", sid)


@sio.event
async def stop_file_stream(sid, data=None):
    """Cancel an in-progress file stream transcription."""
    session = sessions.get(sid)
    if session:
        session.is_active = False  # causes the feed loop to break
        log.info("File stream cancelled by client: %s", sid)
    task = transcription_tasks.pop(sid, None)
    if task:
        task.cancel()
    await sio.emit("file_stream_done", {}, room=sid)


@sio.event
async def set_whisper_only(sid, data=None):
    """Toggle whisper-only mode (skip LLM correction)."""
    session = sessions.get(sid)
    if session:
        enabled = data.get("enabled", False) if isinstance(data, dict) else False
        session.whisper_only = bool(enabled)
        log.info("Whisper-only mode %s for %s", "ON" if session.whisper_only else "OFF", sid)


@sio.event
async def set_live_soap(sid, data=None):
    """Toggle live SOAP generation during streaming."""
    session = sessions.get(sid)
    if session:
        enabled = data.get("enabled", False) if isinstance(data, dict) else False
        session.live_soap = bool(enabled)
        log.info("Live SOAP mode %s for %s", "ON" if session.live_soap else "OFF", sid)


@sio.event
async def update_soap_field(sid, data=None):
    """Doctor edited a SOAP field — store content and mark dirty."""
    session = sessions.get(sid)
    if not session or not isinstance(data, dict):
        return
    field = data.get("field", "")
    text  = data.get("text", "")
    if field not in ("subjective", "objective", "assessment", "plan"):
        return
    session.soap_fields[field] = text
    session.dirty_fields.add(field)


@sio.event
async def unlock_soap_field(sid, data=None):
    """Doctor unlocked a field — remove dirty flag and lower next SOAP threshold."""
    session = sessions.get(sid)
    if not session or not isinstance(data, dict):
        return
    field = data.get("field", "")
    if field not in ("subjective", "objective", "assessment", "plan"):
        return
    session.dirty_fields.discard(field)
    session._soap_unlock_pending = True
    log.info("Field '%s' unlocked for %s — next SOAP triggers at %d chars", field, sid, _LIVE_SOAP_UNLOCK_CHARS)


@sio.event
async def revert_correction(sid, data=None):
    """Doctor reverted an LLM correction — restore the original ASR word."""
    session = sessions.get(sid)
    if not session or not isinstance(data, dict):
        return
    correction_id = data.get("id", "")
    corr = next((c for c in session.pending_corrections if c["id"] == correction_id), None)
    if not corr:
        return
    idx = corr["offset"]
    end = idx + len(corr["corrected"])
    # Only revert if the text at that offset still matches the corrected version
    if session.full_text[idx:end] == corr["corrected"]:
        session.full_text = session.full_text[:idx] + corr["original"] + session.full_text[end:]
    session.pending_corrections = [c for c in session.pending_corrections if c["id"] != correction_id]
    await sio.emit("transcript_corrected", {
        "full_text": session.full_text,
        "reverted_id": correction_id,
    }, room=sid)


@sio.event
async def set_voice_commands(sid, data=None):
    """Update custom voice commands for this session.

    data: { commands: [{spoken: str, replacement: str}, ...] }
    Replacement strings support \\n for newline and \\n\\n for paragraph break.
    """
    session = sessions.get(sid)
    if not session or not isinstance(data, dict):
        return
    commands_list = data.get("commands", [])
    if not isinstance(commands_list, list):
        return
    session.custom_voice_commands = {
        item["spoken"].strip().lower(): item["replacement"].replace("\\n", "\n")
        for item in commands_list
        if isinstance(item, dict) and item.get("spoken") and "replacement" in item
    }
    log.info("Custom voice commands updated for %s: %d commands", sid, len(session.custom_voice_commands))


# ---------------------------------------------------------------------------
# WAV file upload endpoint (non-streaming, for comparison/testing)
# ---------------------------------------------------------------------------

@fastapi_app.post("/v1/transcribe")
async def transcribe_file(file: UploadFile = File(...)):
    """Upload an audio file for batch transcription (non-streaming). Supports WAV, MP3, FLAC, OGG, M4A."""
    if not http_session:
        return {"error": "Server not ready"}

    content = await file.read()

    # Decode and resample to 16kHz mono (handles all formats torchaudio supports)
    try:
        audio, orig_sr = load_audio_bytes(content)
        audio = resample_to_16k(audio, orig_sr)
    except Exception as e:
        return {"error": f"Audiodatei konnte nicht geladen werden: {e}"}

    # Re-encode as WAV for Whisper API
    wav_buffer = io.BytesIO()
    audio_int16 = (audio * 32768).clip(-32768, 32767).astype(np.int16)
    with wave.open(wav_buffer, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_int16.tobytes())
    wav_buffer.seek(0)

    form = aiohttp.FormData()
    form.add_field("file", wav_buffer, filename="audio.wav", content_type="audio/wav")
    form.add_field("model", WHISPER_MODEL)
    form.add_field("language", WHISPER_LANGUAGE)

    headers = {}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"

    async with http_session.post(
        WHISPER_API_URL, data=form, headers=headers,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        if resp.status == 200:
            result = await resp.json()
            raw_text = result.get("text", "")
            corrected = await correct_final_with_llm(raw_text, http_session)
            return {"text": corrected, "raw_whisper": raw_text, "status": "ok"}
        else:
            body = await resp.text()
            return {"error": body, "status": resp.status}


# ---------------------------------------------------------------------------
# SOAP Note Generation + Scoring
# ---------------------------------------------------------------------------

SOAP_SCORE_PROMPT = """Du bewertest eine SOAP-Notiz, die aus einem medizinischen Diktat-Transkript generiert wurde.

Bewerte zwei Dimensionen:

1. TREUE (faithfulness): Ist jede Aussage in der SOAP-Notiz direkt im Transkript belegt?
   1.0 = Alles belegt, keine erfundenen Informationen
   0.5 = Einige Aussagen unklar oder leicht abgewandelt
   0.0 = Viele Aussagen sind erfunden oder halluziniert

2. VOLLSTÄNDIGKEIT (completeness): Wurden alle klinisch relevanten Fakten erfasst?
   Dazu gehören: Medikamente, Dosierungen, Diagnosen, Vitalwerte, Symptome,
   Anordnungen, verweigerte/gestoppte Medikamente.
   1.0 = Alles erfasst
   0.5 = Einige wichtige Fakten fehlen
   0.0 = Die meisten Fakten fehlen

Transkript:
{transcript}

SOAP-Notiz:
S: {S}
O: {O}
A: {A}
P: {P}

Antworte NUR mit einem JSON-Objekt: {{"faithfulness": <float 0..1>, "completeness": <float 0..1>}}"""


async def _score_soap(transcript: str, soap: dict) -> dict:
    """Score a SOAP output against its source transcript using the LLM as judge."""
    if not LLM_CORRECTION_ENABLED or not http_session:
        return {}

    prompt = SOAP_SCORE_PROMPT.format(
        transcript=transcript,
        S=soap.get("subjective", ""),
        O=soap.get("objective", ""),
        A=soap.get("assessment", ""),
        P=soap.get("plan", ""),
    )

    result = await _call_llm(
        "Du bist ein klinischer Qualitätsauditor für SOAP-Notizen. Antworte nur mit JSON.",
        prompt, http_session,
        timeout=15.0, label="LLM-SOAP-score",
    )
    if not result:
        return {}

    try:
        cleaned = result.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned.rsplit("```", 1)[0]
        data = json.loads(cleaned.strip())
        return {
            "faithfulness":  max(0.0, min(1.0, float(data.get("faithfulness", 0)))),
            "completeness":  max(0.0, min(1.0, float(data.get("completeness", 0)))),
        }
    except Exception as e:
        log.warning("Could not parse SOAP scores: %s", e)
        return {}


SOAP_GENERATION_PROMPT = """## CRITICAL: Output Language = Transcript Language

STEP 0 — BEFORE ANY PROCESSING: Detect the language of the transcript.
- Output the SOAP note in THAT language. NOT in German. NOT in English. In the TRANSCRIPT's language.
- Italian transcript → Italian SOAP. French transcript → French SOAP. German transcript → German SOAP.
- This rule overrides everything else in this prompt. The rules below are written in German for specification purposes only — they do NOT dictate the output language.

REGOLA CRITICA: Se il trascritto è in italiano, l'output DEVE essere in italiano.
RÈGLE CRITIQUE: Si la transcription est en français, la sortie DOIT être en français.

Only these elements are language-independent: JSON keys (subjective, objective, assessment, plan), medication names, units (mmHg, °C, mg, kg).
Swiss-German transcripts → Swiss Standard German output (ss not ß).

---

## Task

Analyze the transcript and generate a medical SOAP note in the LANGUAGE OF THE TRANSCRIPT. Correct phonetic ASR transcription errors in medication names using the reference list.

## Regulatory Boundary (MDR Safety)

This system is a PURE EXTRACTION TOOL. NO diagnostic inference.
- CORRECT: Use the doctor's exact wording (e.g., "situazione gravissima", "Blutdruck zu hoch")
- WRONG: Infer medical terminology not spoken (e.g., "ARDS" when doctor said "polmoni molto compromessi")
- CORRECT: Document current medication (e.g., "Attuale: Metformin 1000mg 1-0-1" / "Bisher: Metformin 1000mg 1-0-1")
- WRONG: Infer diagnoses from medications
- Use "V.a." / "susp." / "sospetto di" only when the doctor explicitly expresses suspicion.

## Section Definitions

### subjective
Patient-reported information: chief complaint, symptom history/duration, current medications BEFORE today, medications by OTHER doctors, stopped medications, relevant medical history, social/family history.

Language-specific medication prefix:
- DE: "Bisher: [Name] [Dosis]"
- FR: "Actuel: [Name] [Dose]"
- IT: "Attuale: [Name] [Dose]"
- EN: "Current: [Name] [Dose]"

### objective
Measured/observed data: vital signs ("RR [sys]/[dia] mmHg"), lab values, physical exam findings, weight.

### assessment
Confirmed diagnoses, suspected diagnoses, stable conditions, relevant differentials. ONLY what the doctor explicitly states — no own conclusions.

### plan
ONLY decisions by THIS doctor in THIS consultation:

| Action | DE | FR | IT | EN |
|--------|----|----|----|----|
| New | neu | nouveau | nuovo | new |
| Changed | (Änderung: ...) | (modification: ...) | (modifica: ...) | (change: ...) |
| Stopped | absetzen | arrêter | sospendere | stop |
| Continued | weiter | continuer | continuare | continue |
| Not indicated | nicht indiziert | non indiqué | non indicato | not indicated |
| Follow-up | Kontrolle in ... | Contrôle dans ... | Controllo tra ... | Follow-up in ... |

Instructions in plan: always use infinitive form.
- DE: "Viel Wasser trinken" (NOT "Trinken Sie...")
- FR: "Boire beaucoup d'eau" (NOT "Buvez...")
- IT: "Bere molta acqua" (NOT "Beva...")
- EN: "Drink plenty of water" (NOT "You should drink...")

## Dosage Schema Rules

Schema "X-Y-Z" = [Morning]-[Noon]-[Evening], number = UNITS per time point.

RULE 1 — Daily intake:
- 1x daily morning → 1-0-0
- 1x daily evening → 0-0-1
- 2x daily → 1-0-1 (NOT 2-0-2! "2x daily" = 2 time points, not 2 units)
- 3x daily → 1-1-1
- 2x daily 2 tablets each → 2-0-2

RULE 2 — Non-daily intake → NO X-Y-Z, use free text:
- 3x/week → "[Name] [Dose] 3x/Woche" / "3x/semaine" / "3x/settimana" / "3x/week"
- As needed → "[Name] [Dose] b.B." / "si néc." / "al bisogno" / "PRN"
- WRONG: "Isotretinoin 10mg 4-0-0" (= 4 tablets every morning!)
- CORRECT: "Isotretinoin 10mg 4x/Woche" / "4x/settimana"

RULE 3 — Topicals: "apply 2x daily" → 1-0-1

RULE 4 — Dose changes: state old → new

RULE 5 — Frequency verification: later physician summary/instruction takes precedence over earlier contradictory statements.

RULE 6 — Range dosing ("20 to 30 drops" / "20 a 30 gocce"): use free text.

## Swiss Medication Reference List

Analgetika: Dafalgan, Paracetamol, Ibuprofen, Irfen, Diclofenac, Voltaren, Olfen, Mefenacid, Ponstan, Novalgin, Metamizol, Tramal, Tramadol, Morphin, Oxycontin, Oxynorm, Palexia, Targin, Arcoxia, Celebrex
Antihypertensiva: Ramipril, Lisinopril, Enalapril, Perindopril, Coversum, Amlodipin, Candesartan, Atacand, Losartan, Valsartan, Diovan, Co-Diovan, Exforge, Metoprolol, Bisoprolol, Concor, Nebilet, Carvedilol
Antikoagulation: Xarelto, Eliquis, Lixiana, Pradaxa, Marcoumar, Aspirin Cardio, Plavix, Brilique
Diabetes: Metformin, Glucophage, Jardiance, Forxiga, Januvia, Galvus, Ozempic, Trulicity, Victoza, Lantus, Levemir, Tresiba, NovoRapid, Humalog, Gliclazid
Lipidsenker: Atorvastatin, Sortis, Rosuvastatin, Crestor, Pravastatin, Simvastatin, Ezetimib
Magen/Darm: Pantoprazol, Pantozol, Esomeprazol, Nexium, Omeprazol, Antramups, Movicol, Metamucil, Buscopan
Psychopharmaka: Sertralin, Zoloft, Cipralex, Escitalopram, Citalopram, Mirtazapin, Remeron, Venlafaxin, Efexor, Duloxetin, Cymbalta, Quetiapin, Seroquel, Temesta, Stilnox, Trittico, Ritalin
Antibiotika: Amoxicillin, Co-Amoxicillin, Augmentin, Ciproxin, Klacid, Azithromycin, Bactrim, Nitrofurantoin, Doxycyclin
Atemwege: Symbicort, Relvar, Seretide, Foster, Ventolin, Salbutamol, Spiriva, Singulair, ACC, Fluimucil
Schilddrüse: Euthyrox, Eltroxin, Levothyroxin, Carbimazol
Diuretika: Torasemid, Torem, Furosemid, Lasix, Hydrochlorothiazid, Spironolacton
Neurologie: Lyrica, Gabapentin, Rivotril, Keppra, Tegretol, Topamax, Lamictal
Rheumatologie/Gicht: Allopurinol, Colchicin, Febuxostat
Urologie: Tamsulosin, Finasterid, Vesikur, Betmiga
Supplemente: Ferro-Gradumet, Calcium-Sandoz, Vitamin D3, Magnesiocard
Kortikosteroide: Prednison, Spiricort, Dexamethason
Antihistaminika: Xyzal, Cetirizin, Zyrtec, Aerius, Nasonex, Avamys
Dermatologie: Isotretinoin, Skinoren, Rosalox, Perilox, Dalacin-T, Differin, Epiduo
Andere: Alendronat, Fosamax, Tamoxifen, Letrozol, MTX, Chondrosulf

## ASR Correction

Transcripts contain phonetic ASR errors. Patterns vary by language:
- German/Swiss-German: b↔p, d↔t, g↔k ("Ramibril"→Ramipril, "Tafalgan"→Dafalgan), vowel shifts, missing/extra syllables, "Markumar"→Marcoumar, "Koncor"→Concor.
- French: nasal vowels shifted, endings swallowed ("Ramipri"→Ramipril), "Métoprolole"→Metoprolol.
- Italian: doubled/dropped consonants, vowel endings swapped ("Ramipril-e"→Ramipril), "Metformina"→Metformin.
Rules: Only correct at high confidence (≥90%). Use context (indication, dose) to disambiguate. No generic names in brackets. No hallucinated dosages. If uncertain: keep original text.

## Formatting

Telegram style, no full sentences. No forms of address. Empty sections: omit entirely.
Swiss German output: no ß, always ss.
No ASR artifacts in output — drop or correct unintelligible/garbled words.

## Style Rules (CRITICAL)

Every entry must be telegram style. No full sentences, no doctor questions, no filler words.

Rules:
- Do NOT include doctor questions — only patient statements and doctor decisions
- Current medications MUST have the prefix ("Bisher:" / "Actuel:" / "Attuale:" / "Current:")
- objective = ONLY today's examination findings (what doctor sees/measures TODAY), NOT history
- plan = ONLY concrete actions (medication, referral, follow-up), NO explanations, NO deliberations
- Advice in plan ALWAYS in infinitive form

## Duplicate Rules (CRITICAL)

Each piece of information may appear ONLY ONCE in the entire output:
- Patient reports something → ONLY in subjective
- Doctor sees/measures something → ONLY in objective
- Diagnosis/evaluation → ONLY in assessment
- Medication in plan → ONLY 1 entry per medication (dose + schema in ONE string)

## Decision Logic: What Goes Where?

### Medications — Flow:
1. Patient already taking → subjective ("Bisher:"/"Actuel:"/"Attuale:"/"Current:")
2. Doctor: continue → plan ("[Name] [Dose] weiter/continuer/continuare/continue")
3. Doctor: change → plan ("[Name] [new dose] [schema] (Änderung/modification/modifica/change: ...)")
4. Doctor: stop → plan ("[Name] absetzen/arrêter/sospendere/stop ([reason])")
5. Doctor: new prescription → plan ("[Name] [Dose] [Schema] neu/nouveau/nuovo/new")
6. Doctor: rejects request → plan ("[Name] nicht indiziert/non indiqué/non indicato/not indicated ([reason])")

### Chart findings vs. today's measurement:
- Doctor quotes from chart → subjective (history)
- Doctor measures today → objective
- Doctor evaluates → assessment

### Transition instructions:
- ONE entry in plan with all details combined

## Additional Rules

1. Only extract explicit information — no assumptions
2. Lab results → always objective
3. Home BP → objective
4. Past history (St.n. / état après / stato post) → subjective or assessment
5. NO "Explanation:" entries in plan — plan contains only actions
6. Blood pressure: "160 su 90" / "160 zu 90" / "160 sur 90" → "RR 160/90 mmHg"
7. Stopped medications with disposal note → plan (ONE entry)

## Examples

Example 1 — Deutsch:
Arzt: Husten? Patient: Seit einer Woche, Fieber. Nehme Ramibril 5mg. Arzt: RR 145/92, Temp 37.8. Bronchitis. ACC 600 1x täglich. Ramipril weiter. Kontrolle 1 Woche.
```json
{"subjective":["Husten seit 1 Woche","Fieber","Bisher: Ramipril 5mg"],"objective":["RR 145/92 mmHg","Temp 37.8°C"],"assessment":["Bronchitis"],"plan":["ACC 600mg 1-0-0 neu","Ramipril 5mg weiter","Kontrolle in 1 Woche"]}
```

Example 2 — Français:
Médecin: La toux? Patient: Depuis une semaine, fièvre. Je prends du Ramipril 5mg. Médecin: TA 145/92, Temp 37.8. Bronchite. ACC 600 1x par jour. Ramipril continuer. Contrôle 1 semaine.
```json
{"subjective":["Toux depuis 1 semaine","Fièvre","Actuel: Ramipril 5mg"],"objective":["RR 145/92 mmHg","Temp 37.8°C"],"assessment":["Bronchite"],"plan":["ACC 600mg 1-0-0 nouveau","Ramipril 5mg continuer","Contrôle dans 1 semaine"]}
```

Example 3 — Italiano:
Medico: La tosse? Paziente: Da una settimana, febbre. Prendo Ramipril 5mg. Medico: PA 145/92, Temp 37.8. Bronchite. ACC 600 1x al giorno. Ramipril continuare. Controllo 1 settimana.
```json
{"subjective":["Tosse da 1 settimana","Febbre","Attuale: Ramipril 5mg"],"objective":["RR 145/92 mmHg","Temp 37.8°C"],"assessment":["Bronchite"],"plan":["ACC 600mg 1-0-0 nuovo","Ramipril 5mg continuare","Controllo tra 1 settimana"]}
```

Example 4 — Deutsch: Dosisänderung + Bedarfsmedikation + abgelehntes Medikament:
Arzt: Metformin 1000, 1-0-1? Patient: Ja. Frau empfiehlt Ozempic. Arzt: Ozempic nein, Werte nicht schlecht genug. Ramipril 5mg morgens? Patient: Ja. Arzt: RR 160/90, zu hoch. Verdoppeln auf 10mg. Novalgin Tropfen 20-30 Trpf. bei Bedarf, max. 4x/d. Tramadol zu Hause? Bitte entsorgen. GFR unter 60 in der Akte. Labor. Mallorca nächste Woche? Keine Thrombosespritze nötig.
```json
{"subjective":["Rückenschmerzen","Blutzucker unregelmässig","Bisher: Metformin 1000mg 1-0-1","Bisher: Ramipril 5mg 1-0-0","Tramadol-Tropfen zu Hause vorhanden","Anamnese: GFR unter 60","Flug nach Mallorca nächste Woche"],"objective":["RR 160/90 mmHg"],"assessment":["Blutdruck deutlich zu hoch"],"plan":["Metformin 1000mg 1-0-1 weiter","Ramipril 10mg 1-0-0 (Änderung: von 5mg auf 10mg, Übergang: 2x 5mg bis Packung leer)","Novalgin Tropfen (Metamizol) 20-30 Trpf. b.B. max. 4x/d neu","Tramadol absetzen, entsorgen","Ozempic nicht indiziert (Werte nicht schlecht genug, Lieferengpässe)","Laboruntersuchung","Mallorca-Flug: keine Thrombosespritze nötig","Viel Wasser trinken, Beine bewegen (Flug)"]}
```"""


class SoapRequest(BaseModel):
    text: str
    agent_id: str = ""  # RAGFlow agent ID; empty = LLM fallback


@fastapi_app.get("/v1/ragflow-agents")
async def list_ragflow_agents():
    """Return the list of configured RAGFlow agents for the frontend."""
    return {
        "agents": RAGFLOW_AGENTS,
        "enabled": bool(RAGFLOW_BASE_URL and RAGFLOW_API_KEY and RAGFLOW_AGENTS),
    }


@fastapi_app.get("/v1/ragflow-health")
async def ragflow_health():
    """Probe RAGFlow availability."""
    if not RAGFLOW_BASE_URL:
        return {"ok": False, "reason": "RAGFLOW_BASE_URL not configured"}
    if not http_session:
        return {"ok": False, "reason": "Server still starting up"}

    # Try a lightweight GET on the RAGFlow root — any 2xx/3xx means it's up
    try:
        async with http_session.get(
            RAGFLOW_BASE_URL.rstrip('/'),
            timeout=aiohttp.ClientTimeout(total=5),
            allow_redirects=True,
        ) as resp:
            if resp.status < 500:
                return {"ok": True}
            return {"ok": False, "reason": f"HTTP {resp.status}"}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


@fastapi_app.post("/v1/test-ragflow")
async def test_ragflow(req: SoapRequest):
    """Debug endpoint: call RAGFlow agent and return the raw response."""
    if not RAGFLOW_BASE_URL or not RAGFLOW_API_KEY:
        return {"error": "RAGFLOW_BASE_URL or RAGFLOW_API_KEY not set"}

    agent_id = req.agent_id or "none"
    url = f"{RAGFLOW_BASE_URL.rstrip('/')}/api/v1/agents_openai/{agent_id}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {RAGFLOW_API_KEY}",
    }
    payload = {
        "model": LLM_MODEL,
        "messages": [{"role": "user", "content": req.text.strip() or "Test"}],
    }
    try:
        async with http_session.post(
            url, json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            body = await resp.text()
            return {
                "status": resp.status,
                "url": url,
                "raw_body": body[:2000],
            }
    except Exception as e:
        return {"error": str(e), "url": url}


@fastapi_app.post("/v1/generate-soap")
async def generate_soap(req: SoapRequest):
    """Generate SOAP notes from a dictation transcript.

    If agent_id is provided and RAGFlow is configured, calls that agent.
    Otherwise falls back to direct LLM call.
    """
    text = req.text.strip()
    if not text:
        return {"error": "Kein Text vorhanden", "status": 400}

    soap = None
    source = ""
    t0 = time.perf_counter()

    # --- Strategy 1: RAGFlow agent ---
    ragflow_error = ""
    if req.agent_id and RAGFLOW_BASE_URL and RAGFLOW_API_KEY:
        try:
            soap = await _generate_soap_ragflow(text, req.agent_id)
            if soap:
                source = "ragflow"
        except Exception as e:
            ragflow_error = str(e)
            log.warning("RAGFlow SOAP generation failed (agent %s), falling back to LLM: %s", req.agent_id, e)

    # --- Strategy 2: Direct LLM (Mistral) ---
    if not soap:
        try:
            soap = await _generate_soap_llm(text)
            if soap:
                source = "llm"
        except Exception as e:
            log.error("LLM SOAP generation failed: %s", e)

    duration_ms = round((time.perf_counter() - t0) * 1000)

    if not soap:
        detail = ragflow_error or "Keine Antwort von RAGFlow oder LLM"
        return {"error": f"SOAP-Generierung fehlgeschlagen: {detail}", "status": 500}

    log.info("SOAP generated via %s in %d ms", source, duration_ms)

    # --- Score the generated SOAP against the transcript ---
    scores = await _score_soap(text, soap)

    return {"status": "ok", "source": source, "duration_ms": duration_ms, **soap, "scores": scores}


async def _generate_soap_ragflow(text: str, agent_id: str) -> dict | None:
    """Call a RAGFlow agent (OpenAI-compatible endpoint) to produce SOAP fields."""
    if not http_session:
        raise RuntimeError("HTTP session not initialized")

    url = f"{RAGFLOW_BASE_URL.rstrip('/')}/api/v1/agents_openai/{agent_id}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {RAGFLOW_API_KEY}",
    }
    # Match the working RAGAS project payload exactly:
    # - model must be a model name (not the agent UUID — agent is in the URL)
    # - do NOT send "stream" — RAGFlow agent endpoint doesn't support it
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "user", "content": text},
        ],
    }

    log.info("Calling RAGFlow agent %s at %s", agent_id, url)

    async with http_session.post(
        url, json=payload, headers=headers,
        timeout=aiohttp.ClientTimeout(total=120),
    ) as resp:
        body = await resp.text()
        log.info("RAGFlow HTTP %d, body length %d: %s", resp.status, len(body), body[:500])
        if resp.status != 200:
            raise RuntimeError(f"RAGFlow HTTP {resp.status}: {body[:300]}")

        result = json.loads(body)
        # OpenAI-compatible response: choices[0].message.content
        choices = result.get("choices", [])
        if not choices:
            raise RuntimeError(f"RAGFlow returned empty choices: {body[:300]}")

        answer = choices[0].get("message", {}).get("content", "")
        if not answer:
            raise RuntimeError(f"RAGFlow returned empty content: {body[:300]}")

        log.info("RAGFlow raw response (agent %s): %s", agent_id, answer[:500])

        parsed = _parse_soap_json(answer)
        if parsed:
            return parsed

        # Try splitting free text by SOAP section headers
        split = _parse_soap_text(answer)
        if split:
            return split

        # Last resort — put everything in subjective so it's visible
        log.info("RAGFlow response is not JSON and has no SOAP headers, returning as raw text")
        return {"subjective": answer, "objective": "", "assessment": "", "plan": "", "raw_agent_output": True}


def _build_refinement_context(soap_fields: dict, dirty_fields: set) -> str:
    """Build a refinement context block for the LLM prompt.

    Sections in dirty_fields are marked LOCKED (doctor confirmed).
    Other sections are marked AUTO (LLM should update freely).
    Returns empty string if no fields have content yet.
    """
    has_content = any(v.strip() for v in soap_fields.values())
    if not has_content:
        return ""

    lines = [
        "## REFINEMENT CONTEXT",
        "Sections marked AUTO: update freely with new transcript content.",
        "Sections marked LOCKED: confirmed by the physician — preserve exactly, do not modify.",
        "",
    ]
    for field in ("subjective", "objective", "assessment", "plan"):
        status = "LOCKED" if field in dirty_fields else "AUTO"
        content = soap_fields.get(field, "").strip() or "(empty)"
        lines.append(f"### {field} [{status}]")
        lines.append(content)
        lines.append("")
    return "\n".join(lines)


async def _generate_soap_llm_with_context(
    text: str,
    soap_fields: dict,
    dirty_fields: set,
) -> dict | None:
    """Generate SOAP with refinement context — used for live SOAP updates."""
    context_block = _build_refinement_context(soap_fields, dirty_fields)
    prompt = (SOAP_GENERATION_PROMPT + "\n\n" + context_block) if context_block else SOAP_GENERATION_PROMPT
    result = await _call_llm(prompt, text, http_session, timeout=30.0, label="LLM-SOAP", max_tokens=2048)
    if not result:
        return None
    return _parse_soap_json(result)


async def _generate_soap_llm(text: str) -> dict | None:
    """Direct Mistral call for SOAP structuring — used by the manual generate button (no context)."""
    result = await _call_llm(
        SOAP_GENERATION_PROMPT, text, http_session,
        timeout=30.0, label="LLM-SOAP", max_tokens=2048,
    )
    if not result:
        return None

    return _parse_soap_json(result)


def _parse_soap_text(text: str) -> dict | None:
    """Split free-text SOAP output by common section headers.

    Recognises patterns like:
      **S:** ...  /  S: ...  /  **Subjektiv:** ...  /  ## S - Subjektiv  etc.
    """
    # Normalised header patterns (case-insensitive)
    patterns = [
        # "S:", "**S:**", "## S", "S -", "S.", "Subjektiv:", etc.
        (r'(?:^|\n)\s*(?:\*{1,2})?(?:S|Subjektiv|Subjective)(?:\*{1,2})?\s*[:\-\.]\s*', "subjective"),
        (r'(?:^|\n)\s*(?:\*{1,2})?(?:O|Objektiv|Objective)(?:\*{1,2})?\s*[:\-\.]\s*', "objective"),
        (r'(?:^|\n)\s*(?:\*{1,2})?(?:A|Assessment|Beurteilung)(?:\*{1,2})?\s*[:\-\.]\s*', "assessment"),
        (r'(?:^|\n)\s*(?:\*{1,2})?(?:P|Plan|Procedere)(?:\*{1,2})?\s*[:\-\.]\s*', "plan"),
    ]

    # Find each section's start position
    positions: list[tuple[int, str]] = []
    for pattern, field in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            positions.append((m.end(), field))

    if len(positions) < 2:
        # Need at least 2 sections to consider it a valid SOAP split
        return None

    # Sort by position in text
    positions.sort(key=lambda x: x[0])

    # Extract content between sections
    result = {"subjective": "", "objective": "", "assessment": "", "plan": ""}
    for i, (start, field) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(text)
        # Walk back from end to find the start of the next header
        if i + 1 < len(positions):
            next_pattern, _ = [(p, f) for p, f in patterns if f == positions[i + 1][1]][0]
            m = re.search(next_pattern, text[start:], re.IGNORECASE)
            if m:
                end = start + m.start()
        content = text[start:end].strip()
        # Remove trailing markdown bold/headers residue
        content = re.sub(r'\n\s*(?:\*{1,2})?(?:S|O|A|P|Subjektiv|Objektiv|Assessment|Beurteilung|Plan|Procedere|Subjective|Objective)(?:\*{1,2})?\s*[:\-\.]\s*$', '', content, flags=re.IGNORECASE)
        result[field] = content.strip()

    log.info("Parsed SOAP from free text: S=%d chars, O=%d chars, A=%d chars, P=%d chars",
             len(result["subjective"]), len(result["objective"]), len(result["assessment"]), len(result["plan"]))
    return result


def _build_soap_from_medical_facts(data: dict) -> dict:
    """Convert Medical Facts Agent JSON to SOAP fields.

    Handles both flat structure (chief_complaint, symptoms, …) and
    nested structure (clinical:{…}, medications:{…}, measurements:{…}).
    """
    # Flatten nested structure if present
    if "clinical" in data or "medications" in data or "measurements" in data:
        flat: dict = {}
        flat.update(data.get("clinical", {}))
        flat.update(data.get("medications", {}))
        flat.update(data.get("measurements", {}))
        flat.update(data.get("context", {}))
        data = flat

    def _join(val) -> str:
        if not val:
            return ""
        if isinstance(val, list):
            return "\n".join(f"- {v}" for v in val if v)
        return str(val)

    s_parts = []
    if data.get("chief_complaint"):
        s_parts.append(f"Hauptbeschwerde: {_join(data['chief_complaint'])}")
    if data.get("symptoms"):
        s_parts.append(f"Symptome: {_join(data['symptoms'])}")
    if data.get("patient_measurements"):
        s_parts.append(f"Angaben des Patienten: {_join(data['patient_measurements'])}")
    if data.get("medical_history"):
        s_parts.append(f"Anamnese: {_join(data['medical_history'])}")
    if data.get("patient_education"):
        s_parts.append(f"Patientenaufklärung: {_join(data['patient_education'])}")

    o_parts = []
    if data.get("vital_measurements"):
        o_parts.append(f"Vitalzeichen: {_join(data['vital_measurements'])}")
    if data.get("physical_examination"):
        o_parts.append(f"Untersuchungsbefund: {_join(data['physical_examination'])}")
    if data.get("medications_taken"):
        o_parts.append(f"Aktuelle Medikation: {_join(data['medications_taken'])}")

    a_parts = []
    if data.get("diagnostic_hypotheses"):
        a_parts.append(_join(data["diagnostic_hypotheses"]))
    elif data.get("symptoms"):
        a_parts.append(f"Symptome ohne Diagnose: {_join(data['symptoms'])}")

    p_parts = []
    if data.get("medications_planned"):
        p_parts.append(f"Medikation: {_join(data['medications_planned'])}")
    if data.get("diagnostic_plans"):
        p_parts.append(f"Diagnostik: {_join(data['diagnostic_plans'])}")
    if data.get("therapeutic_interventions"):
        p_parts.append(f"Therapie: {_join(data['therapeutic_interventions'])}")
    if data.get("follow_up_instructions"):
        p_parts.append(f"Verlaufskontrolle: {_join(data['follow_up_instructions'])}")

    return {
        "subjective": "\n".join(s_parts),
        "objective":  "\n".join(o_parts),
        "assessment": "\n".join(a_parts),
        "plan":       "\n".join(p_parts),
    }


def _soap_value_to_str(val) -> str:
    """Convert a SOAP field value to string. Handles lists and strings."""
    if isinstance(val, list):
        return "\n".join(str(item) for item in val)
    return str(val) if val else ""


def _parse_soap_json(raw: str) -> dict | None:
    """Extract SOAP fields from a JSON string, tolerating markdown fences.

    Handles two response formats:
    - Direct SOAP: {"S": "...", "O": "...", "A": "...", "P": "..."}
    - Medical Facts: {"chief_complaint": ..., "symptoms": ..., ...}
    """
    # Strip markdown code fences if present
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]  # remove ```json line
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()
    # Handle leftover language identifier (e.g. "json\n{...}" after _call_llm strips outer ```)
    if cleaned.startswith("json"):
        cleaned = cleaned[4:].strip()
    # Find the first { and last } to extract the JSON object
    brace_start = cleaned.find("{")
    brace_end = cleaned.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        cleaned = cleaned[brace_start:brace_end + 1]

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        log.warning("Could not parse SOAP JSON: %s", cleaned[:200])
        return None

    # Direct SOAP format — short keys (S/O/A/P)
    if any(k in data for k in ("S", "O", "A", "P")):
        return {
            "subjective": _soap_value_to_str(data.get("S", "")),
            "objective":  _soap_value_to_str(data.get("O", "")),
            "assessment": _soap_value_to_str(data.get("A", "")),
            "plan":       _soap_value_to_str(data.get("P", "")),
        }

    # Long keys (subjective/objective/assessment/plan)
    if any(k in data for k in ("subjective", "objective", "assessment", "plan")):
        return {
            "subjective": _soap_value_to_str(data.get("subjective", "")),
            "objective":  _soap_value_to_str(data.get("objective", "")),
            "assessment": _soap_value_to_str(data.get("assessment", "")),
            "plan":       _soap_value_to_str(data.get("plan", "")),
        }

    # Medical Facts format (flat or nested)
    medical_fact_keys = {
        "chief_complaint", "symptoms", "medications_taken", "medications_planned",
        "vital_measurements", "physical_examination", "diagnostic_hypotheses",
        "diagnostic_plans", "therapeutic_interventions", "follow_up_instructions",
        "clinical", "medications", "measurements",
    }
    if medical_fact_keys & set(data.keys()):
        return _build_soap_from_medical_facts(data)

    log.warning("Unrecognised SOAP JSON structure, keys: %s", list(data.keys())[:10])
    return None


# ---------------------------------------------------------------------------
# Gold Standard Save — writes test case + gold SOAP to RAGAS project
# ---------------------------------------------------------------------------

class GoldStandardRequest(BaseModel):
    name: str  # Human-readable test case name
    transcript: str
    S: str = ""
    O: str = ""
    A: str = ""
    P: str = ""


@fastapi_app.post("/v1/save-gold-standard")
async def save_gold_standard(req: GoldStandardRequest):
    """Save the doctor's final SOAP as a gold standard test case.

    Writes two files to the RAGAS project:
    - test_cases/{slug}.json  — transcript + metadata
    - gold_soap/{slug}.soap.json — the doctor-reviewed SOAP sections
    """
    if not RAGAS_PROJECT_DIR:
        return {"error": "RAGAS_PROJECT_DIR nicht konfiguriert", "status": 400}

    base = Path(RAGAS_PROJECT_DIR)
    tc_dir = base / "medical_facts_evaluation" / "test_cases"
    gs_dir = base / "medical_facts_evaluation" / "gold_soap"

    if not tc_dir.is_dir() or not gs_dir.is_dir():
        return {"error": f"RAGAS-Verzeichnisse nicht gefunden: {base}", "status": 400}

    # Slugify name for filename
    name = req.name.strip()
    if not name:
        return {"error": "Name darf nicht leer sein", "status": 400}

    slug = unicodedata.normalize("NFKD", name.lower())
    slug = re.sub(r"[^a-z0-9]+", "_", slug).strip("_")
    if not slug:
        return {"error": "Ungültiger Name", "status": 400}

    # Prevent overwriting existing test cases
    tc_path = tc_dir / f"{slug}.json"
    gs_path = gs_dir / f"{slug}.soap.json"
    if tc_path.exists() or gs_path.exists():
        return {"error": f"Testfall '{slug}' existiert bereits", "status": 409}

    # Build SOAP sections as lists (split on newlines/bullets)
    def _to_list(text: str) -> list[str]:
        items = []
        for line in text.strip().splitlines():
            line = line.strip().lstrip("- ").strip()
            if line:
                items.append(line)
        return items

    soap_sections = {
        "S": _to_list(req.S),
        "O": _to_list(req.O),
        "A": _to_list(req.A),
        "P": _to_list(req.P),
    }

    soap_text = "\n\n".join(
        f"{sec}:\n" + "\n".join(f"- {item}" for item in items)
        for sec, items in soap_sections.items()
        if items
    )

    now = datetime.now(timezone.utc).isoformat()

    # Test case JSON (transcript only — ground_truth can be added in Streamlit)
    test_case = {
        "test_id": f"{slug}_001",
        "name": name,
        "description": "Goldstandard aus Live-Diktat (vitodata strmcp)",
        "language": "de",
        "transcript": req.transcript.strip(),
        "ground_truth": {},
    }

    # Gold SOAP JSON (matching RAGAS project format)
    gold_soap = {
        "metadata": {
            "generated_by": "vitodata-strmcp",
            "timestamp": now,
            "status": "doctor-reviewed",
        },
        "soap_text": soap_text,
        "soap": soap_sections,
        "provenance": {
            "status": "doctor-reviewed",
            "source": "vitodata-strmcp",
            "reviewed_at": now,
        },
    }

    # Write files
    try:
        tc_path.write_text(json.dumps(test_case, ensure_ascii=False, indent=2), encoding="utf-8")
        gs_path.write_text(json.dumps(gold_soap, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Gold standard saved: %s", slug)
        return {"status": "ok", "slug": slug, "test_case_path": str(tc_path), "gold_soap_path": str(gs_path)}
    except Exception as e:
        log.error("Failed to save gold standard: %s", e)
        return {"error": str(e), "status": 500}


# ---------------------------------------------------------------------------
# Mount Socket.IO on the FastAPI app
# ---------------------------------------------------------------------------

app = socketio.ASGIApp(sio, fastapi_app)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8003, log_level="info")
