export type Theme = "obsidian" | "sardinia" | "forest";

export const THEMES: { id: Theme; label: string; dot: string }[] = [
  { id: "obsidian", label: "Obsidian", dot: "#7dd3fc" },
  { id: "sardinia", label: "Sardinia", dot: "#22d3ee" },
  { id: "forest",   label: "Forest",   dot: "#4ade80" },
];

export const DEFAULT_THEME: Theme = "obsidian";
export const STORAGE_KEY = "strmcp-theme";
