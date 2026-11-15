import type {
  ApiErrorInfo, BBox, CrossLink, EventFull, JobStatus, MatchParams, Mode, Obs, ObsFull, ProductAvailability,
  QualityParams, Summary, VMCluster,
} from "./types";
import { getLang, t } from "./i18n";

export class ApiError extends Error {
  info: ApiErrorInfo;
  constructor(info: ApiErrorInfo) {
    super(info.message);
    this.info = info;
  }
}

async function req<T>(url: string, init?: RequestInit): Promise<T> {
  let resp: Response;
  try {
    const headers = new Headers(init?.headers);
    headers.set("X-Lang", getLang());
    resp = await fetch(url, { ...init, headers });
  } catch {
    throw new ApiError({ kind: "backend_unreachable", message: t("Backend'e ulaşılamadı. Sunucu çalışıyor mu? (port 8000)", "Could not reach the backend. Is the server running? (port 8000)") });
  }
  let body: any = null;
  try {
    body = await resp.json();
  } catch {
  }
  if (!resp.ok) {
    const info = body?.error ?? body?.detail;
    if (info && typeof info === "object" && !Array.isArray(info)) throw new ApiError({ ...info, status: resp.status });
    throw new ApiError({ kind: resp.status === 422 ? "validation" : "http", message: t(`İstek başarısız (HTTP ${resp.status}).`, `Request failed (HTTP ${resp.status}).`), status: resp.status });
  }
  return body as T;
}

const post = (body?: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: body === undefined ? undefined : JSON.stringify(body),
});

const qs = (o: Record<string, string | undefined | null>) =>
  Object.entries(o)
    .filter(([, v]) => v)
    .map(([k, v]) => `${k}=${encodeURIComponent(v!)}`)
    .join("&");

export const api = {
  health: () => req<{ ok: boolean; version: string; map_key_configured: boolean }>("/api/health"),
  availability: (refresh = false) =>
    req<{ products: Record<string, ProductAvailability> }>(`/api/availability${refresh ? "?refresh=true" : ""}`),
  startFetch: (p: {
    mode: Mode; viirs_satellite: string; use_modis: boolean; use_viirs: boolean; bbox: BBox; start: string; end: string;
    quality: QualityParams; match: MatchParams;
  }) => req<{ job_id: string }>("/api/fetch", post(p)),
  job: (id: string) => req<JobStatus>(`/api/jobs/${id}`, { signal: AbortSignal.timeout(15000) }),
  cancelJob: (id: string) => req<{ ok: boolean }>(`/api/jobs/${id}/cancel`, post()),
  upload: (files: File[]) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f));
    return req<{ dataset_id: string }>("/api/upload", { method: "POST", body: fd });
  },
  sample: () => req<{ dataset_id: string }>("/api/sample", post()),
  analyzeJob: (dataset_id: string, quality: QualityParams, match: MatchParams) =>
    req<{ job_id: string }>("/api/analyze_job", post({ dataset_id, quality, match })),
  analyze: (dataset_id: string, quality: QualityParams, match: MatchParams) =>
    req<Summary>("/api/analyze", post({ dataset_id, quality, match })),
  observations: (key: string, day?: string | null, event_id?: string | null) =>
    req<{ total: number; truncated: boolean; observations: Obs[] }>(`/api/observations?${qs({ key, day, event_id })}`),
  event: (key: string, event_id: string) =>
    req<{ event: EventFull; members: Obs[] }>(`/api/event?${qs({ key, event_id })}`),
  observation: (key: string, id: string) =>
    req<{ observation: ObsFull; links: CrossLink[] }>(`/api/observation?${qs({ key, id })}`),
  virtualModis: (key: string, day?: string | null) =>
    req<{ available: boolean; reason?: string; clusters: VMCluster[]; n_total: number }>(`/api/virtual_modis?${qs({ key, day })}`),
  exportUrl: (key: string, kind: string) => `/api/export?${qs({ key, kind, lang: getLang() })}`,
  reportUrl: (key: string) => `/api/report?${qs({ key, lang: getLang() })}`,
  climatologyJob: (bbox: BBox, refresh = false) =>
    req<{ job_id: string }>("/api/climatology_job", post({ bbox, refresh })),
};

export const ERROR_TITLES: Record<string, string> = {
  get no_key() { return t("API anahtarı yapılandırılmamış", "API key not configured"); },
  get invalid_key() { return t("Geçersiz API anahtarı", "Invalid API key"); },
  get rate_limit() { return t("FIRMS istek sınırı aşıldı", "FIRMS rate limit exceeded"); },
  get timeout() { return t("Zaman aşımı", "Timeout"); },
  get network() { return t("Ağ hatası", "Network error"); },
  get http() { return t("Sunucu hatası", "Server error"); },
  get bad_response() { return t("Beklenmeyen FIRMS yanıtı", "Unexpected FIRMS response"); },
  get bad_request() { return t("Geçersiz istek", "Invalid request"); },
  get validation() { return t("Geçersiz parametre", "Invalid parameter"); },
  get backend_unreachable() { return t("Backend'e ulaşılamıyor", "Backend unreachable"); },
  get bad_file() { return t("Dosya okunamadı", "File could not be read"); },
  get empty_file() { return t("Dosyada kayıt yok", "File has no records"); },
  get not_found() { return t("Bulunamadı", "Not found"); },
  get analysis_expired() { return t("Analiz süresi doldu", "Analysis expired"); },
  get cancelled() { return t("İptal edildi", "Cancelled"); },
  get internal() { return t("Sunucu hatası", "Server error"); },
};

export function errInfo(e: unknown): ApiErrorInfo {
  if (e instanceof ApiError) return e.info;
  return { kind: "internal", message: String(e) };
}
