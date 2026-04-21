# server.py Refactoring Plan

**Created:** 2026-03-31  
**Revised:** 2026-03-31  
**Current state:** 2,439 lines / 71 functions+classes / 1 file  
**Target:** ~500-line server.py orchestrator + 7 focused modules  

## What changed since v1

- **server.py grew from 1,730 → 2,439 lines** (voice commands, action commands, SOAP refinement with dirty-field protection, gold standard saving, correction diffs, fuzzy dedup)
- **V2 JSON-delta approach abandoned** — normal mode now uses same `_correct_window_simple` as compare mode (full-window correction + overlap dedup). `correct_window_with_llm` and `CORRECTION_PROMPT_V2` are dead code (kept for reference).
- **`call_whisper` simplified** — returns `(text, [])` tuple, no verbose_json attempt (GPUStack doesn't support it)
- **`_accumulate_full_text` rewritten** — uses `_prev_confirmed` window-to-window comparison with fuzzy word matching (`_word_fuzzy_eq`) and tail dedup
- **`compute_stable_text` simplified** — single-arg (current text only), no previous-window diffing
- **Sliding window tuned** — `STEP_INTERVAL_S` = 1.5s (was 2.0), giving 87.5% overlap
- **New features added:** voice commands, action commands with cooldown, SOAP field dirty protection, `_generate_soap_llm_with_context`, `_build_refinement_context`, gold standard endpoint, `apply_corrections` + `revert_correction`

---

## Extraction Order (safest → riskiest)

---

### Phase 0 — Shared Config (do this first)

#### `config.py`
**Lines:** ~70  
**Extract all env-loaded constants + sliding window params:**
- All `os.getenv(...)` blocks: Whisper, LLM, RAGFlow, VAD config
- Audio constants: `SAMPLE_RATE`, `CHANNELS`, `SAMPLE_WIDTH`
- Sliding window: `WINDOW_SIZE_S`, `STEP_INTERVAL_S`, `MIN_AUDIO_S`, `SILENCE_TIMEOUT_S`
- Display: `STABLE_WORD_COUNT`

**Risk:** Zero — pure constant extraction

---

### Phase 1 — Pure Leaf Modules (zero Socket.IO, zero shared state)

#### 1.1 `voice_commands.py`
**Lines:** ~110  
**Extract:**
- `VOICE_COMMANDS` dict, `ACTION_COMMANDS` dict, `ACTION_COOLDOWN_S` constant
- `_build_vc_pattern()`, `apply_voice_commands()`, `extract_action_commands()`
- `_vc_pattern`, `_ac_pattern` compiled regexes

**Dependencies:** `re` only  
**Risk:** Zero — pure functions

---

#### 1.2 `vad.py`
**Lines:** ~50  
**Extract:**
- `SileroVAD` class
- `VAD_THRESHOLD` constant

**Dependencies:** `torch` (lazy), `numpy`  
**Risk:** Zero — self-contained class

---

### Phase 2 — Data & Text Processing

#### 2.1 `session.py`
**Lines:** ~130  
**Extract:**
- `AudioSession` dataclass
- `compute_stable_text()`
- `_word_fuzzy_eq()`
- `_accumulate_full_text()`

**Dependencies:** `dataclasses`, `numpy`, `logging`, config constants  
**Risk:** Low — pure data container + pure functions, no I/O

---

### Phase 3 — External API Clients

#### 3.1 `whisper_client.py`
**Lines:** ~40  
**Extract:**
- `call_whisper(audio, session)` → returns `(text, [])`

**Dependencies:** `io`, `wave`, `numpy`, `aiohttp`, `time`, `logging`, config constants  
**Risk:** Low

---

#### 3.2 `llm_client.py`
**Lines:** ~150  
**Extract:**
- `_call_llm()` generic helper
- `_extract_json()` JSON extraction helper
- `_correct_window_simple()` — per-window full correction (used by both normal + compare modes)
- `correct_final_with_llm()` — finalization correction
- `apply_corrections()` — diff application
- `LLM_WINDOW_CORRECTION_PROMPT`, `LLM_FINAL_CORRECTION_PROMPT` constants
- Dead code to remove: `correct_window_with_llm()`, `CORRECTION_PROMPT_V2`

**Dependencies:** `aiohttp`, `json`, `re`, `time`, `logging`, config constants  
**Risk:** Low

---

### Phase 4 — SOAP Generation

#### 4.1 `soap.py`
**Lines:** ~300  
**Extract:**
- `SOAP_GENERATION_PROMPT`, `SOAP_SCORE_PROMPT` constants
- `_generate_soap_llm(text)`, `_generate_soap_llm_with_context(text, fields, dirty)`
- `_build_refinement_context(soap_fields, dirty_fields)`
- `_generate_soap_ragflow(text, agent_id)`
- `_parse_soap_json(raw)`, `_parse_soap_text(text)`
- `_build_soap_from_medical_facts(data)`, `_soap_value_to_str(val)`, `_join(val)`
- `_score_soap(transcript, soap)`
- `_maybe_live_soap(sid, session)`, `_emit_live_soap(sid, session)` — need `emit_fn` injection

**Coupling:** `_maybe_live_soap` / `_emit_live_soap` use `sio.emit()` → pass as callback  
**Risk:** Medium — needs emit_fn injection pattern

---

### Phase 5 — Audio Utilities

#### 5.1 `audio_utils.py`
**Lines:** ~60  
**Extract:**
- `resample_to_16k(audio, orig_sr)`
- `load_audio_bytes(data)` (WAV/MP3/FLAC detection)

**Dependencies:** `io`, `wave`, `numpy`, `scipy.signal` (optional), `pydub` (optional)  
**Risk:** Low — pure functions

---

## After All Phases

server.py retains:
- FastAPI + Socket.IO app setup (~50 lines)
- `startup()` / `shutdown()` lifecycle (~40 lines)
- Socket.IO event handlers: `connect`, `disconnect`, `start_dictation`, `stop_dictation`, `audio_chunk`, `set_*`, `update_soap_field`, `unlock_soap_field`, `revert_correction`, `set_voice_commands` (~150 lines)
- `transcription_loop()` orchestrator (~180 lines)
- HTTP routes: `/v1/transcribe`, `/v1/generate-soap`, `/v1/test-ragflow`, `/v1/gold-standard`, health, models (~80 lines)
- **Total: ~500 lines**

---

## Execution Order

```
Phase 0: config.py              (constants only — zero risk)
Phase 1: voice_commands.py      (pure functions — zero risk)
         vad.py                 (self-contained class — zero risk)
Phase 2: session.py             (dataclass + pure functions — low risk)
Phase 3: whisper_client.py      (single API fn — low risk)
         llm_client.py          (API + correction fns — low risk)
Phase 4: soap.py                (needs emit_fn injection — medium risk)
Phase 5: audio_utils.py         (pure functions — low risk)
```

## Dead Code to Remove During Refactor

| Code | Reason |
|------|--------|
| `correct_window_with_llm()` | V2 JSON-delta approach abandoned — normal mode uses `_correct_window_simple` now |
| `CORRECTION_PROMPT_V2` | Only used by dead `correct_window_with_llm` |
| `prev_words` field on `AudioSession` | Was for word timestamps (GPUStack doesn't support verbose_json) |
| `prev_raw_text` field on `AudioSession` | Only set, never read in current flow |

## Rules

1. **One module per commit** — extract, test, commit. Never extract two at once.
2. **No behavior changes** — pure mechanical extraction only. Resist "while I'm here" fixes.
3. **Keep imports explicit** — no `from module import *` except config.py.
4. **Test after each extraction** — live dictation + file upload + SOAP generation.
5. **Delete dead code** in the phase where it was defined (e.g. V2 prompt in Phase 3.2).
6. **Update CLAUDE.md file structure** after all phases complete.
