"use client";

import { useTheme } from "@/components/theme-provider";
import { THEMES } from "@/lib/themes";

export function ThemeSwitcher() {
  const { theme, setTheme } = useTheme();

  return (
    <div className="fixed top-4 right-4 z-50 flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-3 py-2 backdrop-blur-xl shadow-[0_2px_16px_rgba(0,0,0,0.3),inset_0_1px_0_rgba(255,255,255,0.08)]" role="radiogroup" aria-label="Farbschema wählen">
      {THEMES.map((t) => (
        <button
          key={t.id}
          title={t.label}
          onClick={() => setTheme(t.id)}
          role="radio"
          aria-checked={theme === t.id}
          aria-label={`Farbschema: ${t.label}`}
          className="relative flex h-4 w-4 items-center justify-center rounded-full transition-all duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40"
          style={{
            backgroundColor: t.dot,
            transform: theme === t.id ? "scale(1.35)" : "scale(1)",
            opacity: theme === t.id ? 1 : 0.45,
            boxShadow: theme === t.id ? `0 0 8px ${t.dot}80` : "none",
          }}
        />
      ))}
    </div>
  );
}
