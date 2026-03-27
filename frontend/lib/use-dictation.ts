"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { io, Socket } from "socket.io-client";

/* ─── Socket event types ──────────────────────────────────────────────── */

interface TranscriptionEvent {
  confirmed: string;
  provisional: string;
  full_text?: string;
  is_final: boolean;
}

interface FileStreamStartedEvent {
  duration: number;
}

interface TranscriptionErrorEvent {
  error: string;
}

export interface Transcript {
  confirmed: string;
  provisional: string;
}

export interface SoapResult {
  ok: boolean;
  S?: string;
  O?: string;
  A?: string;
  P?: string;
  source?: string;
  duration_ms?: number;
  scores?: SoapScores;
  error?: string;
}

export interface SoapScores {
  faithfulness: number;
  completeness: number;
}

export interface GoldStandardResult {
  ok: boolean;
  slug?: string;
  error?: string;
}

export interface CompareTranscript {
  confirmed_a: string;
  confirmed_b: string;
  provisional_a: string;
  provisional_b: string;
  model_a: string;
  model_b: string;
}

export interface CompareModels {
  models: { id: string; name: string }[];
  compare_enabled: boolean;
}

export interface RagflowAgent {
  id: string;
  name: string;
}

export type SoapField = "subjective" | "objective" | "assessment" | "plan";

const MAX_FILE_SIZE = 100 * 1024 * 1024; // 100 MB
const EMPTY_LEVELS = new Array(12).fill(0) as number[];
const LEVEL_THROTTLE_MS = 50; // ~20fps is plenty for a visual meter

// Connect directly to the backend — Next.js rewrites cannot proxy WebSocket upgrades
function getBackendUrl(): string {
  return (
    process.env.NEXT_PUBLIC_BACKEND_URL ??
    (typeof window !== "undefined"
      ? `${window.location.protocol}//${window.location.hostname}:8003`
      : "http://localhost:8003")
  );
}

export function useDictation() {
  const [connected, setConnected]   = useState(false);
  const [recording, setRecording]   = useState(false);
  const [streaming, setStreaming]   = useState(false);   // file-stream in progress
  const [streamDuration, setStreamDuration] = useState(0); // total file duration (s)
  const [streamElapsed, setStreamElapsed] = useState(0);  // how much has been fed (s)
  const [streamStatus, setStreamStatus] = useState("");   // current processing step

  // Live transcript — populated by Whisper streaming events
  const [transcript, setTranscript] = useState<Transcript>({ confirmed: "", provisional: "" });
  const [fullText, setFullText] = useState("");  // full accumulated transcript across all windows

  // SOAP fields — populated separately (by RAGFlow or manual edit)
  const [fields, setFields] = useState<Record<SoapField, string>>({
    subjective: "",
    objective:  "",
    assessment: "",
    plan:       "",
  });
  const [activeField, setActiveField] = useState<SoapField>("subjective");
  const [soapScores, setSoapScores]   = useState<SoapScores | null>(null);
  const [soapDuration, setSoapDuration] = useState<number | null>(null);
  const [soapGenerating, setSoapGenerating] = useState(false);
  const soapTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const soapStartRef = useRef<number>(0);
  const [compareMode, setCompareMode] = useState(false);
  const [compareEnabled, setCompareEnabled] = useState(false);
  const [comparison, setComparison]   = useState<CompareTranscript | null>(null);
  const [whisperOnly, setWhisperOnlyState] = useState(false);
  const [liveSoap, setLiveSoapState] = useState(false);
  const [ragflowAgents, setRagflowAgents] = useState<RagflowAgent[]>([]);
  const [ragflowConnected, setRagflowConnected] = useState<boolean | null>(null); // null = checking
  const [selectedAgent, setSelectedAgentState] = useState<string>("");
  const setSelectedAgent = useCallback((id: string) => {
    setSelectedAgentState(id);
    if (typeof window !== "undefined") localStorage.setItem("selectedAgent", id);
  }, []);
  const [chunks, setChunks]           = useState(0);
  const [whisperCalls, setWhisperCalls] = useState(0);
  const [duration, setDuration]       = useState(0);
  const [level, setLevel]             = useState<number[]>(EMPTY_LEVELS);

  const socketRef    = useRef<Socket | null>(null);
  const streamRef    = useRef<MediaStream | null>(null);
  const ctxRef       = useRef<AudioContext | null>(null);
  const timerRef     = useRef<ReturnType<typeof setInterval> | null>(null);
  const startTimeRef = useRef(0);
  const activeFieldRef = useRef<SoapField>(activeField);
  const lastLevelUpdate = useRef(0);
  const chunksRef = useRef(0);
  const transcriptRef = useRef("");
  const fieldsRef = useRef(fields);
  const compareModeRef = useRef(false);
  const whisperOnlyRef = useRef(false);

  activeFieldRef.current = activeField;
  fieldsRef.current = fields;
  compareModeRef.current = compareMode;
  whisperOnlyRef.current = whisperOnly;

  const ensureSocket = useCallback(() => {
    if (socketRef.current?.connected) return socketRef.current;
    const s = io(getBackendUrl(), { transports: ["websocket", "polling"], reconnection: true });
    s.on("connect",       () => setConnected(true));
    s.on("disconnect",    () => setConnected(false));
    s.on("connect_error", (err) => {
      console.error("Socket.IO Verbindungsfehler:", err.message);
      setConnected(false);
    });
    s.on("transcription", (data: TranscriptionEvent) => {
      setWhisperCalls((n) => n + 1);
      const confirmed = data.confirmed;
      transcriptRef.current = data.full_text || confirmed;
      setTranscript({
        confirmed,
        provisional: data.is_final ? "" : data.provisional,
      });
      if (data.full_text) setFullText(data.full_text);
      if (data.is_final && data.confirmed) setFullText(data.confirmed);
    });
    // Live SOAP generation started
    s.on("soap_generating", () => {
      setSoapGenerating(true);
      soapStartRef.current = performance.now();
      setSoapDuration(0);
      if (soapTimerRef.current) clearInterval(soapTimerRef.current);
      soapTimerRef.current = setInterval(() => {
        setSoapDuration(Math.round(performance.now() - soapStartRef.current));
      }, 100);
    });
    // Live SOAP updates during streaming
    s.on("soap_update", (data: { S?: string; O?: string; A?: string; P?: string; duration_ms?: number }) => {
      if (soapTimerRef.current) { clearInterval(soapTimerRef.current); soapTimerRef.current = null; }
      setSoapGenerating(false);
      setFields({
        subjective: data.S ?? "",
        objective:  data.O ?? "",
        assessment: data.A ?? "",
        plan:       data.P ?? "",
      });
      if (data.duration_ms != null) setSoapDuration(data.duration_ms);
    });
    // Comparison events (Model A vs B)
    s.on("transcription_compare", (data: CompareTranscript & { is_final: boolean }) => {
      setComparison({
        confirmed_a: data.confirmed_a,
        confirmed_b: data.confirmed_b,
        provisional_a: data.is_final ? "" : data.provisional_a,
        provisional_b: data.is_final ? "" : data.provisional_b,
        model_a: data.model_a,
        model_b: data.model_b,
      });
    });
    // File-stream lifecycle events
    s.on("file_stream_started", (data: FileStreamStartedEvent) => {
      setStreamDuration(data.duration ?? 0);
      setStreamElapsed(0);
      setStreamStatus("");
      setStreaming(true);
      setWhisperCalls(0);
    });
    s.on("file_stream_progress", (data: { elapsed: number; duration: number }) => {
      setStreamElapsed(data.elapsed ?? 0);
    });
    s.on("file_stream_status", (data: { step: string; message: string }) => {
      setStreamStatus(data.message ?? "");
    });
    s.on("file_stream_done", () => {
      setStreaming(false);
      setStreamDuration(0);
      setStreamElapsed(0);
      setStreamStatus("");
    });
    s.on("transcription_error", (data: TranscriptionErrorEvent) => {
      console.error("Transkriptions-Fehler:", data.error);
    });
    socketRef.current = s;
    return s;
  }, []);

  useEffect(() => {
    ensureSocket();

    // Fetch available RAGFlow agents and compare models
    const url = getBackendUrl();
    fetch(`${url}/v1/ragflow-agents`).then((r) => r.json()).then((data) => {
      if (data.agents?.length) {
        setRagflowAgents(data.agents);
        // Restore last selected agent from localStorage, or use default
        const saved = typeof window !== "undefined" ? localStorage.getItem("selectedAgent") : null;
        const DEFAULT_AGENT = "edecf68221ec11f194964348756e437e";
        const ids = data.agents.map((a: { id: string }) => a.id);
        if (saved && ids.includes(saved)) {
          setSelectedAgent(saved);
        } else if (ids.includes(DEFAULT_AGENT)) {
          setSelectedAgent(DEFAULT_AGENT);
        } else {
          setSelectedAgent(data.agents[0].id);
        }
      }
    }).catch(() => {});
    fetch(`${url}/v1/compare-models`).then((r) => r.json()).then((data) => {
      setCompareEnabled(data.compare_enabled ?? false);
    }).catch(() => {});
    fetch(`${url}/v1/ragflow-health`).then((r) => r.json()).then((data) => {
      setRagflowConnected(data.ok === true);
    }).catch(() => setRagflowConnected(false));

    return () => {
      socketRef.current?.disconnect();
      socketRef.current = null;
      ctxRef.current?.close();
      streamRef.current?.getTracks().forEach((t) => t.stop());
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [ensureSocket]);

  // ─── Mic recording ────────────────────────────────────────────────────────

  const startRecording = useCallback(async () => {
    if (streaming) return;
    const socket = ensureSocket();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
      streamRef.current = stream;
      const ctx = new AudioContext({ sampleRate: 16000 });
      ctxRef.current = ctx;
      const source    = ctx.createMediaStreamSource(stream);
      const processor = ctx.createScriptProcessor(4096, 1, 1);
      let sending = false;

      processor.onaudioprocess = (e) => {
        const float32 = e.inputBuffer.getChannelData(0);
        const bandSize = Math.floor(float32.length / 12);

        // Combined RMS + level meter (single pass per band)
        let totalSum = 0;
        const now = performance.now();
        const shouldUpdateLevel = now - lastLevelUpdate.current >= LEVEL_THROTTLE_MS;
        let bands: number[] | null = shouldUpdateLevel ? [] : null;

        for (let i = 0; i < 12; i++) {
          let sum = 0;
          for (let j = i * bandSize; j < (i + 1) * bandSize && j < float32.length; j++) {
            sum += float32[j] * float32[j];
          }
          totalSum += sum;
          if (bands) bands.push(Math.min(1, Math.sqrt(sum / bandSize) * 20));
        }

        if (bands) {
          setLevel(bands);
          lastLevelUpdate.current = now;
        }

        // Gate using already-computed total RMS
        const rms = Math.sqrt(totalSum / float32.length);
        if (!sending && rms > 0.01) sending = true;
        if (!sending) return;

        // PCM int16
        const int16 = new Int16Array(float32.length);
        for (let i = 0; i < float32.length; i++) {
          const s = Math.max(-1, Math.min(1, float32[i]));
          int16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
        socket.emit("audio_chunk", int16.buffer);
        chunksRef.current += 1;
      };

      source.connect(processor);
      processor.connect(ctx.destination);
      socket.emit("start_dictation", {
        field: activeFieldRef.current,
        compare: compareModeRef.current,
        whisper_only: whisperOnlyRef.current,
      });

      setRecording(true);
      chunksRef.current = 0;
      setChunks(0);
      setWhisperCalls(0);
      startTimeRef.current = Date.now();
      timerRef.current = setInterval(() => {
        setDuration((Date.now() - startTimeRef.current) / 1000);
        setChunks(chunksRef.current);
      }, 500);
    } catch (err) {
      console.error("Mikrofon-Zugriff fehlgeschlagen:", err);
    }
  }, [ensureSocket, streaming]);

  const stopRecording = useCallback(() => {
    setRecording(false);
    setLevel(EMPTY_LEVELS);
    if (timerRef.current) clearInterval(timerRef.current);
    socketRef.current?.emit("stop_dictation");
    streamRef.current?.getTracks().forEach((t) => t.stop());
    ctxRef.current?.close();
    streamRef.current = null;
    ctxRef.current    = null;
  }, []);

  const toggleRecording = useCallback(() => {
    if (streaming) return;
    if (recording) stopRecording();
    else startRecording();
  }, [recording, streaming, startRecording, stopRecording]);

  // ─── File streaming ───────────────────────────────────────────────────────

  /**
   * Send a file through the same sliding-window Whisper + Mistral pipeline
   * as the mic.  Progressive transcription events arrive on the normal
   * `transcription` socket event and land in the active SOAP field.
   */
  const streamFile = useCallback(
    async (file: File): Promise<{ ok: boolean; error?: string }> => {
      if (recording || streaming) return { ok: false, error: "busy" };
      if (file.size > MAX_FILE_SIZE) {
        return { ok: false, error: "Datei zu gross (max. 100 MB)" };
      }
      const socket = ensureSocket();
      const buffer = await file.arrayBuffer();

      return new Promise((resolve) => {
        const onDone = () => {
          socket.off("file_stream_done",    onDone);
          socket.off("transcription_error", onError);
          resolve({ ok: true });
        };
        const onError = (data: TranscriptionErrorEvent) => {
          socket.off("file_stream_done",    onDone);
          socket.off("transcription_error", onError);
          setStreaming(false);
          resolve({ ok: false, error: data.error });
        };

        socket.on("file_stream_done",    onDone);
        socket.on("transcription_error", onError);
        socket.emit("transcribe_file_stream", buffer);
      });
    },
    [ensureSocket, recording, streaming],
  );

  const stopFileStream = useCallback(() => {
    socketRef.current?.emit("stop_file_stream");
    setStreaming(false);
    setStreamDuration(0);
    setStreamElapsed(0);
  }, []);

  const toggleCompareMode = useCallback(() => {
    setCompareMode((prev) => {
      const next = !prev;
      socketRef.current?.emit("set_compare_mode", { enabled: next });
      if (!next) setComparison(null);
      return next;
    });
  }, []);

  const toggleWhisperOnly = useCallback(() => {
    setWhisperOnlyState((prev) => {
      const next = !prev;
      socketRef.current?.emit("set_whisper_only", { enabled: next });
      return next;
    });
  }, []);

  const toggleLiveSoap = useCallback(() => {
    setLiveSoapState((prev) => {
      const next = !prev;
      socketRef.current?.emit("set_live_soap", { enabled: next });
      return next;
    });
  }, []);

  const clearTranscript = useCallback(() => {
    setTranscript({ confirmed: "", provisional: "" });
    setFullText("");
  }, []);

  const setFieldText = useCallback((field: SoapField, text: string) => {
    setFields((prev) => ({ ...prev, [field]: text }));
  }, []);

  const clearField = useCallback(() => {
    setFields((prev) => ({ ...prev, [activeField]: "" }));
  }, [activeField]);

  // ─── SOAP generation via backend ──────────────────────────────────────

  const generateSoap = useCallback(
    async (text: string, agentId?: string): Promise<SoapResult> => {
      if (!text.trim()) return { ok: false, error: "Kein Text vorhanden" };
      // Start live timer
      setSoapGenerating(true);
      soapStartRef.current = performance.now();
      setSoapDuration(0);
      if (soapTimerRef.current) clearInterval(soapTimerRef.current);
      soapTimerRef.current = setInterval(() => {
        setSoapDuration(Math.round(performance.now() - soapStartRef.current));
      }, 100);
      try {
        const resp = await fetch(`${getBackendUrl()}/v1/generate-soap`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text, agent_id: agentId ?? "" }),
        });

        if (soapTimerRef.current) { clearInterval(soapTimerRef.current); soapTimerRef.current = null; }
        setSoapGenerating(false);
        if (!resp.ok) return { ok: false, error: `Server-Fehler: ${resp.status}` };
        const data = await resp.json();
        if (data.error) return { ok: false, error: data.error };
        setFields({
          subjective: data.S ?? "",
          objective:  data.O ?? "",
          assessment: data.A ?? "",
          plan:       data.P ?? "",
        });
        if (data.scores) setSoapScores(data.scores);
        if (data.duration_ms != null) setSoapDuration(data.duration_ms);
        return { ok: true, ...data };
      } catch (err) {
        if (soapTimerRef.current) { clearInterval(soapTimerRef.current); soapTimerRef.current = null; }
        setSoapGenerating(false);
        return { ok: false, error: err instanceof Error ? err.message : "Unbekannter Fehler" };
      }
    },
    [],
  );

  // ─── Gold standard save ────────────────────────────────────────────

  const saveGoldStandard = useCallback(
    async (name: string): Promise<GoldStandardResult> => {
      const transcript = transcriptRef.current;
      if (!transcript.trim()) return { ok: false, error: "Kein Transkript vorhanden" };
      if (!name.trim()) return { ok: false, error: "Name darf nicht leer sein" };

      try {
        const resp = await fetch(`${getBackendUrl()}/v1/save-gold-standard`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name,
            transcript,
            S: fieldsRef.current.subjective,
            O: fieldsRef.current.objective,
            A: fieldsRef.current.assessment,
            P: fieldsRef.current.plan,
          }),
        });
        const data = await resp.json();
        if (data.error) return { ok: false, error: data.error };
        return { ok: true, slug: data.slug };
      } catch (err) {
        return { ok: false, error: err instanceof Error ? err.message : "Unbekannter Fehler" };
      }
    },
    [],
  );

  return {
    connected,
    recording,
    streaming,
    streamDuration,
    streamElapsed,
    streamStatus,
    transcript,
    fullText,
    fields,
    activeField,
    setActiveField,
    chunks,
    whisperCalls,
    duration,
    level,
    toggleRecording,
    clearTranscript,
    clearField,
    setFieldText,
    streamFile,
    stopFileStream,
    generateSoap,
    saveGoldStandard,
    soapScores,
    soapDuration,
    soapGenerating,
    compareMode,
    compareEnabled,
    comparison,
    toggleCompareMode,
    whisperOnly,
    toggleWhisperOnly,
    liveSoap,
    toggleLiveSoap,
    ragflowAgents,
    ragflowConnected,
    selectedAgent,
    setSelectedAgent,
  };
}
