export type Lang = "tr" | "en";

function load(): Lang {
  try {
    const v = localStorage.getItem("lang");
    if (v === "tr" || v === "en") return v;
  } catch {
  }
  return typeof navigator !== "undefined" && navigator.language?.toLowerCase().startsWith("tr") ? "tr" : "en";
}

let current: Lang = load();
if (typeof document !== "undefined") document.documentElement.lang = current;

export function getLang(): Lang {
  return current;
}

export function setLang(l: Lang) {
  current = l;
  try {
    localStorage.setItem("lang", l);
  } catch {
  }
  document.documentElement.lang = l;
}

export function t(tr: string, en: string): string {
  return current === "en" ? en : tr;
}

export function locale(): string {
  return current === "en" ? "en-GB" : "tr-TR";
}
