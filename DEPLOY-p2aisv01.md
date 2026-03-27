# Deploy Streaming Dictation on p2aisv01

Step-by-step for PuTTY. All commands run as `dev@p2aisv01`.

---

## Prerequisites (already running)

Verify these are up before deploying:

```bash
# Check Whisper API (port 8001)
curl -s http://localhost:8001/v1/audio/transcriptions \
  -F "file=@/home/dev/vllm/272_2.wav" \
  -F "model=whisper-large-v3-turbo" \
  -F "language=de" | head -c 200

# Check GPUStack + Mistral (port 8000)
curl -s http://localhost:8000/v1/models \
  -H "Authorization: Bearer gpustack_bab7076d66f90f98_d937d1f7db12829b844d6f918f995cdc" | head -c 200

# Check GPU status
ixsmi
```

Expected GPU layout:
- GPU 0: Whisper (dev-vitomed-ai-whisper-api-corex) — port 8001
- GPU 6+7: Mistral Small 3.2 24B (GPUStack) — port 8000

---

## Step 1 — Copy files to server

From your Windows machine, use WinSCP:
1. Connect to 172.17.16.150 as `dev`
2. Navigate to `/home/dev/`
3. Create folder `streaming-dictation`
4. Upload all files:
   - `server.py`
   - `Dockerfile`
   - `docker-compose.yml`
   - `requirements.txt`
   - `static/index.html` (create `static/` subfolder first)

Or from PuTTY if files are on GitHub:
```bash
cd ~
git clone <your-repo-url> streaming-dictation
```

---

## Step 2 — Verify the GPUStack API key

The docker-compose.yml uses the GPUStack API key. Verify it works:

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer gpustack_bab7076d66f90f98_d937d1f7db12829b844d6f918f995cdc" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Mistral-Small-3.2-24B-Instruct-2506",
    "messages": [{"role": "user", "content": "Korrigiere: Der Patient hat Tafalgan genommen"}],
    "max_tokens": 100
  }' | python3 -m json.tool
```

If the API key is expired, create a new one:
1. Open http://172.17.16.150:8000 in browser
2. Go to Access Control → API Keys → Create
3. Update `LLM_API_KEY` in docker-compose.yml

---

## Step 3 — Check the model name

The model name in GPUStack must match exactly. Check:

```bash
curl -s http://localhost:8000/v1/models \
  -H "Authorization: Bearer gpustack_bab7076d66f90f98_d937d1f7db12829b844d6f918f995cdc" \
  | python3 -c "import sys,json; [print(m['id']) for m in json.load(sys.stdin)['data']]"
```

If the Mistral model name is different from `Mistral-Small-3.2-24B-Instruct-2506`,
update `LLM_MODEL` in docker-compose.yml to match.

---

## Step 4 — Check disk space

```bash
df -h /
```

The streaming-dictation image is small (~2GB with PyTorch for VAD).
You need at least 5GB free on `/dev/sda4`. If tight, Silero VAD
downloads a ~3MB model on first run — negligible.

---

## Step 5 — Build and start

```bash
cd ~/streaming-dictation

# Build the container (first time takes ~3 minutes for pip installs)
docker compose build

# Start in background
docker compose up -d

# Watch logs
docker logs -f vitodata-streaming-dictation
```

Expected startup output:
```
Starting Vitodata Streaming Dictation Server...
Whisper API: http://localhost:8001/v1/audio/transcriptions
LLM Correction: ENABLED (model: Mistral-Small-3.2-24B-Instruct-2506, ...)
Silero VAD loaded (threshold=0.30)
Server ready.
INFO:     Started server process
INFO:     Uvicorn running on http://0.0.0.0:8003
```

---

## Step 6 — Test it

### Quick health check:
```bash
curl http://localhost:8003/health
```

### Open in browser:
Navigate to `http://172.17.16.150:8003`

1. Click the red button (or press Space)
2. Allow microphone access when browser asks
3. Speak in German: "Der Patient klagt über Kopfschmerzen seit drei Tagen"
4. Watch text appear — white = confirmed, orange = provisional
5. Click red button again to stop → final text gets LLM correction
6. Try the S/O/A/P field tabs

### Test with WAV file (from PuTTY):
```bash
curl -X POST http://localhost:8003/v1/transcribe \
  -F "file=@/home/dev/vllm/272_2.wav"
```

---

## Step 7 — Verify LLM correction is working

In the browser test, after you stop dictation, check:
- The final text should have corrected medication names
- Browser console (F12) shows the WebSocket events including `raw_whisper` field

Or test the LLM directly:
```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer gpustack_bab7076d66f90f98_d937d1f7db12829b844d6f918f995cdc" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Mistral-Small-3.2-24B-Instruct-2506",
    "messages": [
      {"role": "system", "content": "Korrigiere ASR-Fehler. Nur korrigierten Text zurückgeben."},
      {"role": "user", "content": "Der Patient nimmt Tafalgan ein Gramm dreimal täglich und Gesinsaal zehn Milligramm"}
    ],
    "temperature": 0.1, "max_tokens": 200
  }' | python3 -c "import sys,json; print(json.load(sys.stdin)['choices'][0]['message']['content'])"
```

Expected: "Der Patient nimmt Dafalgan ein Gramm dreimal täglich und Xyzal zehn Milligramm"

---

## Troubleshooting

### Container won't start
```bash
docker logs vitodata-streaming-dictation 2>&1 | tail -30
```

### "Connection refused" to Whisper
The container uses `network_mode: host`, so `localhost:8001` should work.
Check Whisper is running:
```bash
docker ps | grep whisper
```

### VAD won't load (torch issue)
If Silero VAD fails to load, the server still runs — it just sends audio
to Whisper without silence detection. Check logs for the warning.

### LLM correction not working
Server falls back to raw Whisper output on any LLM error.
Check logs for "LLM correction failed" messages.
To disable correction entirely:
```bash
# In docker-compose.yml, change:
- LLM_CORRECTION_ENABLED=false
# Then restart:
docker compose restart
```

### Port 8003 already in use
```bash
sudo lsof -i :8003
# Change the port in server.py (last line) and rebuild
```

---

## Port Summary on p2aisv01

| Port | Service | Used by dictation? |
|------|---------|-------------------|
| 80/443 | RAGFlow web | No |
| 8000 | GPUStack (Mistral, Qwen, etc.) | Yes — LLM correction |
| 8001 | Whisper API (gunicorn) | Yes — ASR |
| **8003** | **Streaming Dictation (NEW)** | **This service** |
| 9380-9382 | RAGFlow API/MCP | No |
| 9000-9001 | MinIO | No |

---

## Updating

```bash
cd ~/streaming-dictation
# Edit files with WinSCP, then:
docker compose up -d --build
```
