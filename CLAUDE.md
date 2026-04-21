# Vitodata Streaming Dictation — Project Context

## What This Is

A live streaming medical dictation system for Vitodata AG (Swiss healthcare IT, 5000+ doctors). Converts doctor speech into structured SOAP notes. Everything runs **on-premises** for Swiss nDSG compliance — no patient audio leaves the building.

## Architecture

```
Doctor's browser/desktop (mic)
  ↓ WebSocket (PCM 16kHz mono 16-bit)
Python Backend (server.py, port 8003, FastAPI + Socket.IO)
  ├── Audio Buffer (accumulates chunks)
  ├── VAD — Silero voice activity detection (optional, CPU)
  ├── Sliding Window — 12s window, decoupled cadence (normal mode):
  │     ├── Whisper every 2s → provisional amber text (raw ASR tail)
  │     └── LLM every 10s  → committed white text (corrected, deduplicated)
  │     Whisper large-v3-turbo-nvidia via GPUStack (172.17.16.150:8000)
  ├── LLM Processing — Mistral Small 3.2 24B NVFP4 via GPUStack
  │     └── ASR correction, punctuation, medical abbreviations, Swiss orthography
  ├── Compare Mode — runs both Mistral + Qwen 72B in parallel, side-by-side diff
  ├── Diff Engine (confirmed vs provisional, word-level fuzzy overlap dedup)
  ├── SOAP Generation — RAGFlow agents + direct LLM, live updates during dictation
  ↓ WebSocket push
React Frontend (Next.js + shadcn/ui, port 3000, proxies to :8003)
  ├── Live transcription (confirmed=white, provisional=amber, LLM corrections highlighted)
  ├── SOAP tabs (Subjektiv/Objektiv/Beurteilung/Prozedere)
  ├── Compare mode panel (side-by-side + word diff)
  ├── WAV file streaming with progress
  └── Theme switcher (obsidian/sardinia/forest)
```

## Tech Stack

| Layer | Tech |
|-------|------|
| Backend | Python 3.12, FastAPI, python-socketio, aiohttp |
| Frontend | Next.js 16, React 19, TypeScript, Tailwind CSS 4, shadcn/ui |
| ASR | Whisper large-v3-turbo-nvidia (GPUStack) |
| LLM | Mistral Small 3.2 24B Instruct NVFP4 (GPUStack) |
| LLM-B | Qwen 2.5 72B Instruct AWQ (GPUStack, compare mode only) |
| Infra | GPUStack on p2aisv01 (172.17.16.150), NVIDIA GPUs |

## File Structure

```
strmcp/
├── server.py              # FastAPI + Socket.IO backend (port 8003)
├── .env                   # Environment vars (API URLs, keys) — loaded by python-dotenv
├── docker-compose.yml     # Production deployment config
├── Dockerfile             # Container build
├── requirements.txt       # Python deps
├── dictation_client.py    # Windows desktop client (types into any app via clipboard)
├── index.html             # Legacy vanilla HTML frontend (also at static/index.html)
├── static/index.html      # Legacy vanilla HTML frontend (still works at :8003)
├── docs/plans/            # Architecture and implementation plans
├── frontend/              # React app with shadcn/ui (port 3000)
│   ├── app/               # Next.js app router pages
│   ├── components/        # React components + shadcn ui/
│   ├── lib/               # Hooks (use-dictation.ts), utils, themes
│   └── next.config.ts     # Proxies /socket.io/*, /v1/*, /health to :8003
└── .claude/               # Claude Code settings + MCP config
```

## Key Commands

```bash
# Backend (from project root, venv active)
pip install -r requirements.txt
python server.py                    # Starts on http://0.0.0.0:8003

# Frontend (from frontend/)
npm install
npm run dev                         # Starts on http://localhost:3000, proxies to :8003

# Docker (production on p2aisv01)
docker compose up -d --build
```

## Environment Variables (.env)

| Var | Purpose | Default |
|-----|---------|---------|
| WHISPER_API_URL | Whisper endpoint | http://172.17.16.150:8000/v1/audio/transcriptions |
| WHISPER_MODEL | Model ID in GPUStack | whisper-large-v3-turbo-nvidia |
| WHISPER_LANGUAGE | Language hint | de |
| LLM_API_URL | LLM endpoint | http://172.17.16.150:8000/v1/chat/completions |
| LLM_MODEL | Primary model ID | mistral-small-3.2-24b-instruct-2506-nvfp4 |
| LLM_API_KEY | GPUStack bearer token | (in .env) |
| LLM_CORRECTION_ENABLED | Enable LLM post-processing | true |
| LLM_MODEL_B | Compare mode model ID | qwen2.5-72b-instruct-awq-copy |
| LLM_MODEL_B_NAME | Display name for Model B | Qwen2.5 72B Instruct |
| VAD_ENABLED | Load Silero VAD (needs torch) | true (set false locally to save RAM) |
| RAGFLOW_BASE_URL | RAGFlow server | http://172.17.16.150 |
| RAGFLOW_API_KEY | RAGFlow bearer token | (in .env) |
| RAGFLOW_AGENTS | JSON list of {id, name} agent objects | [] |
| RAGAS_PROJECT_DIR | Path for gold standard saves | (in .env) |

## GPUStack Details

- **Host:** 172.17.16.150:8000 (all models routed through single proxy)
- **Auth:** Bearer token in Authorization header (same key for Whisper + LLM)
- **GPUs:** NVIDIA
- GPUStack Whisper does NOT support `verbose_json` or `timestamp_granularities[]` — always use `json` format

## Transcription Architecture Details

### Normal mode (single model, decoupled cadence)
- **Whisper** fires every `STEP_INTERVAL_S = 2.0s` → emits raw provisional tail (last 3 words, amber)
- **LLM** fires every `LLM_STEP_INTERVAL_S = 10.0s` → `_correct_window_simple` corrects full 12s window → `_accumulate_full_text` deduplicates via fuzzy word overlap (2s overlap ≈ 4-6 words, easy to detect)
- After stop: Whisper on all accumulated audio → `correct_final_with_llm` on full text

### Compare mode (two models in parallel)
- Both `LLM_MODEL` and `LLM_MODEL_B` run on every Whisper step (2s cadence)
- Side-by-side display + unified word diff
- `full_text` uses Model A (Mistral) accumulation

### File streaming
- Audio fed at 2× real-time speed via `transcribe_file_stream` Socket.IO event
- Supports WAV, MP3, FLAC, OGG (decoded server-side via soundfile/numpy)
- Uses the same transcription loop as live mic
- Whisper-only mode: single Whisper call, no sliding window — instant
- Progress events: `file_stream_started`, `file_stream_progress`, `file_stream_done`
- Final pass: `correct_final_with_llm` on accumulated text

### Live SOAP
- Triggered automatically during dictation when new text exceeds ~100 chars since last SOAP
- Dirty fields (edited by doctor) are locked and excluded from LLM updates
- `unlock_soap_field` re-enables a field and lowers the next trigger threshold
- SOAP scoring: LLM evaluates faithfulness + completeness (0-1 scores)

## Regulatory Constraint (Critical)

This is a **documentation tool**, NOT a medical device (EU MDR). The LLM must:
- NEVER infer diagnoses
- NEVER substitute medications (brand ↔ generic)
- ONLY fix ASR errors, add punctuation/formatting, remove fillers
- Preserve everything the doctor actually said

## Current Status

- ✅ Streaming transcription — sliding window Whisper + decoupled LLM cadence (2s provisional / 10s committed)
- ✅ LLM window correction (Mistral Small 3.2) + final correction pass on stop
- ✅ LLM correction diffs — highlights changed words, shift-to-reveal, click to revert
- ✅ Compare mode — Mistral vs Qwen 72B side-by-side with word diff
- ✅ Voice command detection (punkt, komma, neue zeile, etc.) + custom per-session voice commands
- ✅ Action commands (soap erstellen, mach einen bericht, etc.)
- ✅ WAV/MP3/FLAC/OGG file upload + streaming with progress bar
- ✅ Whisper-only mode — skip LLM correction entirely (single Whisper call for files)
- ✅ SOAP note generation via RAGFlow agents + direct LLM fallback
- ✅ Live SOAP — auto-updates SOAP fields during dictation (every ~100 chars new text)
- ✅ SOAP refinement — dirty field protection, unlock/re-generate per field
- ✅ SOAP scoring — LLM evaluates faithfulness + completeness (0-1)
- ✅ Gold standard saves to RAGAS project dir
- ✅ React frontend — 3 themes, SOAP tabs, compare panel, corrections UI
- ✅ Legacy vanilla HTML frontend (static/index.html)
- ✅ Windows desktop dictation client (dictation_client.py)
- 🔧 Overlap detection in `_accumulate_full_text` — fuzzy word matching works but occasional drift
- 🔧 LLM sometimes outputs "..." prefix in corrections — gets accumulated as literal text

## Conventions

- Swiss German orthography: **ss** not ß
- All user-facing text in German
- Backend uses aiohttp for async HTTP to GPUStack
- Frontend proxies API calls via Next.js rewrites (no direct CORS)
- Never hardcode API keys in source — always .env / env vars
- SOAP field names in code: always `subjective/objective/assessment/plan` (never S/O/A/P abbreviations outside LLM parsing)

## HTTP Endpoints

| Method | Path | Purpose |
|--------|------|--------|
| GET | `/` | Serves legacy HTML frontend |
| GET | `/health` | Health check |
| GET | `/v1/compare-models` | List available models for compare mode |
| POST | `/v1/transcribe` | Single-shot WAV transcription |
| GET | `/v1/ragflow-agents` | List configured RAGFlow agents |
| GET | `/v1/ragflow-health` | RAGFlow availability check |
| POST | `/v1/test-ragflow` | Debug RAGFlow agent call |
| POST | `/v1/generate-soap` | Generate SOAP notes from transcript |
| POST | `/v1/save-gold-standard` | Save gold standard to RAGAS project dir |

## Socket.IO Events

**Client → Server:**
`start_dictation`, `stop_dictation`, `audio_chunk`, `transcribe_file_stream`, `stop_file_stream`, `set_compare_mode`, `set_whisper_only`, `set_live_soap`, `update_soap_field`, `unlock_soap_field`, `revert_correction`, `set_voice_commands`

**Server → Client:**
`dictation_started`, `transcription`, `transcript_correction`, `transcript_corrected`, `transcription_compare`, `transcription_error`, `soap_generating`, `soap_update`, `file_stream_started`, `file_stream_progress`, `file_stream_status`, `file_stream_done`
