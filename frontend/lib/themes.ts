export type Theme = "obsidian" | "sardinia" | "forest" | "vitodata" | "liquid";

export const THEMES: { id: Theme; label: string; dot: string }[] = [
  { id: "obsidian", label: "Obsidian", dot: "#7dd3fc" },
  { id: "sardinia", label: "Sardinia", dot: "#22d3ee" },
  { id: "forest",   label: "Forest",   dot: "#4ade80" },
  { id: "vitodata", label: "Vitodata", dot: "#F5A623" },
  { id: "liquid",   label: "Liquid",   dot: "#e0c3fc" },
];

export const DEFAULT_THEME: Theme = "obsidian";
export const STORAGE_KEY = "strmcp-theme";
