"use client";

import { Moon, Sun } from "lucide-react";
import { createContext, type ReactNode, useContext, useEffect, useState } from "react";
import { resolveTheme, THEME_STORAGE_KEY, type Theme } from "./theme-preference";

const ThemeContext = createContext<{
  theme: Theme;
  toggleTheme: () => void;
  error: string | null;
} | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>("light");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    function syncTheme() {
      let preference: string | null = null;
      try {
        preference = window.localStorage.getItem(THEME_STORAGE_KEY);
        setError(null);
      } catch (cause) {
        console.warn("无法读取主题偏好。", cause);
        setError("无法读取主题偏好，当前跟随系统主题。");
      }
      const next = resolveTheme(preference, media.matches);
      document.documentElement.dataset.theme = next;
      setTheme(next);
    }
    function onStorage(event: StorageEvent) {
      if (event.key === THEME_STORAGE_KEY || event.key === null) syncTheme();
    }
    syncTheme();
    media.addEventListener("change", syncTheme);
    window.addEventListener("storage", onStorage);
    return () => {
      media.removeEventListener("change", syncTheme);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  function toggleTheme() {
    const next = theme === "light" ? "dark" : "light";
    setTheme(next);
    document.documentElement.dataset.theme = next;
    setError(null);
    try {
      window.localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch (cause) {
      console.warn("无法保存主题偏好。", cause);
      setError("主题已切换，但无法保存偏好，刷新后可能恢复之前的主题。");
    }
  }

  return (
    <ThemeContext.Provider value={{ theme, toggleTheme, error }}>
      {children}
    </ThemeContext.Provider>
  );
}

export function ThemeToggle() {
  const context = useContext(ThemeContext);
  if (!context) throw new Error("ThemeToggle must be used within ThemeProvider.");
  const { theme, toggleTheme, error } = context;
  const label = theme === "light" ? "切换到深色主题" : "切换到浅色主题";
  return (
    <div className="theme-control">
      <button aria-label={label} className="theme-toggle" onClick={toggleTheme}
        title={label} type="button">
        {theme === "light" ? <Moon aria-hidden="true" size={18} /> : <Sun aria-hidden="true" size={18} />}
      </button>
      {error ? <span className="theme-error" role="status">{error}</span> : null}
    </div>
  );
}
