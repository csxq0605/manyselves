import { useEffect, useState } from "react";

export type ColorScheme = "light" | "dark";

/** The scheme the page is drawn in: the client theme setting, with "system" following the OS. */
export function currentColorScheme(root: HTMLElement = document.documentElement): ColorScheme {
  const theme = root.dataset.theme;
  if (theme === "dark") return "dark";
  if (theme === "light") return "light";
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

export function useColorScheme(): ColorScheme {
  const [scheme, setScheme] = useState<ColorScheme>(() => currentColorScheme());
  useEffect(() => {
    const update = () => setScheme(currentColorScheme());
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, { attributeFilter: ["data-theme"], attributes: true });
    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    media?.addEventListener?.("change", update);
    return () => {
      observer.disconnect();
      media?.removeEventListener?.("change", update);
    };
  }, []);
  return scheme;
}
