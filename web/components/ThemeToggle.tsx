"use client";
// System / Light / Dark. Stored per browser; layout.tsx applies it before paint so there's no flash.
import { useEffect, useState } from "react";

type Theme = "system" | "light" | "dark";
const KEY = "theme";

export const themeScript = `try{var t=localStorage.getItem("${KEY}");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}catch(e){}`;

export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("system");
  useEffect(() => {
    try { const t = localStorage.getItem(KEY); if (t === "light" || t === "dark") setTheme(t); } catch { /* storage blocked */ }
  }, []);
  const pick = (t: Theme) => {
    setTheme(t);
    if (t === "system") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
    try { if (t === "system") localStorage.removeItem(KEY); else localStorage.setItem(KEY, t); } catch { /* storage blocked */ }
  };
  return (
    <div role="radiogroup" aria-label="Theme" className="flex rounded-md border border-line p-0.5 text-xs">
      {(["system", "light", "dark"] as const).map((t) => (
        <button key={t} role="radio" aria-checked={theme === t} onClick={() => pick(t)}
          className={`rounded px-2 py-0.5 capitalize ${theme === t ? "bg-ink text-bg" : "text-ink-2 hover:text-ink"}`}>{t}</button>
      ))}
    </div>
  );
}
