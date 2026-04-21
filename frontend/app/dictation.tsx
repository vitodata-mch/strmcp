"use client";

import { useEffect, useRef, useState } from "react";
import { useDictation, SoapField, SoapScores, CompareTranscript, VoiceCommand, TranscriptCorrection } from "@/lib/use-dictation";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { Textarea } from "@/components/ui/textarea";
import { ThemeSwitcher } from "@/components/theme-switcher";
import { Lock } from "lucide-react";

const SOAP_LABELS: Record<SoapField, { short: string; label: string }> = {
  subjective:  { short: "S", label: "Subjektiv" },
  objective:   { short: "O", label: "Objektiv" },
  assessment:  { short: "A", label: "Assessment" },
  plan:        { short: "P", label: "Plan" },
};

function ScoreBadge({ label, value }: { label: string; value: number }) {
  const pct = Math.round(value * 100);
  const color =
    pct >= 80 ? "text-success border-success/30 bg-success/5"
    : pct >= 60 ? "text-theme-accent border-theme-accent/30 bg-theme-accent/5"
    : "text-danger border-danger/30 bg-danger/5";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-xs font-medium ${color}`}>
      {label}
      <span className="tabular-nums">{pct}%</span>
    </span>
  );
}

/* ─── Word-level diff ────────────────────────────────────────────────── */

type DiffSegment = { text: string; type: "same" | "added" | "removed" };

function computeWordDiff(textA: string, textB: string): DiffSegment[] {
  const wordsA = textA.split(/\s+/).filter(Boolean);
  const wordsB = textB.split(/\s+/).filter(Boolean);
  if (!wordsA.length && !wordsB.length) return [];
  if (!wordsA.length) return [{ text: textB, type: "added" }];
  if (!wordsB.length) return [{ text: textA, type: "removed" }];

  // LCS table
  const m = wordsA.length, n = wordsB.length;
  const dp: number[][] = Array.from({ length: m + 1 }, () => new Array(n + 1).fill(0));
  for (let i = 1; i <= m; i++)
    for (let j = 1; j <= n; j++)
      dp[i][j] = wordsA[i - 1] === wordsB[j - 1]
        ? dp[i - 1][j - 1] + 1
        : Math.max(dp[i - 1][j], dp[i][j - 1]);

  // Backtrack to build diff
  const segments: DiffSegment[] = [];
  let i = m, j = n;
  const raw: { word: string; type: DiffSegment["type"] }[] = [];
  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && wordsA[i - 1] === wordsB[j - 1]) {
      raw.push({ word: wordsA[i - 1], type: "same" });
      i--; j--;
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      raw.push({ word: wordsB[j - 1], type: "added" });
      j--;
    } else {
      raw.push({ word: wordsA[i - 1], type: "removed" });
      i--;
    }
  }
  raw.reverse();

  // Merge consecutive same-type words into segments
  for (const r of raw) {
    const last = segments[segments.length - 1];
    if (last && last.type === r.type) {
      last.text += " " + r.word;
    } else {
      segments.push({ text: r.word, type: r.type });
    }
  }
  return segments;
}

/* ─── Transcript with correction highlights ──────────────────────────── */

function renderTranscriptWithCorrections(
  text: string,
  corrections: TranscriptCorrection[],
  showCorrections: boolean,
  editingId: string | null,
  setEditingId: (id: string | null) => void,
  onRevert: (id: string) => void,
  onEdit: (id: string, val: string) => void,
): React.ReactNode {
  if (!showCorrections || !corrections.length) {
    return <span className="text-foreground">{text}</span>;
  }

  // Sort corrections by offset, filter to those within text bounds
  const sorted = [...corrections]
    .filter((c) => c.offset >= 0 && c.offset + c.corrected.length <= text.length)
    .sort((a, b) => a.offset - b.offset);

  if (!sorted.length) return <span className="text-foreground">{text}</span>;

  const nodes: React.ReactNode[] = [];
  let cursor = 0;

  for (const c of sorted) {
    if (c.offset < cursor) continue; // overlapping correction, skip
    if (c.offset > cursor) {
      nodes.push(<span key={`t-${cursor}`} className="text-foreground">{text.slice(cursor, c.offset)}</span>);
    }
    const end = c.offset + c.corrected.length;
    if (editingId === c.id) {
      nodes.push(
        <input
          key={`e-${c.id}`}
          autoFocus
          defaultValue={c.corrected}
          className="rounded border border-theme-accent bg-background px-1 text-sm"
          onBlur={(e) => { onEdit(c.id, e.target.value); setEditingId(null); }}
          onKeyDown={(e) => {
            if (e.key === "Enter") { onEdit(c.id, e.currentTarget.value); setEditingId(null); }
            if (e.key === "Escape") setEditingId(null);
          }}
        />
      );
    } else {
      nodes.push(
        <span
          key={`c-${c.id}`}
          className="rounded bg-theme-accent/15 text-theme-accent underline decoration-dotted cursor-pointer hover:bg-theme-accent/25 transition-colors"
          title={`Korrigiert von: "${c.original}" — Klick: rückgängig, Doppelklick: bearbeiten`}
          onClick={() => onRevert(c.id)}
          onDoubleClick={() => setEditingId(c.id)}
        >
          {c.corrected}
        </span>
      );
    }
    cursor = end;
  }
  if (cursor < text.length) {
    nodes.push(<span key={`t-end`} className="text-foreground">{text.slice(cursor)}</span>);
  }
  return <>{nodes}</>;
}

function DiffView({ comparison, onClear }: { comparison: CompareTranscript; onClear?: () => void }) {
  const segments = computeWordDiff(comparison.confirmed_a, comparison.confirmed_b);
  const hasDiff = segments.some((s) => s.type !== "same");

  return (
    <div className="space-y-3">
      {/* Diff header */}
      <div className="flex items-center gap-3 text-xs">
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2 w-2 rounded-full bg-danger/70" />
          <span className="text-muted-foreground">Nur {comparison.model_a}</span>
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2 w-2 rounded-full bg-success/70" />
          <span className="text-muted-foreground">Nur {comparison.model_b}</span>
        </span>
        {!hasDiff && (
          <span className="text-xs text-success">Identisch ✔</span>
        )}
        {onClear && (
          <button
            onClick={onClear}
            className="ml-auto text-xs text-muted-foreground hover:text-danger transition-colors"
            title="Vergleich löschen"
          >
            Vergleich löschen
          </button>
        )}
      </div>

      {/* Side-by-side panels */}
      <div className="grid grid-cols-2 gap-3">
        {/* Model A */}
        <Card className="border-border bg-surface">
          <div className="px-3 py-1.5 text-[10px] font-medium uppercase tracking-widest text-muted-foreground border-b border-border">
            {comparison.model_a}
          </div>
          <CardContent className="max-h-[300px] overflow-y-auto p-3">
            <p className="whitespace-pre-wrap text-sm leading-6">
              {comparison.confirmed_a || <span className="italic text-text-dim">—</span>}
              {comparison.provisional_a && (
                <span className="text-provisional italic"> {comparison.provisional_a}</span>
              )}
            </p>
          </CardContent>
        </Card>

        {/* Model B */}
        <Card className="border-border bg-surface">
          <div className="px-3 py-1.5 text-[10px] font-medium uppercase tracking-widest text-muted-foreground border-b border-border">
            {comparison.model_b}
          </div>
          <CardContent className="max-h-[300px] overflow-y-auto p-3">
            <p className="whitespace-pre-wrap text-sm leading-6">
              {comparison.confirmed_b || <span className="italic text-text-dim">—</span>}
              {comparison.provisional_b && (
                <span className="text-provisional italic"> {comparison.provisional_b}</span>
              )}
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Unified diff view */}
      {hasDiff && (
        <Card className="border-border bg-surface">
          <div className="px-3 py-1.5 text-[10px] font-medium uppercase tracking-widest text-muted-foreground border-b border-border">
            Unterschiede
          </div>
          <CardContent className="max-h-[200px] overflow-y-auto p-3">
            <p className="whitespace-pre-wrap text-sm leading-6">
              {segments.map((seg, i) => (
                <span
                  key={i}
                  className={
                    seg.type === "removed"
                      ? "bg-danger/15 text-danger line-through decoration-danger/40"
                      : seg.type === "added"
                      ? "bg-success/15 text-success"
                      : "text-foreground"
                  }
                >
                  {i > 0 ? " " : ""}{seg.text}
                </span>
              ))}
            </p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

type FileState =
  | { status: "idle" }
  | { status: "streaming"; name: string }
  | { status: "done";      name: string }
  | { status: "error";     message: string };

const CUSTOM_AGENTS_KEY = "dictation_custom_agents";

// Built-in voice commands (mirrors server.py VOICE_COMMANDS — informational only)
const BUILTIN_VC: { spoken: string; replacement: string }[] = [
  { spoken: "punkt",          replacement: "." },
  { spoken: "komma",          replacement: "," },
  { spoken: "doppelpunkt",    replacement: ":" },
  { spoken: "semikolon",      replacement: ";" },
  { spoken: "fragezeichen",   replacement: "?" },
  { spoken: "ausrufezeichen", replacement: "!" },
  { spoken: "neue zeile",     replacement: "\n" },
  { spoken: "neuer absatz",   replacement: "\n\n" },
  { spoken: "bindestrich",    replacement: "-" },
  { spoken: "schrägstrich",   replacement: "/" },
  { spoken: "klammer auf",    replacement: "(" },
  { spoken: "klammer zu",     replacement: ")" },
];

// Built-in action commands (mirrors server.py ACTION_COMMANDS)
const BUILTIN_ACTIONS = [
  "mach einen bericht",
  "erstelle bericht",
  "bericht erstellen",
  "generiere bericht",
  "soap erstellen",
  "soap note erstellen",
  "zeige soap",
];

const VC_PRESET_REPLACEMENTS = [
  { label: ". (Punkt)",       value: "." },
  { label: ", (Komma)",       value: "," },
  { label: ": (Doppelpunkt)", value: ":" },
  { label: "↵ (Neue Zeile)",  value: "\n" },
  { label: "↵↵ (Absatz)",     value: "\n\n" },
  { label: "Eigener Text…",   value: "__custom__" },
];

function displayReplacement(r: string): string {
  return r.replace(/\n\n/g, "↵↵").replace(/\n/g, "↵");
}

function loadCustomAgents(): { id: string; name: string }[] {
  try {
    return JSON.parse(localStorage.getItem(CUSTOM_AGENTS_KEY) ?? "[]");
  } catch {
    return [];
  }
}

export default function DictationPage() {
  const d = useDictation();
  const fileRef = useRef<HTMLInputElement>(null);
  const transcriptRef = useRef<HTMLDivElement>(null);
  const [fileState, setFileState] = useState<FileState>({ status: "idle" });
  const [soapLoading, setSoapLoading] = useState(false);
  const [soapError, setSoapError] = useState("");
  const [goldName, setGoldName] = useState("");
  const [goldSaving, setGoldSaving] = useState(false);
  const [goldStatus, setGoldStatus] = useState<{ ok: boolean; message: string } | null>(null);

  // Custom (user-added) agents stored in localStorage
  const [customAgents, setCustomAgents] = useState<{ id: string; name: string }[]>([]);
  const [showAddAgent, setShowAddAgent] = useState(false);
  const [newAgentId, setNewAgentId] = useState("");
  const [newAgentName, setNewAgentName] = useState("");

  // Voice commands panel
  const [showVcPanel, setShowVcPanel] = useState(false);
  const [newVcSpoken, setNewVcSpoken] = useState("");
  const [newVcReplacement, setNewVcReplacement] = useState(".");
  const [newVcCustomText, setNewVcCustomText] = useState("");

  // Correction inline-edit state + Shift-held to reveal highlights
  const [editingCorrectionId, setEditingCorrectionId] = useState<string | null>(null);
  const [shiftHeld, setShiftHeld] = useState(false);

  useEffect(() => {
    const onDown = (e: KeyboardEvent) => { if (e.key === "Shift") setShiftHeld(true); };
    const onUp   = (e: KeyboardEvent) => { if (e.key === "Shift") setShiftHeld(false); };
    window.addEventListener("keydown", onDown);
    window.addEventListener("keyup",   onUp);
    return () => { window.removeEventListener("keydown", onDown); window.removeEventListener("keyup", onUp); };
  }, []);

  // Load custom agents from localStorage on mount
  useEffect(() => {
    setCustomAgents(loadCustomAgents());
  }, []);

  const allAgents = [
    ...d.ragflowAgents,
    ...customAgents.filter((ca) => !d.ragflowAgents.some((a) => a.id === ca.id)),
  ];

  const handleAddAgent = () => {
    const id = newAgentId.trim();
    const name = newAgentName.trim() || id;
    if (!id) return;
    const updated = [...customAgents.filter((a) => a.id !== id), { id, name }];
    setCustomAgents(updated);
    localStorage.setItem(CUSTOM_AGENTS_KEY, JSON.stringify(updated));
    d.setSelectedAgent(id);
    setNewAgentId("");
    setNewAgentName("");
    setShowAddAgent(false);
  };

  const handleRemoveCustomAgent = (id: string) => {
    const updated = customAgents.filter((a) => a.id !== id);
    setCustomAgents(updated);
    localStorage.setItem(CUSTOM_AGENTS_KEY, JSON.stringify(updated));
    if (d.selectedAgent === id) d.setSelectedAgent(allAgents.find((a) => a.id !== id)?.id ?? "");
  };

  const handleAddVoiceCommand = () => {
    const spoken = newVcSpoken.trim().toLowerCase();
    if (!spoken) return;
    const replacement = newVcReplacement === "__custom__" ? newVcCustomText : newVcReplacement;
    if (!replacement) return;
    const updated: VoiceCommand[] = [
      ...d.voiceCommands.filter((c) => c.spoken !== spoken),
      { spoken, replacement },
    ];
    d.setVoiceCommands(updated);
    setNewVcSpoken("");
    setNewVcCustomText("");
  };

  const handleRemoveVoiceCommand = (spoken: string) => {
    d.setVoiceCommands(d.voiceCommands.filter((c) => c.spoken !== spoken));
  };

  const busy         = d.recording || d.streaming;
  const hasTranscript = !!(d.transcript.confirmed || d.transcript.provisional || d.fullText);
  const hasConfirmed  = !!(d.transcript.confirmed.trim() || d.fullText.trim());
  const hasAnySoap    = Object.values(d.fields).some((v) => v.trim());

  // Auto-scroll transcript box to bottom as new text arrives
  useEffect(() => {
    if (transcriptRef.current) {
      transcriptRef.current.scrollTop = transcriptRef.current.scrollHeight;
    }
  }, [d.transcript.confirmed, d.transcript.provisional]);

  const handleFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setFileState({ status: "streaming", name: file.name });
    const result = await d.streamFile(file);
    setFileState(
      result.ok
        ? { status: "done",  name: file.name }
        : { status: "error", message: result.error ?? "Unbekannter Fehler" }
    );
    if (fileRef.current) fileRef.current.value = "";
  };

  const handleGenerateSOAP = async () => {
    const text = d.fullText || d.transcript.confirmed;
    if (!text.trim()) return;
    setSoapLoading(true);
    setSoapError("");
    const result = await d.generateSoap(text, d.selectedAgent || undefined);
    if (!result.ok) setSoapError(result.error ?? "SOAP-Generierung fehlgeschlagen");
    setSoapLoading(false);
  };

  const handleSaveGoldStandard = async () => {
    if (!goldName.trim() || !hasAnySoap) return;
    setGoldSaving(true);
    setGoldStatus(null);
    const result = await d.saveGoldStandard(goldName.trim());
    if (result.ok) {
      setGoldStatus({ ok: true, message: `Goldstandard "${result.slug}" gespeichert` });
      setGoldName("");
    } else {
      setGoldStatus({ ok: false, message: result.error ?? "Speichern fehlgeschlagen" });
    }
    setGoldSaving(false);
  };

  return (
    <div className="flex min-h-screen flex-col items-center bg-transparent px-4 py-10 text-foreground">
      <ThemeSwitcher />

      {/* Header */}
      <header className="mb-8 text-center">
        <h1 className="text-2xl font-light tracking-wide">
          <span className="font-semibold text-theme-accent">vitodata</span>{" "}
          <span className="text-text-dim">·</span> Live Diktat
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Streaming-Transkription mit Sliding-Window-Whisper &amp; Mistral-Korrektur
        </p>
      </header>

      <div className="w-full max-w-5xl space-y-5">

        {/* ─── SECTION 1: Live Transcription ─────────────────────────── */}

        {/* Controls row */}
        <div className="flex items-center gap-4">
          {/* Mic button */}
          <button
            onClick={d.toggleRecording}
            disabled={d.streaming}
            aria-label={d.recording ? "Aufnahme stoppen" : "Aufnahme starten"}
            className={`group relative flex h-14 w-14 shrink-0 items-center justify-center rounded-full border-2 backdrop-blur-md transition-all disabled:opacity-40 disabled:cursor-not-allowed
              ${
                d.recording
                  ? "border-danger bg-danger/10 shadow-[0_0_0_4px_rgba(248,113,113,0.14)] animate-[pulse-ring_2s_ease-in-out_infinite]"
                  : "border-border bg-white/5 hover:border-theme-accent hover:bg-theme-accent/8 hover:scale-105"
              }`}
          >
            <div
              className={`transition-all bg-danger ${
                d.recording ? "h-4 w-4 rounded-sm" : "h-5 w-5 rounded-full"
              }`}
            />
          </button>

          {/* Status */}
          <div className="min-w-0 flex-1">
            <p
              className={`text-sm font-medium ${
                d.recording
                  ? "text-danger"
                  : d.streaming
                  ? "text-theme-accent"
                  : d.connected
                  ? "text-success"
                  : "text-muted-foreground"
              }`}
            >
              {d.recording
                ? "Aufnahme läuft…"
                : d.streaming
                ? d.streamStatus
                  ? `${d.streamStatus} (${d.streamElapsed.toFixed(0)}s / ${d.streamDuration.toFixed(0)}s)`
                  : `Datei wird gestreamt… ${d.streamElapsed.toFixed(0)}s / ${d.streamDuration.toFixed(0)}s`
                : d.connected
                ? "Verbunden — bereit"
                : "Verbindung wird hergestellt…"}
            </p>
          </div>

          {/* Level meter */}
          {d.recording && (
            <div className="flex h-6 items-end gap-[2px]">
              {d.level.map((v, i) => (
                <div
                  key={i}
                  className="w-[3px] rounded-sm bg-theme-accent transition-all duration-75"
                  style={{ height: Math.max(2, v * 24) }}
                />
              ))}
            </div>
          )}

          {/* Streaming pulse + stop button */}
          {d.streaming && (
            <div className="flex items-center gap-3">
              <div className="flex h-6 items-end gap-[2px]">
                {Array.from({ length: 12 }, (_, i) => (
                  <div
                    key={i}
                    className="stream-bar w-[3px] rounded-sm bg-theme-accent/60 origin-bottom"
                    style={{
                      height: 6 + (i % 4) * 4,
                      animationDuration: `${0.55 + i * 0.06}s`,
                      animationDelay: `${i * 0.04}s`,
                    }}
                  />
                ))}
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => { d.stopFileStream(); setFileState({ status: "idle" }); }}
                className="border-danger/50 text-danger hover:bg-danger/10 hover:border-danger text-xs"
              >
                Stop
              </Button>
            </div>
          )}
        </div>

        {/* Live transcript box */}
        <Card
          className={`relative overflow-visible border-border bg-surface transition-all ${
            busy ? "ring-1 ring-theme-accent/50 shadow-[0_0_24px_var(--accent-glow)]" : ""
          }`}
        >
          <div className="absolute -top-2.5 left-4 bg-background/80 backdrop-blur-sm px-2 text-[10px] font-medium uppercase tracking-widest text-muted-foreground">
            Transkription
          </div>
          <CardContent ref={transcriptRef} className="max-h-[400px] min-h-[180px] overflow-y-auto p-5 pt-4" aria-live="polite">
            {!hasTranscript ? (
              <p className="italic text-text-dim">
                Klicke auf den Aufnahme-Knopf oder lade eine Datei hoch&hellip;
              </p>
            ) : (
              <p className="whitespace-pre-wrap text-[1.05rem] leading-7">
                {/* Full accumulated text with correction highlights */}
                {d.fullText ? renderTranscriptWithCorrections(
                  d.fullText,
                  d.corrections,
                  shiftHeld,
                  editingCorrectionId,
                  setEditingCorrectionId,
                  d.revertCorrection,
                  d.editCorrection,
                ) : d.transcript.confirmed ? (
                  <span className="text-foreground">{d.transcript.confirmed}</span>
                ) : null}
                {d.transcript.provisional && (
                  <>
                    {" "}
                    <span className="text-provisional italic underline decoration-provisional/30 underline-offset-4">
                      {d.transcript.provisional}
                    </span>
                  </>
                )}
                {busy && (
                  <span className="ml-0.5 inline-block h-[1.1em] w-[2px] animate-pulse bg-theme-accent align-text-bottom" />
                )}
              </p>
            )}
          </CardContent>
        </Card>

        {/* Legend + clear transcript + mode toggles */}
        <div className="flex flex-wrap items-center gap-4 text-xs text-muted-foreground">
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-2 w-2 rounded-full bg-foreground" />
            Bestätigt
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block h-2 w-2 rounded-full bg-provisional" />
            Provisorisch
          </span>
          {d.corrections.length > 0 && (
            <span className={`text-[10px] transition-colors ${shiftHeld ? "text-theme-accent" : "text-muted-foreground/60"}`}>
              {shiftHeld ? "●" : "○"} {d.corrections.length} KI-Korrektur{d.corrections.length !== 1 ? "en" : ""} — Shift halten
            </span>
          )}

          <div className="ml-auto flex items-center gap-2">
            {/* Whisper-only toggle */}
            <Button
              type="button"
              variant={d.whisperOnly ? "default" : "outline"}
              size="sm"
              className={`text-xs ${
                d.whisperOnly
                  ? "bg-theme-accent text-background hover:bg-theme-accent/80"
                  : "border-border text-muted-foreground hover:border-theme-accent hover:text-theme-accent"
              }`}
              onClick={d.toggleWhisperOnly}
              disabled={busy}
            >
              {d.whisperOnly ? "Nur Whisper aktiv" : "Nur Whisper"}
            </Button>

            {/* Live SOAP toggle */}
            <Button
              type="button"
              variant={d.liveSoap ? "default" : "outline"}
              size="sm"
              className={`text-xs ${
                d.liveSoap
                  ? "bg-theme-accent text-background hover:bg-theme-accent/80"
                  : "border-border text-muted-foreground hover:border-theme-accent hover:text-theme-accent"
              }`}
              onClick={d.toggleLiveSoap}
              disabled={busy}
            >
              {d.liveSoap ? "Live SOAP aktiv" : "Live SOAP"}
            </Button>

            {/* Compare toggle */}
            {d.compareEnabled && (
              <Button
                type="button"
                variant={d.compareMode ? "default" : "outline"}
                size="sm"
                className={`text-xs ${
                  d.compareMode
                    ? "bg-theme-accent text-background hover:bg-theme-accent/80"
                    : "border-border text-muted-foreground hover:border-theme-accent hover:text-theme-accent"
                }`}
                onClick={d.toggleCompareMode}
                disabled={busy}
              >
                {d.compareMode ? "Vergleich aktiv" : "Modelle vergleichen"}
              </Button>
            )}

            {/* Voice commands panel toggle */}
            <Button
              type="button"
              variant={showVcPanel ? "default" : "outline"}
              size="sm"
              className={`text-xs ${
                showVcPanel
                  ? "bg-theme-accent text-background hover:bg-theme-accent/80"
                  : "border-border text-muted-foreground hover:border-theme-accent hover:text-theme-accent"
              }`}
              onClick={() => setShowVcPanel((v) => !v)}
            >
              Sprachbefehle
            </Button>

            {/* Clear transcript button */}
            <Button
              variant="ghost"
              size="sm"
              className="text-xs text-muted-foreground hover:text-danger"
              onClick={d.clearTranscript}
              disabled={!hasConfirmed || busy}
            >
              Löschen
            </Button>

            {/* Clear all button */}
            <Button
              variant="ghost"
              size="sm"
              className="text-xs text-muted-foreground hover:text-danger"
              onClick={d.clearAll}
              disabled={(!hasConfirmed && !d.comparison) || busy}
            >
              Alles löschen
            </Button>
          </div>
        </div>

        {/* Comparison diff view */}
        {d.compareMode && d.comparison && <DiffView comparison={d.comparison} onClear={d.clearComparison} />}

        {/* Voice commands panel */}
        {showVcPanel && (
          <div className="space-y-4 rounded-lg border border-dashed border-theme-accent/40 bg-theme-accent/5 px-4 py-4">
            <h3 className="text-xs font-medium uppercase tracking-widest text-muted-foreground">
              Sprachbefehle
            </h3>

            {/* Built-in replacement commands */}
            <div className="space-y-1.5">
              <p className="text-[10px] uppercase tracking-widest text-muted-foreground/70">
                Integriert — Ersetzungen
              </p>
              <div className="flex flex-wrap gap-1.5">
                {BUILTIN_VC.map((cmd) => (
                  <span
                    key={cmd.spoken}
                    className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-0.5 text-xs text-muted-foreground"
                  >
                    <span className="font-medium text-foreground/70">{cmd.spoken}</span>
                    <span className="text-text-dim">→</span>
                    <span className="font-mono">{displayReplacement(cmd.replacement)}</span>
                  </span>
                ))}
              </div>
            </div>

            {/* Built-in action commands */}
            <div className="space-y-1.5">
              <p className="text-[10px] uppercase tracking-widest text-muted-foreground/70">
                Integriert — Aktionen (löst SOAP-Generierung aus)
              </p>
              <div className="flex flex-wrap gap-1.5">
                {BUILTIN_ACTIONS.map((cmd) => (
                  <span
                    key={cmd}
                    className="inline-flex items-center gap-1.5 rounded-md border border-theme-accent/30 bg-theme-accent/5 px-2 py-0.5 text-xs text-theme-accent"
                  >
                    {cmd} → SOAP
                  </span>
                ))}
              </div>
            </div>

            {/* Custom commands */}
            {d.voiceCommands.length > 0 && (
              <div className="space-y-1.5">
                <p className="text-[10px] uppercase tracking-widest text-muted-foreground/70">
                  Eigene Befehle
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {d.voiceCommands.map((cmd) => (
                    <span
                      key={cmd.spoken}
                      className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-0.5 text-xs"
                    >
                      <span className="font-medium text-foreground">{cmd.spoken}</span>
                      <span className="text-text-dim">→</span>
                      <span className="font-mono text-foreground/80">{displayReplacement(cmd.replacement)}</span>
                      <button
                        type="button"
                        onClick={() => handleRemoveVoiceCommand(cmd.spoken)}
                        className="ml-0.5 text-muted-foreground/50 hover:text-danger"
                        aria-label={`Befehl '${cmd.spoken}' entfernen`}
                      >✕</button>
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* Diff hotkey config */}
            <div className="flex items-center gap-2 border-t border-border pt-3 text-xs text-muted-foreground">
              <span>Diff-Ansicht:</span>
              <kbd className="rounded border border-border px-1.5 py-0.5 font-mono text-foreground">Alt+</kbd>
              <input
                type="text"
                maxLength={1}
                value={d.diffHotkey}
                onChange={(e) => e.target.value && d.setDiffHotkey(e.target.value)}
                className="h-6 w-8 rounded border border-border bg-background text-center text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-theme-accent"
                title="Taste für Diff-Ansicht"
              />
            </div>

            {/* Add custom command form */}
            <div className="flex flex-wrap items-end gap-2">
              <div className="flex flex-col gap-1">
                <label className="text-[10px] font-medium uppercase tracking-widest text-muted-foreground">
                  Gesprochenes Wort
                </label>
                <input
                  type="text"
                  value={newVcSpoken}
                  onChange={(e) => setNewVcSpoken(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && handleAddVoiceCommand()}
                  placeholder="z.B. absatz"
                  className="h-8 w-40 rounded-md border border-border bg-background px-3 text-sm text-foreground placeholder:text-text-dim/50 focus:outline-none focus:ring-1 focus:ring-theme-accent"
                />
              </div>
              <div className="flex flex-col gap-1">
                <label className="text-[10px] font-medium uppercase tracking-widest text-muted-foreground">
                  Ersetzung
                </label>
                <select
                  value={newVcReplacement}
                  onChange={(e) => setNewVcReplacement(e.target.value)}
                  className="h-8 rounded-md border border-border bg-surface px-2 text-sm text-foreground focus:outline-none focus:ring-1 focus:ring-theme-accent"
                >
                  {VC_PRESET_REPLACEMENTS.map((opt) => (
                    <option key={opt.value} value={opt.value}>{opt.label}</option>
                  ))}
                </select>
              </div>
              {newVcReplacement === "__custom__" && (
                <div className="flex flex-col gap-1">
                  <label className="text-[10px] font-medium uppercase tracking-widest text-muted-foreground">
                    Eigener Text
                  </label>
                  <input
                    type="text"
                    value={newVcCustomText}
                    onChange={(e) => setNewVcCustomText(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && handleAddVoiceCommand()}
                    placeholder="Ersatztext"
                    className="h-8 w-40 rounded-md border border-border bg-background px-3 text-sm text-foreground placeholder:text-text-dim/50 focus:outline-none focus:ring-1 focus:ring-theme-accent"
                    autoFocus
                  />
                </div>
              )}
              <Button
                size="sm"
                disabled={!newVcSpoken.trim() || (newVcReplacement === "__custom__" && !newVcCustomText.trim())}
                onClick={handleAddVoiceCommand}
                className="shrink-0 bg-theme-accent text-background hover:bg-theme-accent/80 disabled:opacity-40"
              >
                Hinzufügen
              </Button>
            </div>
          </div>
        )}

        {/* Upload row */}
        <div className="space-y-2">
          <input
            ref={fileRef}
            type="file"
            accept=".wav,.mp3,.ogg,.m4a,.flac,.webm,.mp4,.wma,.aac,audio/*"
            aria-label="Audiodatei auswählen"
            className="hidden"
            onChange={handleFile}
          />
          <div className="flex items-center gap-3">
            <Button
              variant="outline"
              size="sm"
              disabled={busy}
              onClick={() => fileRef.current?.click()}
              className="border-dashed border-border text-muted-foreground hover:border-theme-accent hover:text-theme-accent disabled:opacity-40"
            >
              Datei hochladen
            </Button>
            {fileState.status === "idle" && (
              <span className="text-xs text-muted-foreground">WAV, MP3, OGG, M4A, FLAC</span>
            )}
          </div>

          {/* File status panel */}
          {fileState.status !== "idle" && (
            <div className={`flex items-center gap-3 rounded-lg border px-3 py-2 text-sm transition-all ${
              fileState.status === "done"
                ? "border-success/30 bg-success/5 text-success"
                : fileState.status === "streaming"
                ? "border-theme-accent/30 bg-theme-accent/5 text-theme-accent"
                : "border-danger/30 bg-danger/5 text-danger"
            }`}>
              {fileState.status === "streaming" && (
                <span className="relative flex h-3 w-3 shrink-0">
                  <span className="absolute inline-flex h-3 w-3 animate-ping rounded-full bg-theme-accent opacity-60" />
                  <span className="relative inline-flex h-3 w-3 rounded-full bg-theme-accent" />
                </span>
              )}
              {fileState.status === "done"  && <span className="shrink-0">✔</span>}
              {fileState.status === "error" && <span className="shrink-0">✕</span>}
              <span className="min-w-0 truncate">
                {fileState.status === "streaming" && (d.streamStatus || `${fileState.name} wird transkribiert…`)}
                {fileState.status === "done"      && `${fileState.name} — fertig`}
                {fileState.status === "error"     && fileState.message}
              </span>
              {fileState.status !== "streaming" && (
                <button
                  type="button"
                  onClick={() => setFileState({ status: "idle" })}
                  className="ml-auto shrink-0 text-xs opacity-50 hover:opacity-100"
                  aria-label="Meldung schliessen"
                >✕</button>
              )}
            </div>
          )}
        </div>

        <Separator className="bg-border" />

        {/* ─── SECTION 2: SOAP Notes via RAGFlow ────────────────────── */}

        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <h2 className="text-sm font-medium uppercase tracking-widest text-muted-foreground">
              SOAP-Notizen
            </h2>
            {/* RAGFlow connection indicator */}
            <span
              className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[10px] font-medium ${
                d.ragflowConnected === null
                  ? "border-border text-muted-foreground"
                  : d.ragflowConnected
                  ? "border-success/30 text-success"
                  : "border-danger/30 text-danger"
              }`}
              title={
                d.ragflowConnected === null
                  ? "RAGFlow-Verbindung wird geprüft…"
                  : d.ragflowConnected
                  ? "RAGFlow erreichbar"
                  : "RAGFlow nicht erreichbar"
              }
            >
              <span className={`inline-block h-1.5 w-1.5 rounded-full ${
                d.ragflowConnected === null ? "bg-muted-foreground animate-pulse"
                : d.ragflowConnected ? "bg-success" : "bg-danger"
              }`} />
              {d.ragflowConnected === null ? "Agent…" : d.ragflowConnected ? "Agent verbunden" : "Agent offline"}
            </span>
          </div>
          <div className="flex items-center gap-2">
            {allAgents.length > 0 && (
              <div className="flex items-center gap-1">
                <select
                  value={d.selectedAgent}
                  onChange={(e) => d.setSelectedAgent(e.target.value)}
                  className="h-8 rounded-md border border-border bg-surface px-2 text-xs text-foreground focus:outline-none focus:ring-1 focus:ring-theme-accent"
                  aria-label="RAGFlow Agent auswählen"
                >
                  {allAgents.map((a) => (
                    <option key={a.id} value={a.id}>{a.name}</option>
                  ))}
                </select>
                {/* Remove button for custom agents */}
                {customAgents.some((a) => a.id === d.selectedAgent) && (
                  <button
                    type="button"
                    onClick={() => handleRemoveCustomAgent(d.selectedAgent)}
                    className="flex h-8 w-8 items-center justify-center rounded-md border border-danger/40 text-danger/60 hover:bg-danger/10 hover:text-danger text-sm"
                    aria-label="Agent entfernen"
                    title="Diesen Agent entfernen"
                  >✕</button>
                )}
              </div>
            )}
            {/* Add agent button */}
            <button
              type="button"
              onClick={() => setShowAddAgent((v) => !v)}
              className={`flex h-8 w-8 items-center justify-center rounded-md border text-sm transition-colors ${
                showAddAgent
                  ? "border-theme-accent bg-theme-accent/10 text-theme-accent"
                  : "border-border text-muted-foreground hover:border-theme-accent hover:text-theme-accent"
              }`}
              aria-label="Agent hinzufügen"
              title="RAGFlow Agent hinzufügen"
            >+</button>
            <Button
              size="sm"
              disabled={!hasConfirmed || busy || soapLoading}
              onClick={handleGenerateSOAP}
              className="bg-theme-accent text-background hover:bg-theme-accent/80 disabled:opacity-40"
            >
              {soapLoading ? "Wird generiert…" : "SOAP generieren"}
            </Button>
          </div>
        </div>

        {/* Add agent inline form */}
        {showAddAgent && (
          <div className="flex items-end gap-2 rounded-lg border border-dashed border-theme-accent/40 bg-theme-accent/5 px-4 py-3">
            <div className="flex flex-1 flex-col gap-1">
              <label className="text-[10px] font-medium uppercase tracking-widest text-muted-foreground">
                Agent-ID
              </label>
              <input
                type="text"
                value={newAgentId}
                onChange={(e) => setNewAgentId(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleAddAgent()}
                placeholder="z.B. 5cb3f0a0e2f711ef…"
                className="h-8 rounded-md border border-border bg-background px-3 text-sm text-foreground placeholder:text-text-dim/50 focus:outline-none focus:ring-1 focus:ring-theme-accent"
                autoFocus
              />
            </div>
            <div className="flex flex-1 flex-col gap-1">
              <label className="text-[10px] font-medium uppercase tracking-widest text-muted-foreground">
                Anzeigename (optional)
              </label>
              <input
                type="text"
                value={newAgentName}
                onChange={(e) => setNewAgentName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && handleAddAgent()}
                placeholder="z.B. Medical Facts Agent"
                className="h-8 rounded-md border border-border bg-background px-3 text-sm text-foreground placeholder:text-text-dim/50 focus:outline-none focus:ring-1 focus:ring-theme-accent"
              />
            </div>
            <Button
              size="sm"
              disabled={!newAgentId.trim()}
              onClick={handleAddAgent}
              className="shrink-0 bg-theme-accent text-background hover:bg-theme-accent/80 disabled:opacity-40"
            >
              Hinzufügen
            </Button>
            <button
              type="button"
              onClick={() => { setShowAddAgent(false); setNewAgentId(""); setNewAgentName(""); }}
              className="shrink-0 text-xs text-muted-foreground hover:text-foreground"
              aria-label="Abbrechen"
            >
              Abbrechen
            </button>
          </div>
        )}

        {/* SOAP error message */}
        {soapError && (
          <div className="flex items-center gap-2 rounded-lg border border-danger/30 bg-danger/5 px-3 py-2 text-sm text-danger">
            <span className="shrink-0">✕</span>
            <span className="min-w-0">{soapError}</span>
            <button
              type="button"
              onClick={() => setSoapError("")}
              className="ml-auto shrink-0 text-xs opacity-50 hover:opacity-100"
              aria-label="Fehlermeldung schliessen"
            >✕</button>
          </div>
        )}

        {/* SOAP quality scores + timer */}
        {(d.soapScores || d.soapDuration != null || d.soapGenerating) && (
          <div className="flex items-center gap-4 rounded-lg border border-border bg-surface/80 px-4 py-2.5 text-sm">
            <span className="text-xs font-medium uppercase tracking-widest text-muted-foreground">Qualität</span>
            {d.soapScores && <ScoreBadge label="Treue" value={d.soapScores.faithfulness} />}
            {d.soapScores && <ScoreBadge label="Vollständigkeit" value={d.soapScores.completeness} />}
            {d.soapDuration != null && (
              <span className={`ml-auto text-xs ${d.soapGenerating ? "animate-pulse text-theme-accent" : "text-muted-foreground"}`}>
                {d.soapGenerating ? "⏱ " : ""}
                {d.soapDuration >= 1000
                  ? `${(d.soapDuration / 1000).toFixed(1)}s`
                  : `${d.soapDuration}ms`}
              </span>
            )}
          </div>
        )}

        {/* SOAP 2×2 grid */}
        <div className="grid grid-cols-2 gap-3">
          {(Object.keys(SOAP_LABELS) as SoapField[]).map((f) => {
            const lbl = SOAP_LABELS[f];
            const isDirty = d.dirtyFields.has(f);
            const hasLlm  = !!d.llmFields[f]?.trim();
            return (
              <Card key={f} className="relative overflow-visible border-border bg-surface">
                <div className="absolute -top-2.5 left-3 right-3 flex items-center bg-background/80 backdrop-blur-sm px-2">
                  <span className="text-[10px] font-medium uppercase tracking-widest text-muted-foreground">
                    {lbl.short} · {lbl.label}
                    {d.fields[f] && (
                      <span className="ml-1.5 inline-block h-1.5 w-1.5 rounded-full bg-theme-accent align-middle" />
                    )}
                  </span>
                  {isDirty && (
                    <button
                      type="button"
                      onClick={() => d.unlockField(f)}
                      title="Feld für automatische Aktualisierung freigeben"
                      className="ml-auto flex items-center gap-1 text-[10px] text-theme-accent/70 hover:text-theme-accent transition-colors"
                    >
                      <Lock className="h-2.5 w-2.5" />
                      <span>gesperrt</span>
                    </button>
                  )}
                </div>
                <CardContent className="p-3 pt-4">
                  {/* Mode C — inline diff (only when dirty AND has LLM version) */}
                  {d.diffMode && isDirty && hasLlm ? (
                    <div className="min-h-[100px] max-h-[400px] overflow-y-auto whitespace-pre-wrap text-sm leading-6 py-1">
                      {computeWordDiff(d.llmFields[f], d.fields[f]).map((seg, i) => (
                        <span
                          key={i}
                          className={
                            seg.type === "removed"
                              ? "bg-danger/15 text-danger line-through decoration-danger/40"
                              : seg.type === "added"
                              ? "bg-success/15 text-success"
                              : "text-foreground"
                          }
                        >
                          {i > 0 ? " " : ""}{seg.text}
                        </span>
                      ))}
                    </div>
                  ) : (
                    <Textarea
                      value={d.fields[f]}
                      onChange={(e) => d.setFieldText(f, e.target.value)}
                      placeholder={`${lbl.label}…`}
                      className="min-h-[100px] max-h-[400px] resize-y overflow-y-auto border-none bg-transparent text-sm leading-6 placeholder:text-text-dim/50 focus-visible:ring-0"
                    />
                  )}
                  {/* Mode B — collapsed LLM suggestion (default, when dirty and not in diff mode) */}
                  {!d.diffMode && isDirty && hasLlm && (
                    <details className="mt-2 border-t border-border pt-2">
                      <summary className="cursor-pointer text-[10px] uppercase tracking-widest text-muted-foreground hover:text-foreground transition-colors">
                        KI-Vorschlag anzeigen
                      </summary>
                      <p className="mt-1.5 whitespace-pre-wrap text-sm text-muted-foreground/70 leading-6">
                        {d.llmFields[f]}
                      </p>
                    </details>
                  )}
                </CardContent>
              </Card>
            );
          })}
        </div>

        {/* Diff mode status + hotkey hint */}
        {d.dirtyFields.size > 0 && (
          <p className="text-[10px] text-muted-foreground">
            {d.diffMode ? "Diff-Ansicht aktiv" : "Bearbeitete Felder gesperrt — KI aktualisiert nur offene Felder"}
            {" · "}
            <kbd className="rounded border border-border px-1 py-0.5 font-mono">Alt+{d.diffHotkey}</kbd>
            {" "}{d.diffMode ? "zum Bearbeiten" : "zum Vergleichen"}
          </p>
        )}

        {/* ─── SECTION 3: Save as Gold Standard ─────────────────────── */}

        {hasAnySoap && hasConfirmed && (
          <div className="space-y-2 rounded-lg border border-dashed border-border bg-surface/50 p-4">
            <h3 className="text-xs font-medium uppercase tracking-widest text-muted-foreground">
              Als Goldstandard speichern
            </h3>
            <p className="text-xs text-muted-foreground">
              Speichert das Transkript und die finalen SOAP-Felder als Referenz-Testfall für das RAGAS-Evaluationstool.
            </p>
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={goldName}
                onChange={(e) => setGoldName(e.target.value)}
                placeholder="Testfall-Name (z.B. Diabetes Rückenschmerzen)"
                className="h-8 flex-1 rounded-md border border-border bg-background px-3 text-sm text-foreground placeholder:text-text-dim/50 focus:outline-none focus:ring-1 focus:ring-theme-accent"
                aria-label="Testfall-Name"
              />
              <Button
                size="sm"
                variant="outline"
                disabled={!goldName.trim() || goldSaving}
                onClick={handleSaveGoldStandard}
                className="shrink-0 border-border text-foreground hover:border-theme-accent hover:text-theme-accent disabled:opacity-40"
              >
                {goldSaving ? "Wird gespeichert…" : "Speichern"}
              </Button>
            </div>
            {goldStatus && (
              <div className={`text-xs ${goldStatus.ok ? "text-success" : "text-danger"}`}>
                {goldStatus.message}
              </div>
            )}
          </div>
        )}

        <Separator className="bg-border" />
        <div className="flex flex-wrap gap-x-6 gap-y-1 rounded-lg border border-border bg-surface/80 px-4 py-3 text-xs text-muted-foreground backdrop-blur-md">
          <span>
            <span className="mr-1 opacity-60">Dauer:</span>
            <span className="font-medium tabular-nums text-foreground/80">{d.duration.toFixed(1)}s</span>
          </span>
          <span>
            <span className="mr-1 opacity-60">Chunks:</span>
            <span className="font-medium tabular-nums text-foreground/80">{d.chunks}</span>
          </span>
          <span>
            <span className="mr-1 opacity-60">Whisper-Aufrufe:</span>
            <span className="font-medium tabular-nums text-foreground/80">{d.whisperCalls}</span>
          </span>
          <span>
            <span className="mr-1 opacity-60">Verbindung:</span>
            <Badge
              variant={d.connected ? "default" : "destructive"}
              className="h-4 px-1.5 text-[10px]"
            >
              {d.connected ? "verbunden" : "getrennt"}
            </Badge>
          </span>
        </div>
      </div>
    </div>
  );
}
