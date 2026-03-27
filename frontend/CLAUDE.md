# Frontend — React + shadcn/ui

## Stack

- Next.js 16 (App Router, TypeScript)
- React 19
- Tailwind CSS 4 (PostCSS)
- shadcn/ui (base-nova style, lucide icons)
- socket.io-client for WebSocket to backend

## Run

```bash
npm install
npm run dev    # http://localhost:3000
```

Backend must be running on :8003 — Next.js rewrites proxy all `/socket.io/*`, `/v1/*`, and `/health` requests there.

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
- `connected`, `recording` — connection/recording state
- `fields` — Record<SoapField, {confirmed, provisional}> for S/O/A/P
- `activeField`, `setActiveField` — which SOAP tab is active
- `toggleRecording()` — start/stop mic
- `streamFile(file)` — stream audio file through the sliding-window pipeline
- `clearField()` — clear active field text
- `chunks`, `whisperCalls`, `duration`, `streamDuration`, `level` — stats

## shadcn/ui Components Installed

badge, button, card, separator, tabs, textarea, toggle

To add more: `npx shadcn@latest add <component-name>`

## Conventions

- All colors via CSS variables (--bg, --surface, --accent, etc.) — never hardcoded
- Client components use `"use client"` directive
- German UI text throughout
- Use lucide-react for icons
