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
  ├── Sliding Window (sends last 12s to Whisper every ~1.5s)
  │     └── Whisper large-v3-turbo via GPUStack (172.17.16.150:8000)
  ├── LLM Processing — per chunk (Mistral Small 3.2 24B AWQ via GPUStack)
  │     └── Punctuation, formatting, filler removal, ASR correction, medical abbreviations
  ├── Diff Engine (confirmed vs provisional text)
  ↓ WebSocket push
React Frontend (Next.js + shadcn/ui, port 3000, proxies to :8003)
  ├── Live transcription display (confirmed=white, provisional=amber)
  ├── SOAP field tabs (S/O/A/P)
  ├── WAV file upload for batch transcription
  └── Theme switcher (obsidian/sardinia/forest)
```

## Tech Stack

| Layer | Tech |
|-------|------|
| Backend | Python 3.12, FastAPI, python-socketio, aiohttp |
| Frontend | Next.js 16, React 19, TypeScript, Tailwind CSS 4, shadcn/ui |
| ASR | Whisper large-v3-turbo (vLLM on Iluvatar GPU via GPUStack) |
| LLM | Mistral Small 3.2 24B Instruct AWQ (GPUStack, GPUs 6+7) |
| Infra | GPUStack on p2aisv01 (172.17.16.150), Iluvatar GPUs (NOT NVIDIA) |

## File Structure

```
strmcp/
├── server.py              # FastAPI + Socket.IO backend (port 8003)
├── .env                   # Environment vars (API URLs, keys) — loaded by python-dotenv
├── docker-compose.yml     # Production deployment config
├── Dockerfile             # Container build
├── requirements.txt       # Python deps
├── dictation_client.py    # Windows desktop client (types into any app via clipboard)
├── static/index.html      # Legacy vanilla HTML frontend (still works at :8003)
├── frontend/              # NEW — React app with shadcn/ui (port 3000)
│   ├── app/               # Next.js app router pages
│   ├── components/        # React components + shadcn ui/
│   ├── lib/               # Hooks (use-dictation.ts), utils, themes
│   └── next.config.ts     # Proxies /socket.io/* and /v1/* to :8003
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
| WHISPER_MODEL | Model ID in GPUStack | whisper-large-v3-turbo |
| LLM_API_URL | LLM endpoint | http://172.17.16.150:8000/v1/chat/completions |
| LLM_MODEL | Model ID | mistral-small-3.2-24b-instruct-2506-awq-sym |
| LLM_API_KEY | GPUStack bearer token | (in .env) |
| LLM_CORRECTION_ENABLED | Enable LLM post-processing | true |
| VAD_ENABLED | Load Silero VAD (needs torch) | true (set false locally to save RAM) |

## GPUStack Details

- **Host:** 172.17.16.150:8000 (all models routed through single proxy)
- **Auth:** Bearer token in Authorization header (same key for Whisper + LLM)
- **GPUs:** Iluvatar (NOT NVIDIA) — no CUDA, no faster-whisper. CoreX 4.4.0 runtime.
- **Whisper:** GPU 0 (MR-V100), ~1.5-2s for 12s sliding window
- **Mistral:** GPUs 6+7 (MR-V100 pair), ~0.3-0.5s per correction chunk

## Regulatory Constraint (Critical)

This is a **documentation tool**, NOT a medical device (EU MDR). The LLM must:
- NEVER infer diagnoses
- NEVER substitute medications (brand ↔ generic)
- ONLY fix ASR errors, add punctuation/formatting, remove fillers
- Preserve everything the doctor actually said

## Current Status / TODO

- ✅ Streaming transcription with sliding window + diff engine
- ✅ WAV file upload endpoint (/v1/transcribe)
- ✅ Legacy vanilla HTML frontend (static/index.html)
- ✅ React frontend scaffolded with shadcn/ui + 3 themes
- 🔧 LLM currently runs only on finalize — needs rewrite to run per-chunk with context buffer
- 🔧 Voice command detection (phrase matching before LLM) — not yet implemented
- 🔧 SOAP note generation via RAGFlow agents — not yet integrated
- 🔧 Frontend needs full component buildout (dictation UI, SOAP panel, animations)

## Conventions

- Swiss German orthography: **ss** not ß
- All user-facing text in German
- Backend uses aiohttp for async HTTP to GPUStack
- Frontend proxies API calls via Next.js rewrites (no direct CORS)
- Never hardcode API keys in source — always .env / env vars
