# SOAP Refinement + Dirty Field Protection

**Date:** 2026-03-31
**Status:** Corrections applied — ready for implementation

---

## Design Decisions (agreed)

| Decision | Choice |
|----------|--------|
| Dirty field handling in LLM prompt | **B** — locked context: LLM sees it, can't overwrite it |
| What triggers dirty | **A** — any keystroke |
| Dirty state location | **C** — both frontend (UI) + backend session (content store) |
| Unlock behaviour | Wait for next trigger at **80 chars** (lower threshold than normal 150) |
| View modes | **B + C**, globally toggled by configurable hotkey (Alt+key) |
| Field sizing | Expandable + scrollable (resize-y, max-h, overflow-y-auto) |

---

## Naming Convention (CRITICAL — applies everywhere)

**Always use full names: `subjective / objective / assessment / plan`**

- Socket events: `update_soap_field`, `unlock_soap_field`, `soap_update.dirty_protected`
- Session storage: `session.soap_fields`
- Frontend state: `fields`, `llmFields`, `dirtyFields`

S/O/A/P only appears **inside the LLM JSON response** and the prompt template. The parsing layer (`_parse_soap_json`) maps S→subjective etc. and is the only place the short keys exist.

---

## Architecture

### Data flow — normal live SOAP cycle

```
Doctor speaks
  → transcript grows by 150 chars
  → _maybe_live_soap fires
  → _emit_live_soap(sid, session)
  → LLM prompt: transcript + existing clean fields as draft + dirty fields as LOCKED
  → soap_update event: { subjective, objective, assessment, plan, dirty_protected: ["plan", ...] }
  → Frontend: overwrite only clean fields, keep dirty fields untouched
```

### Data flow — doctor edits a field

```
Doctor types in textarea
  → setFieldText called → field marked dirty immediately in React state
  → debounced 300ms → update_soap_field { field: "plan", text: "..." }
  → Backend: session.soap_fields["plan"] = text; session.dirty_fields.add("plan")
  → Padlock icon shown on field header
```

### Data flow — doctor unlocks a field

```
Doctor clicks padlock
  → Frontend: dirty flag cleared for that field
  → unlock_soap_field { field: "plan" }
  → Backend: session.dirty_fields.discard("plan"); session._soap_unlock_pending = True
  → _maybe_live_soap threshold drops to 80 chars until next trigger fires
  → Next trigger fills the unlocked field from LLM
  → session._soap_unlock_pending reset to False
```

---

## Backend changes (`server.py`)

### 1. `AudioSession` — new fields

```python
soap_fields: dict = field(default_factory=lambda: {
    "subjective": "", "objective": "", "assessment": "", "plan": ""
})
dirty_fields: set = field(default_factory=set)
_soap_unlock_pending: bool = False
```

### 2. Constants

```python
_LIVE_SOAP_MIN_CHARS = 150        # existing
_LIVE_SOAP_UNLOCK_CHARS = 80      # new — lower threshold after unlock
```

### 3. `_maybe_live_soap` — unlock threshold + pass session

```python
async def _maybe_live_soap(sid: str, session: AudioSession) -> None:
    if not session.live_soap:
        return
    text = session.full_text
    threshold = _LIVE_SOAP_UNLOCK_CHARS if session._soap_unlock_pending else _LIVE_SOAP_MIN_CHARS
    new_chars = len(text) - session._soap_text_len if text else 0
    if not text or new_chars < threshold:
        return
    log.info("Live SOAP triggered: %d new chars (threshold=%d)", new_chars, threshold)
    session._soap_text_len = len(text)
    asyncio.create_task(_emit_live_soap(sid, session))   # ← pass session, not text
```

### 4. `_emit_live_soap` — new signature, injects refinement context

```python
async def _emit_live_soap(sid: str, session: AudioSession) -> None:
    session._soap_unlock_pending = False   # reset after firing
    try:
        await sio.emit("soap_generating", {}, room=sid)
        t0 = time.perf_counter()
        soap = await _generate_soap_llm_with_context(
            session.full_text, session.soap_fields, session.dirty_fields
        )
        duration_ms = round((time.perf_counter() - t0) * 1000)
        if soap:
            soap["duration_ms"] = duration_ms
            soap["dirty_protected"] = list(session.dirty_fields)
            await sio.emit("soap_update", soap, room=sid)
    except Exception as e:
        log.warning("Live SOAP generation failed: %s", e, exc_info=True)
```

### 5. `_generate_soap_llm_with_context` — new function

```python
async def _generate_soap_llm_with_context(
    text: str,
    soap_fields: dict,
    dirty_fields: set,
) -> dict | None:
    context_block = _build_refinement_context(soap_fields, dirty_fields)
    prompt = SOAP_GENERATION_PROMPT + "\n\n" + context_block if context_block else SOAP_GENERATION_PROMPT
    result = await _call_llm(prompt, text, http_session, timeout=30.0, label="LLM-SOAP", max_tokens=2048)
    if not result:
        return None
    return _parse_soap_json(result)


def _build_refinement_context(soap_fields: dict, dirty_fields: set) -> str:
    has_content = any(v.strip() for v in soap_fields.values())
    if not has_content:
        return ""
    label_map = {
        "subjective": "subjective",
        "objective":  "objective",
        "assessment": "assessment",
        "plan":       "plan",
    }
    lines = [
        "## REFINEMENT CONTEXT",
        "Sections marked AUTO: update freely with new transcript content.",
        "Sections marked LOCKED: confirmed by the physician — preserve exactly, do not modify.",
        "",
    ]
    for field, label in label_map.items():
        status = "LOCKED" if field in dirty_fields else "AUTO"
        content = soap_fields.get(field, "").strip() or "(empty)"
        lines.append(f"### {label} [{status}]")
        lines.append(content)
        lines.append("")
    return "\n".join(lines)
```

### 6. `_parse_soap_json` — map S/O/A/P → full names on output

Add mapping in the existing parser so output always uses full names:

```python
KEY_MAP = {"S": "subjective", "O": "objective", "A": "assessment", "P": "plan"}
# After parsing, remap keys:
return {KEY_MAP.get(k, k): v for k, v in parsed.items()}
```

### 7. New socket events

```python
@sio.event
async def update_soap_field(sid, data=None):
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
    session = sessions.get(sid)
    if not session or not isinstance(data, dict):
        return
    field = data.get("field", "")
    if field not in ("subjective", "objective", "assessment", "plan"):
        return
    session.dirty_fields.discard(field)
    session._soap_unlock_pending = True
    log.info("Field '%s' unlocked for %s — next SOAP at %d chars", field, sid, _LIVE_SOAP_UNLOCK_CHARS)
```

---

## Frontend changes

### `use-dictation.ts`

**New exports needed:** `dirtyFields`, `unlockField`, `llmFields`, `diffMode`, `setDiffMode`, `diffHotkey`, `setDiffHotkey`

**New state:**
```typescript
const DIFF_HOTKEY_KEY = "dictation_diff_hotkey";

const [dirtyFields, setDirtyFields] = useState<Set<SoapField>>(new Set());
const [llmFields, setLlmFields] = useState<Record<SoapField, string>>({
  subjective: "", objective: "", assessment: "", plan: "",
});
const [diffMode, setDiffMode] = useState(false);
const [diffHotkey, setDiffHotkey] = useState<string>(() =>
  typeof window !== "undefined"
    ? (localStorage.getItem(DIFF_HOTKEY_KEY) ?? "d")
    : "d"
);
```

**Debounce ref:**
```typescript
const soapFieldDebounceRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
```

**`setFieldText` — gains dirty tracking + debounced emit:**
```typescript
const setFieldText = useCallback((field: SoapField, text: string) => {
  setFields(prev => ({ ...prev, [field]: text }));
  setDirtyFields(prev => { const s = new Set(prev); s.add(field); return s; });
  // Debounced socket emit
  clearTimeout(soapFieldDebounceRef.current[field]);
  soapFieldDebounceRef.current[field] = setTimeout(() => {
    socketRef.current?.emit("update_soap_field", { field, text });
  }, 300);
}, []);
```

**`unlockField`:**
```typescript
const unlockField = useCallback((field: SoapField) => {
  setDirtyFields(prev => { const s = new Set(prev); s.delete(field); return s; });
  socketRef.current?.emit("unlock_soap_field", { field });
}, []);
```

**`setDiffHotkey` — persists to localStorage:**
```typescript
const updateDiffHotkey = useCallback((key: string) => {
  setDiffHotkey(key);
  localStorage.setItem(DIFF_HOTKEY_KEY, key);
}, []);
```

**`soap_update` handler — respect `dirty_protected`, store `llmFields`:**
```typescript
s.on("soap_update", (data: {
  subjective?: string; objective?: string; assessment?: string; plan?: string;
  dirty_protected?: string[]; duration_ms?: number;
}) => {
  const protectedFields = new Set(data.dirty_protected ?? []);
  const incoming: Record<SoapField, string> = {
    subjective: data.subjective ?? "",
    objective:  data.objective  ?? "",
    assessment: data.assessment ?? "",
    plan:       data.plan       ?? "",
  };
  // Always store the LLM's version for diff/suggestion display
  setLlmFields(incoming);
  // Only overwrite clean fields
  setFields(prev => ({
    subjective: protectedFields.has("subjective") ? prev.subjective : incoming.subjective,
    objective:  protectedFields.has("objective")  ? prev.objective  : incoming.objective,
    assessment: protectedFields.has("assessment") ? prev.assessment : incoming.assessment,
    plan:       protectedFields.has("plan")       ? prev.plan       : incoming.plan,
  }));
  if (soapTimerRef.current) { clearInterval(soapTimerRef.current); soapTimerRef.current = null; }
  setSoapGenerating(false);
  if (data.duration_ms != null) setSoapDuration(data.duration_ms);
});
```

**Global diff hotkey listener (in `useEffect`):**
```typescript
useEffect(() => {
  const handler = (e: KeyboardEvent) => {
    if (e.altKey && e.key === diffHotkey) {
      e.preventDefault();
      setDiffMode(v => !v);
    }
  };
  window.addEventListener("keydown", handler);
  return () => window.removeEventListener("keydown", handler);
}, [diffHotkey]);
```

**Cleanup debounce timers on unmount:**
```typescript
// In the existing cleanup useEffect:
Object.values(soapFieldDebounceRef.current).forEach(clearTimeout);
```

---

### `dictation.tsx`

#### Expandable/scrollable fields
```tsx
// Textarea: add max-h and overflow
className="min-h-[100px] max-h-[400px] resize-y overflow-y-auto border-none bg-transparent ..."
```

#### Field header — padlock icon
```tsx
import { Lock, Unlock } from "lucide-react";

// In the field card header:
{d.dirtyFields.has(f) && (
  <button
    type="button"
    onClick={() => d.unlockField(f)}
    title="Feld für automatische Aktualisierung freigeben"
    className="ml-auto flex items-center gap-1 text-[10px] text-theme-accent/70 hover:text-theme-accent"
  >
    <Lock className="h-3 w-3" />
    <span>gesperrt</span>
  </button>
)}
```

#### Mode B — LLM suggestion panel (default, `!diffMode`)
Only shown when field is dirty AND LLM has a different version:
```tsx
{!d.diffMode && d.dirtyFields.has(f) && d.llmFields[f]?.trim() && (
  <details className="mt-2 border-t border-border pt-2">
    <summary className="cursor-pointer text-[10px] uppercase tracking-widest text-muted-foreground hover:text-foreground">
      KI-Vorschlag anzeigen
    </summary>
    <p className="mt-1.5 whitespace-pre-wrap text-sm text-muted-foreground/70 leading-6">
      {d.llmFields[f]}
    </p>
  </details>
)}
```

#### Mode C — inline diff (`diffMode && dirtyFields.has(f)`)
Replace textarea with read-only diff view. Reuses existing `computeWordDiff`.
```tsx
{d.diffMode && d.dirtyFields.has(f) && d.llmFields[f]?.trim() ? (
  <div className="min-h-[100px] max-h-[400px] overflow-y-auto whitespace-pre-wrap text-sm leading-6 py-2">
    {computeWordDiff(d.llmFields[f], d.fields[f]).map((seg, i) => (
      <span key={i} className={
        seg.type === "removed" ? "bg-danger/15 text-danger line-through decoration-danger/40" :
        seg.type === "added"   ? "bg-success/15 text-success" :
        "text-foreground"
      }>{i > 0 ? " " : ""}{seg.text}</span>
    ))}
  </div>
) : (
  <Textarea ... />  // normal edit mode
)}
```

#### Diff mode indicator + hotkey hint
Small status line below the SOAP grid:
```tsx
{(d.dirtyFields.size > 0) && (
  <p className="text-[10px] text-muted-foreground">
    {d.diffMode ? "Diff-Ansicht aktiv" : "Bearbeitete Felder gesperrt"}
    {" · "}
    <kbd className="rounded border border-border px-1">Alt+{d.diffHotkey}</kbd>
    {" "}zum {d.diffMode ? "Bearbeiten" : "Vergleichen"}
  </p>
)}
```

#### Hotkey config — in voice commands panel (or settings row)
```tsx
<div className="flex items-center gap-2 text-xs text-muted-foreground">
  <span>Diff-Ansicht:</span>
  <kbd className="rounded border border-border px-1.5 py-0.5 text-foreground">Alt+</kbd>
  <input
    type="text"
    maxLength={1}
    value={d.diffHotkey}
    onChange={(e) => d.setDiffHotkey(e.target.value)}
    className="h-6 w-8 rounded border border-border bg-background text-center text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-theme-accent"
  />
</div>
```

---

## Implementation order

1. `server.py` — `AudioSession` new fields + `_LIVE_SOAP_UNLOCK_CHARS`
2. `server.py` — `_parse_soap_json` key remapping (S→full names)
3. `server.py` — `_build_refinement_context` + `_generate_soap_llm_with_context`
4. `server.py` — `_emit_live_soap` new signature + `_maybe_live_soap` update
5. `server.py` — `update_soap_field` + `unlock_soap_field` socket events
6. `use-dictation.ts` — all new state, `setFieldText` dirty tracking, `unlockField`, updated `soap_update` handler, diff hotkey
7. `dictation.tsx` — expandable fields, padlock icon, Mode B, Mode C, status line, hotkey config

---

## What does NOT change

- RAGFlow path (`_generate_soap_ragflow`) — refinement context not injected there yet
  - `# TODO: RAGFlow refinement context not yet implemented`
- The `generate-soap` REST endpoint — starts fresh intentionally (manual button)
- The 150-char normal live SOAP threshold
