# Transcript Deduplication & Overlap-Aware LLM Correction

**Date:** 2026-03-31
**Status:** Ready for implementation

---

## Problem

The sliding window produces repetition because:
1. Each 12s window overlaps the previous by 10.5s
2. The LLM rewrites words in the overlap region
3. Word-level overlap detection in `_accumulate_full_text` fails when words differ → appends full window → repetition

---

## Design Decisions (agreed)

| Decision | Choice |
|----------|--------|
| Fix layer | Both — audio layer (what LLM receives) + text layer (accumulation) |
| Step interval | **2s** (up from 1.5s) |
| Whisper timestamps | **Enabled** — `timestamp_granularities=["word"]` |
| LLM input | full_text + prev window + overlap comparison + curr window + new 2s slice |
| LLM output | New content only + optional correction diffs |
| Overlap errors | **B** — LLM can flag corrections to committed text |
| Correction display | Highlighted in transcript, **toggle button** to show/hide |
| Click correction | **Single click** = revert to original ASR · **Double-click** = inline edit |
| Provisional text | Last 3 words of new output shown in amber (unchanged) |

---

## Architecture

### What changes

```
Before:
  Whisper(12s window) → raw_text
  LLM(confirmed, last_transcription, raw_text) → corrected_full_window
  _accumulate_full_text(confirmed) → tries overlap detection → often fails

After:
  Whisper(12s window, timestamps=word) → {text, words:[{word,start,end}]}
  LLM(full_text, prev_words, curr_words, step=2s, window=12s) → {new, corrections}
  _accumulate_full_text(new) → trivial append, no overlap detection needed
```

### New data flow

```
Doctor speaks
  → audio buffer grows by 2s (STEP_INTERVAL_S = 2.0)
  → call_whisper(12s window, timestamps=True)
      → returns {text: str, words: [{word, start, end}, ...]}
  → extract regions using timestamps:
      overlap_words_prev = session.prev_words where start >= 2s  (last 10s of prev window)
      overlap_words_curr = curr_words where start < 10s          (first 10s of curr window)
      new_words_curr     = curr_words where start >= 10s         (last 2s of curr window)
  → correct_window_with_llm(
        full_text       = session.full_text,
        prev_text       = session.prev_raw_text,
        overlap_prev    = words_to_text(overlap_words_prev),
        overlap_curr    = words_to_text(overlap_words_curr),
        new_slice       = words_to_text(new_words_curr),
        curr_text       = curr_raw_text,
    )
      → returns {"new": "...", "corrections": [{"from": "...", "to": "..."}]}
  → apply corrections to session.full_text + emit transcript_correction events
  → _accumulate_full_text(new_text)  ← trivial: just append
  → session.prev_words = curr_words
  → session.prev_raw_text = curr_raw_text
  → compute_stable_text(new_text) → confirmed + provisional (last 3 words)
  → emit transcription event
```

---

## Backend changes (`server.py`)

### 1. Constants

```python
STEP_INTERVAL_S = 2.0    # was 1.5
```

### 2. `call_whisper` — enable word timestamps

```python
form.add_field("timestamp_granularities[]", "word")
form.add_field("response_format", "verbose_json")
```

Response parsing:
```python
data = await resp.json()
text  = data.get("text", "")
words = data.get("words", [])   # [{word, start, end}, ...]
return text, words
```

Return type changes from `str` → `tuple[str, list[dict]]`.

### 3. `AudioSession` — new fields

```python
prev_raw_text: str = ""
prev_words: list = field(default_factory=list)
pending_corrections: list = field(default_factory=list)
```

`pending_corrections` entries:
```python
{"id": str, "original": str, "corrected": str, "offset": int}
# offset = char position in full_text where correction was applied
```

### 4. `correct_window_with_llm` — new signature + prompt

```python
async def correct_window_with_llm(
    full_text: str,
    prev_raw_text: str,
    overlap_prev: str,
    overlap_curr: str,
    new_slice: str,
    curr_raw_text: str,
    http_session,
    model: str = LLM_MODEL,
) -> dict:
    """Returns {"new": str, "corrections": list[dict]}"""
```

New system prompt (`CORRECTION_PROMPT_V2`):

```
Du bist ein medizinischer ASR-Korrektor. Deine Aufgabe besteht aus zwei Teilen:

## TEIL 1 — NEUE INHALTE
Schreibe NUR den neuen Inhalt, der nach dem COMMITTED TEXT kommt.
- Starte unmittelbar nach dem Ende des committed text
- Wiederhole NICHTS aus dem committed text
- Korrigiere phonetische ASR-Fehler im neuen Inhalt
- Verwende Schweizer Orthographie (ss statt ß)
- Behalte Telegrammstil bei

## TEIL 2 — KORREKTUREN (optional)
Wenn der ÜBERLAPPUNGSVERGLEICH zeigt dass beide Whisper-Läufe dieselbe Audiostelle
verschieden transkribiert haben UND der medizinische Kontext eindeutig eine Version
bestätigt, gib max. 2 Korrekturen an.

Nur ausgeben wenn hochsicher. Niemals Diagnosen oder Medikamente erfinden.
Niemals Markennamen ↔ Generika tauschen.

## AUSGABEFORMAT (JSON)
{
  "new": "neuer text hier",
  "corrections": [
    {"from": "falsches wort", "to": "richtiges wort"}
  ]
}
corrections darf leer sein: []
```

User message structure:

```
## COMMITTED TEXT (Kontext — nicht wiederholen)
{full_text}

## VORHERIGES WHISPER-FENSTER (12s, Lauf N-1)
{prev_raw_text}

## AKTUELLES WHISPER-FENSTER (12s, Lauf N)
{curr_raw_text}

## ÜBERLAPPUNGSVERGLEICH (dieselbe ~10s-Audiostelle, zweimal transkribiert)
Lauf N-1: {overlap_prev}
Lauf N:   {overlap_curr}

## NEUER INHALT (~2s, nur in aktuellem Fenster)
{new_slice}
```

### 5. `_accumulate_full_text` — simplified

```python
def _accumulate_full_text(session: AudioSession, new_text: str) -> None:
    """Trivial append — LLM guarantees new_text contains only unseen content."""
    if not new_text.strip():
        return
    if session.full_text:
        session.full_text += " " + new_text
    else:
        session.full_text = new_text
```

No overlap detection. `_prev_confirmed` and the existing O(n²) suffix search are removed.

### 6. `apply_corrections` — new helper

```python
def apply_corrections(
    full_text: str,
    corrections: list[dict],
) -> tuple[str, list[dict]]:
    """Apply LLM correction diffs to full_text.

    Returns (updated_full_text, applied_corrections_with_offsets).
    Only applies corrections where the 'from' string is found (case-insensitive).
    At most one occurrence replaced per correction (last occurrence — most recent = more likely wrong).
    """
    applied = []
    for c in corrections[:2]:   # cap at 2 per call
        original = c.get("from", "").strip()
        corrected = c.get("to", "").strip()
        if not original or not corrected or original == corrected:
            continue
        # Find last occurrence (most recently spoken = closest to error)
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
```

### 7. Transcription loop changes

After LLM returns `{"new": ..., "corrections": [...]}`:

```python
result = await correct_window_with_llm(...)
new_text = result.get("new", "")
raw_corrections = result.get("corrections", [])

# Apply corrections to committed text
if raw_corrections:
    session.full_text, applied = apply_corrections(session.full_text, raw_corrections)
    session.pending_corrections.extend(applied)
    if applied:
        await sio.emit("transcript_correction", {"corrections": applied}, room=sid)

# Append new content
_accumulate_full_text(session, new_text)

# Update prev window state
session.prev_raw_text = curr_raw_text
session.prev_words    = curr_words
```

### 8. New socket event — `revert_correction`

```python
@sio.event
async def revert_correction(sid, data=None):
    """Doctor reverted an LLM correction — restore original ASR word."""
    session = sessions.get(sid)
    if not session or not isinstance(data, dict):
        return
    correction_id = data.get("id", "")
    corr = next((c for c in session.pending_corrections if c["id"] == correction_id), None)
    if not corr:
        return
    # Restore: swap corrected → original
    idx = corr["offset"]
    end = idx + len(corr["corrected"])
    if session.full_text[idx:end] == corr["corrected"]:
        session.full_text = session.full_text[:idx] + corr["original"] + session.full_text[end:]
    session.pending_corrections = [c for c in session.pending_corrections if c["id"] != correction_id]
    await sio.emit("transcript_corrected", {
        "full_text": session.full_text,
        "reverted_id": correction_id,
    }, room=sid)
```

---

## Frontend changes

### `use-dictation.ts`

**New types:**
```typescript
export interface TranscriptCorrection {
  id: string;
  original: string;
  corrected: string;
  offset: number;
}
```

**New state:**
```typescript
const [corrections, setCorrections] = useState<TranscriptCorrection[]>([]);
const [showCorrections, setShowCorrections] = useState(true);
```

**New socket handlers:**
```typescript
// LLM applied a correction to committed text
s.on("transcript_correction", (data: { corrections: TranscriptCorrection[] }) => {
  setCorrections(prev => [...prev, ...data.corrections]);
});

// Correction was reverted — full_text updated
s.on("transcript_corrected", (data: { full_text: string; reverted_id: string }) => {
  if (data.full_text) setFullText(data.full_text);
  setCorrections(prev => prev.filter(c => c.id !== data.reverted_id));
});
```

**New callback:**
```typescript
const revertCorrection = useCallback((id: string) => {
  socketRef.current?.emit("revert_correction", { id });
}, []);

const editCorrection = useCallback((id: string, newText: string) => {
  // Optimistic update — doctor typed their own version
  setCorrections(prev => prev.filter(c => c.id !== id));
  // Also patch full_text locally (the server will correct on next transcription event)
}, []);
```

**New exports:** `corrections`, `showCorrections`, `setShowCorrections`, `revertCorrection`, `editCorrection`

**Clear on new session:**
```typescript
// In clearTranscript:
setCorrections([]);
```

---

### `dictation.tsx`

#### Corrections toggle button
In the transcript card header row (next to "Bestätigt / Provisorisch" legend):
```tsx
{d.corrections.length > 0 && (
  <button
    onClick={() => d.setShowCorrections(v => !v)}
    className="text-[10px] text-theme-accent/70 hover:text-theme-accent"
    title="KI-Korrekturen anzeigen/ausblenden"
  >
    {d.showCorrections ? "● " : "○ "}{d.corrections.length} Korrektur{d.corrections.length !== 1 ? "en" : ""}
  </button>
)}
```

#### Transcript rendering with corrections highlighted

Replace the plain `<span>` transcript render with a function that overlays corrections:

```tsx
function renderTranscriptWithCorrections(
  text: string,
  corrections: TranscriptCorrection[],
  showCorrections: boolean,
  onRevert: (id: string) => void,
  onEdit: (id: string, val: string) => void,
): React.ReactNode {
  if (!showCorrections || !corrections.length) {
    return <span className="text-foreground">{text}</span>;
  }
  // Sort corrections by offset, build segments
  // Highlighted segments: click=revert, double-click=inline edit input
  // Non-corrected segments: plain text
}
```

Correction chip style:
```tsx
<span
  className="rounded bg-theme-accent/15 text-theme-accent underline decoration-dotted cursor-pointer
             hover:bg-theme-accent/25 transition-colors"
  title={`Korrigiert von: "${c.original}" — Klick zum Rückgängig, Doppelklick zum Bearbeiten`}
  onClick={() => onRevert(c.id)}
  onDoubleClick={() => setEditingId(c.id)}
>
  {c.corrected}
</span>
```

Inline edit on double-click:
```tsx
{editingId === c.id ? (
  <input
    autoFocus
    defaultValue={c.corrected}
    className="rounded border border-theme-accent bg-background px-1 text-sm"
    onBlur={(e) => { onEdit(c.id, e.target.value); setEditingId(null); }}
    onKeyDown={(e) => {
      if (e.key === "Enter") { onEdit(c.id, e.currentTarget.value); setEditingId(null); }
      if (e.key === "Escape") setEditingId(null);
    }}
  />
) : ...}
```

---

## Implementation order

1. `server.py` — `STEP_INTERVAL_S = 2.0`
2. `server.py` — `call_whisper` returns `(text, words)`, enable timestamps
3. `server.py` — `AudioSession` new fields (`prev_raw_text`, `prev_words`, `pending_corrections`)
4. `server.py` — `CORRECTION_PROMPT_V2` + new `correct_window_with_llm` signature
5. `server.py` — `apply_corrections` helper + import `uuid`
6. `server.py` — `_accumulate_full_text` simplified
7. `server.py` — transcription loop: extract regions, call new LLM fn, apply corrections, update prev state
8. `server.py` — `revert_correction` socket event
9. `use-dictation.ts` — `TranscriptCorrection` type, new state, new socket handlers, `revertCorrection`, `editCorrection`
10. `dictation.tsx` — corrections toggle button, `renderTranscriptWithCorrections`, inline edit

---

## What does NOT change

- Window size: 12s
- `compute_stable_text` — still marks last 3 words provisional
- SOAP generation paths
- File streaming pipeline (uses single Whisper call, no sliding window)
- VAD logic

---

## Edge cases

| Case | Handling |
|------|----------|
| First window (no prev_words) | Skip overlap section in prompt, send only curr window + new slice |
| Whisper returns no word timestamps | Fall back to proportional estimation (existing step-ratio logic) |
| LLM returns malformed JSON | Parse defensively, log warning, treat as `{"new": raw_output, "corrections": []}` |
| Correction offset stale (text changed since correction) | `full_text[idx:end] == corrected` guard in `revert_correction` prevents wrong revert |
| Doctor edits transcript field directly | Corrections list cleared for that session (they own the text now) |
