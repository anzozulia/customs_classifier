/**
 * Light/dark for BOTH halves of the page, kept in one place because they are two different
 * mechanisms that must not drift:
 *
 *   - the shell  -> a `dark` class on <html>, which is what Tailwind's darkMode:"class" reads
 *   - the chat   -> ChatKit's `theme.colorScheme`, passed through useChatKit
 *
 * ChatKit does NOT follow prefers-color-scheme on its own; its default is "light" forever.
 * So the media query is watched here and the result is pushed into both.
 */

import { useCallback, useEffect, useState } from "react";

import type { ColorScheme } from "@openai/chatkit";

export type { ColorScheme };

const STORAGE_KEY = "uktzed:color-scheme";

function readStored(): ColorScheme | null {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    return value === "light" || value === "dark" ? value : null;
  } catch {
    // Private mode / storage disabled. Fall back to the media query.
    return null;
  }
}

function systemScheme(): ColorScheme {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function useColorScheme(): { scheme: ColorScheme; toggle: () => void } {
  const [scheme, setScheme] = useState<ColorScheme>(() => readStored() ?? systemScheme());

  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle("dark", scheme === "dark");
    // Makes native form controls and scrollbars follow too.
    root.style.colorScheme = scheme;
  }, [scheme]);

  useEffect(() => {
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (event: MediaQueryListEvent) => {
      // An explicit choice wins over the OS until the user clears site data.
      if (readStored() === null) setScheme(event.matches ? "dark" : "light");
    };
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);

  const toggle = useCallback(() => {
    setScheme((current) => {
      const next: ColorScheme = current === "dark" ? "light" : "dark";
      try {
        window.localStorage.setItem(STORAGE_KEY, next);
      } catch {
        // Not persisting a theme preference is not worth an error path.
      }
      return next;
    });
  }, []);

  return { scheme, toggle };
}
