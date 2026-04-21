# Frontend — React + shadcn/ui

## Stack

- Next.js 15 (App Router, TypeScript) — pinned to ^15.5.14 (Next.js 16 crashes)
- React 19
- Tailwind CSS 4 (PostCSS)
- shadcn/ui (base-nova style, lucide icons)
- socket.io-client for WebSocket to backend

## Run

```bash
npm install
npm run dev    # http://localhost:3000
```

Backend must be running on :8003 — Next.js rewrites proxy all `/socket.io/*`, `/v1/*`, and `/health` requests there. Backend URL configurable via `BACKEND_URL` env var.

## Structure

```
app/
  page.tsx            # Entry — renders DictationPage
  dictation.tsx       # Main dictation UI (client component)
  layout.tsx          # Root layout (dark mode, Geist font, lang=de)
  globals.css         # Tailwind + CSS variable theme system
components/
  theme-provider.tsx  # React context for theme switching
  theme-switcher.tsx  # Dropdown to switch obsidian/sardinia/forest themes
  ui/                 # shadcn/ui components (button, card, badge, tabs, etc.)
lib/
  use-dictation.ts    # Core hook: Socket.IO, mic capture, WAV upload, state
  themes.ts           # Theme definitions (CSS variables per theme)
  utils.ts            # shadcn cn() utility
```

## Theme System

Three themes switchable at runtime, stored in localStorage:
1. **obsidian** — True black, zinc surfaces (default)
2. **sardinia** — Deep Mediterranean blue, cyan/sky accents, glass-morphism
3. **forest** — Dark green, emerald/lime accents, earthy warmth

Themes are defined as CSS variable maps in `lib/themes.ts` and applied to `:root` by `ThemeProvider`.

## Key Hook: useDictation()

Located in `lib/use-dictation.ts`. Returns:

### Connection / recording state
- `connected`, `recording`, `streaming` — status flags
- `toggleRecording()` — start/stop mic dictation
- `streamFile(file)` — stream audio file through the sliding-window pipeline
- `stopFileStream()` — abort file streaming
- `streamDuration`, `streamElapsed`, `streamStatus` — file stream progress

### Transcription
- `transcript` — `{confirmed, provisional}` for the current window
- `fullText` — full accumulated corrected transcription
- `corrections: TranscriptCorrection[]` — active LLM correction diffs
- `clearTranscript()` — clear transcript text only
- `revertCorrection(id)` — revert a specific LLM correction
- `editCorrection(id, newText)` — accept correction with edit
- `diffMode`, `setDiffMode` — correction highlight display mode
- `diffHotkey`, `setDiffHotkey` — hotkey for shift-to-reveal corrections

### SOAP fields
- `fields` — `Record<SoapField, string>` for subjective/objective/assessment/plan
- `activeField`, `setActiveField` — which SOAP tab is active
- `clearField()` — clear active SOAP field
- `setFieldText(field, text)` — set SOAP field content
- `dirtyFields` — fields manually edited by doctor (locked from LLM updates)
- `unlockField(field)` — re-enable a locked field for LLM updates
- `llmFields` — latest LLM-generated field values
- `generateSoap()` — trigger manual SOAP generation
- `saveGoldStandard(name)` — save current transcript + SOAP as gold standard
- `soapGenerating`, `soapDuration`, `soapScores` — generation state
- `liveSoap`, `toggleLiveSoap()` — auto-SOAP during dictation

### Compare mode
- `compareMode`, `compareEnabled`, `toggleCompareMode()` — Model A vs B
- `comparison: CompareTranscript | null` — side-by-side corrected text from both models
- `clearComparison()` — clear comparison panel only

### Voice commands
- `voiceCommands`, `setVoiceCommands` — custom per-session spoken→replacement mappings

### Clear / reset
- `clearTranscript()` — clears transcript + corrections (not comparison)
- `clearComparison()` — clears comparison panel
- `clearAll()` — clears transcript + corrections + comparison

### Stats / misc
- `chunks`, `whisperCalls`, `duration`, `level` — stats
- `whisperOnly`, `toggleWhisperOnly()` — skip LLM correction
- `ragflowAgents`, `ragflowConnected`, `selectedAgent`, `setSelectedAgent` — RAGFlow

## shadcn/ui Components Installed

badge, button, card, separator, tabs, textarea, toggle

To add more: `npx shadcn@latest add <component-name>`

## Conventions

- All colors via CSS variables (--bg, --surface, --accent, etc.) — never hardcoded
- Client components use `"use client"` directive
- German UI text throughout
- Use lucide-react for icons
- SOAP field names: always `subjective/objective/assessment/plan` — never S/O/A/P abbreviations
