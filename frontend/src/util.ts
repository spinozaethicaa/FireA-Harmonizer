import type { DayClass } from "./types";
import { getLang, locale, t } from "./i18n";

export type PaletteName = "standard" | "cvd";
const PALETTES = {
  standard: {
    MODIS: "#eb6834", VIIRS: "#2a78d6", both: "#1baf7a", onMODIS: "#ffffff", onVIIRS: "#ffffff", onBoth: "#ffffff",
    mapMODIS: "#d95926", mapVIIRS: "#3987e5",
  },
  cvd: {
    MODIS: "#E69F00", VIIRS: "#0072B2", both: "#009E73", onMODIS: "#000000", onVIIRS: "#ffffff", onBoth: "#ffffff",
    mapMODIS: "#E69F00", mapVIIRS: "#56B4E9",
  },
};

export const COLORS = {
  MODIS: "#eb6834",
  VIIRS: "#2a78d6",
  both: "#1baf7a",
  onMODIS: "#ffffff",
  onVIIRS: "#ffffff",
  onBoth: "#ffffff",
  none: "#e9e7e1",
  excluded: "#a3a29d",
};

export const MAP_COLORS = { MODIS: "#d95926", VIIRS: "#3987e5", excluded: "#7d7d78", harmon: "#3987e5" };

export const DISPLAY = { patterns: true };

export function applyPalette(name: PaletteName) {
  const p = PALETTES[name] ?? PALETTES.standard;
  Object.assign(COLORS, { MODIS: p.MODIS, VIIRS: p.VIIRS, both: p.both, onMODIS: p.onMODIS, onVIIRS: p.onVIIRS, onBoth: p.onBoth });
  Object.assign(MAP_COLORS, { MODIS: p.mapMODIS, VIIRS: p.mapVIIRS, harmon: p.mapVIIRS });
}

export function loadPref<T extends string>(key: string, fallback: T): T {
  try {
    return (localStorage.getItem(key) as T) || fallback;
  } catch {
    return fallback;
  }
}

export function savePref(key: string, value: string) {
  try {
    localStorage.setItem(key, value);
  } catch {
  }
}

export const DAY_CLASS_LABEL: Record<DayClass, string> = {
  get both() { return t("İki sensör de tespit etti", "Both sensors detected"); },
  get modis_only() { return t("Yalnızca MODIS", "MODIS only"); },
  get viirs_only() { return t("Yalnızca VIIRS", "VIIRS only"); },
  get none() { return t("Tespit yok (yangın olmadığı doğrulanmaz)", "No detection (fire absence not verified)"); },
  get unavailable() { return t("Veri yok / kapsam dışı", "No data / out of range"); },
};

export const COVERAGE_LABEL: Record<string, string> = {
  get ok() { return t("indirildi", "downloaded"); },
  get empty() { return t("indirildi, sıfır tespit", "downloaded, zero detections"); },
  get error() { return t("veri alınamadı (hata)", "could not be retrieved (error)"); },
  get out_of_range() { return t("ürün kullanılabilirlik aralığı dışında", "outside the product's availability range"); },
  get not_queried() { return t("sorgulanmadı", "not queried"); },
  get uploaded() { return t("yüklenen dosya (kapsam varsayımı)", "uploaded file (coverage assumed)"); },
  get sample() { return t("örnek veri", "sample data"); },
};

const tzFmt = new Intl.DateTimeFormat("tr-TR", { timeZone: "Europe/Istanbul", hour: "2-digit", minute: "2-digit" });
const MONTHS_TR = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"];
const MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTHS_LONG_TR = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"];
const MONTHS_LONG_EN = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

export function monthsShort(): string[] {
  return getLang() === "en" ? MONTHS_EN : MONTHS_TR;
}

export function monthsLong(): string[] {
  return getLang() === "en" ? MONTHS_LONG_EN : MONTHS_LONG_TR;
}

export function fmtUtc(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.slice(0, 16).replace("T", " ") + " UTC";
}

export function fmtLocal(iso: string | null | undefined): string {
  if (!iso) return "";
  return tzFmt.format(new Date(iso)) + t(" TSİ", " Turkey time");
}

export function fmtDate(d: string): string {
  return `${+d.slice(8, 10)} ${monthsShort()[+d.slice(5, 7) - 1]} ${d.slice(0, 4)}`;
}

export function monthLabel(ym: string): string {
  return `${monthsShort()[+ym.slice(5, 7) - 1]} ${ym.slice(0, 4)}`;
}

export function fmtNum(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toLocaleString(locale(), { maximumFractionDigits: digits });
}

export function isoDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

export function addDays(iso: string, n: number): string {
  const d = new Date(iso + "T00:00:00Z");
  d.setUTCDate(d.getUTCDate() + n);
  return isoDay(d);
}

export function eventColor(id: string | null): string {
  if (!id) return COLORS.excluded;
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360}, 60%, 42%)`;
}

export function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

export function fmtInt(v: number): string {
  return v.toLocaleString(locale());
}
