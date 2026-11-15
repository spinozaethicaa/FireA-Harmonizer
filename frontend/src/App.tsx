import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ERROR_TITLES, errInfo } from "./api";
import Calendar from "./Calendar";
import DetailPanel, { ModelCard } from "./DetailPanel";
import HarmonyChart from "./HarmonyChart";
import HistoryPanel from "./HistoryPanel";
import MapView from "./MapView";
import { getLang, setLang as setLangGlobal, t } from "./i18n";
import type { Lang } from "./i18n";
import type {
  ApiErrorInfo, BBox, ClimResult, JobStatus, MatchParams, Mode, Obs, ProductAvailability, QualityParams, Summary, VMCluster,
} from "./types";
import { addDays, applyPalette, DISPLAY, fmtDate, fmtInt, fmtNum, loadPref, MAP_COLORS, savePref } from "./util";
import type { PaletteName } from "./util";

type Tab = "obs" | "events" | "harmonize" | "history";

const DEFAULT_BBOX: BBox = [27, 35.5, 32.5, 38];
const DEFAULT_QUALITY: QualityParams = { modis_min_confidence: 30, viirs_classes: ["n", "h"], daynight: "all" };
const DEFAULT_MATCH: MatchParams = {
  cross_dist_km: 1.5, cross_dt_min: 60, use_pixel_size: true, same_pass_adj_km: 1.0, pass_window_min: 10,
  max_cluster_span_min: 90, max_cluster_extent_km: 10, cont_dist_km: 2.0, cont_gap_h: 48, max_event_extent_km: 30,
};
const modes = (): { id: Mode; label: string; desc: string }[] => [
  { id: "nrt", label: t("Güncel (NRT)", "Near real-time (NRT)"), desc: t("yakın gerçek zamanlı, son birkaç ay", "near real-time, last few months") },
  { id: "sp", label: t("Arşiv (SP)", "Archive (SP)"), desc: t("standart bilimsel ürün, birkaç ay gecikmeli", "standard science product, a few months behind") },
  { id: "combined", label: t("Arşiv + Güncel", "Archive + NRT"), desc: t("SP bittiği günden sonra NRT; ürünler ayrı etiketlenir", "NRT after the last SP day; products are labelled separately") },
];
const tabs = (): { id: Tab; label: string }[] => [
  { id: "obs", label: t("Gözlemler", "Observations") },
  { id: "events", label: t("Olaylar", "Events") },
  { id: "harmonize", label: t("Harmonize", "Harmonize") },
  { id: "history", label: t("Tarihsel", "History") },
];

const places = (): { name: string; lat: number; lon: number; km: number }[] => [
  { name: "Manavgat (Antalya)", lat: 36.79, lon: 31.44, km: 30 },
  { name: "Marmaris (Muğla)", lat: 36.86, lon: 28.27, km: 30 },
  { name: "Bodrum–Milas (Muğla)", lat: 37.15, lon: 27.65, km: 35 },
  { name: "Köyceğiz (Muğla)", lat: 36.97, lon: 28.69, km: 25 },
  { name: "Kemer–Kumluca (Antalya)", lat: 36.55, lon: 30.45, km: 30 },
  { name: t("İzmir çevresi", "İzmir area"), lat: 38.42, lon: 27.14, km: 50 },
  { name: "Çanakkale", lat: 40.15, lon: 26.41, km: 40 },
  { name: "Hatay", lat: 36.40, lon: 36.20, km: 40 },
];

function bboxFromPoint(lat: number, lon: number, km: number): BBox {
  const dlat = km / 111.32;
  const dlon = km / (111.32 * Math.max(0.05, Math.cos((lat * Math.PI) / 180)));
  const r = (v: number) => Math.round(v * 1000) / 1000;
  return [r(Math.max(-180, lon - dlon)), r(Math.max(-90, lat - dlat)), r(Math.min(180, lon + dlon)), r(Math.min(90, lat + dlat))];
}

const LABELS: [string, string][] = [
  ["Standart (SP)", "Standard (SP)"], ["SP + NRT birleşik", "SP + NRT combined"], ["Yüklenen CSV: ", "Uploaded CSV: "],
  ["ÖRNEK VERİ (sentetik, gerçek gözlem değil)", "SAMPLE DATA (synthetic, not real observations)"],
];
const dsLabel = (s: string) => LABELS.reduce((acc, [tr, en]) => (getLang() === "en" ? acc.replace(tr, en) : acc.replace(en, tr)), s);

const sameBox = (a: BBox | null | undefined, b: BBox) => !!a && a.every((v, i) => Math.abs(v - b[i]) < 1e-6);

function productsFor(mode: Mode, sat: string, useM: boolean, useV: boolean) {
  const pre: [string, string][] = [];
  if (useM) pre.push(["MODIS", "MODIS"]);
  if (useV) pre.push(["VIIRS", `VIIRS_${sat}`]);
  return pre.map(([instr, p]) => ({
    instr, products: mode === "nrt" ? [`${p}_NRT`] : mode === "sp" ? [`${p}_SP`] : [`${p}_SP`, `${p}_NRT`],
  }));
}

export default function App() {
  const HASH: Record<Tab, string> = { obs: "gozlemler", events: "olaylar", harmonize: "harmonize", history: "tarihsel" };
  const fromHash = (): Tab => (Object.keys(HASH) as Tab[]).find((k) => "#" + HASH[k] === window.location.hash) ?? "obs";
  const [tab, setTabState] = useState<Tab>(fromHash);
  const setTab = (next: Tab) => {
    setTabState(next);
    if (window.location.hash !== "#" + HASH[next]) window.history.replaceState(null, "", "#" + HASH[next]);
  };
  useEffect(() => {
    const on = () => setTabState(fromHash());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const [lang, setLangState] = useState<Lang>(getLang());
  const setLang = (l: Lang) => {
    if (l === getLang()) return;
    setLangGlobal(l);
    setLangState(l);
    if (summary && !jobIdRef.current) runAnalysis(summary.dataset.dataset_id, applied.q, applied.m);
  };
  const [keyOk, setKeyOk] = useState<boolean | null>(null);
  const [avail, setAvail] = useState<Record<string, ProductAvailability> | null>(null);
  const [mode, setMode] = useState<Mode>("nrt");
  const [viirsSat, setViirsSat] = useState("NOAA20");
  const [useModis, setUseModis] = useState(true);
  const [useViirs, setUseViirs] = useState(true);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [bbox, setBboxState] = useState<BBox>(DEFAULT_BBOX);
  const [fitNonce, setFitNonce] = useState(0);
  const [drawMode, setDrawMode] = useState(false);
  const [quality, setQuality] = useState(DEFAULT_QUALITY);
  const [match, setMatch] = useState(DEFAULT_MATCH);
  const [applied, setApplied] = useState({ q: DEFAULT_QUALITY, m: DEFAULT_MATCH });
  const [job, setJob] = useState<JobStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<ApiErrorInfo | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [dayFilter, setDayFilter] = useState<string | null>(null);
  const [selEvent, setSelEvent] = useState<string | null>(null);
  const [selObs, setSelObs] = useState<string | null>(null);
  const [eventMembers, setEventMembers] = useState<Obs[] | null>(null);
  const [mapObs, setMapObs] = useState<Obs[]>([]);
  const [mapNote, setMapNote] = useState<string | null>(null);
  const [vmClusters, setVmClusters] = useState<VMCluster[]>([]);
  const [showM, setShowM] = useState(true);
  const [showV, setShowV] = useState(true);
  const [showExcl, setShowExcl] = useState(false);
  const jobIdRef = useRef<string | null>(null);
  const runSeq = useRef(0);
  const startingRef = useRef(false);
  const jobShownAt = useRef(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [history, setHistory] = useState<ClimResult | null>(null);
  const [notify, setNotify] = useState(false);
  const [notifyState, setNotifyState] = useState<"on" | "denied" | "unsupported" | null>(null);
  const lastSeenObsRef = useRef<string | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [lastRefresh, setLastRefresh] = useState<string | null>(null);
  const [palette, setPaletteState] = useState<PaletteName>(() => { const p = loadPref<PaletteName>("palette", "standard"); applyPalette(p); return p; });
  const [patterns, setPatternsState] = useState<boolean>(() => { const v = loadPref("patterns", "1") === "1"; DISPLAY.patterns = v; return v; });
  const setPalette = (p: PaletteName) => { applyPalette(p); savePref("palette", p); setPaletteState(p); };
  const setPatterns = (v: boolean) => { DISPLAY.patterns = v; savePref("patterns", v ? "1" : "0"); setPatternsState(v); };

  const applyBbox = useCallback((b: BBox) => {
    setBboxState((prev) => (sameBox(prev, b) ? prev : b));
    setFitNonce((n) => n + 1);
    setDrawMode(false);
  }, []);

  const layer: "raw" | "events" = tab === "events" ? "events" : "raw";
  const view: "real" | "modis" = tab === "harmonize" ? "modis" : "real";

  useEffect(() => {
    api.health().then((h) => {
      setKeyOk(h.map_key_configured);
      if (h.map_key_configured) api.availability().then((a) => setAvail(a.products)).catch((e) => setError(errInfo(e)));
    }).catch((e) => setError(errInfo(e)));
  }, []);

  useEffect(() => {
    if (!drawMode) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setDrawMode(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawMode]);

  const range = useMemo(() => {
    if (!avail) return null;
    const per = productsFor(mode, viirsSat, useModis, useViirs).map(({ instr, products }) => {
      const ps = products.map((p) => avail[p]).filter(Boolean);
      if (ps.length !== products.length) return { instr, min: null as string | null, max: null as string | null };
      return { instr, min: ps.map((p) => p.min_date).sort()[0], max: ps.map((p) => p.max_date).sort().reverse()[0] };
    });
    const valid = per.filter((x) => x.min && x.max);
    if (!valid.length) return { per, commonMax: null as string | null };
    return { per, commonMax: valid.map((x) => x.max!).sort()[0] };
  }, [avail, mode, viirsSat, useModis, useViirs]);

  useEffect(() => {
    if (range?.commonMax && !start) {
      setEnd(range.commonMax);
      setStart(addDays(range.commonMax, -6));
    }
  }, [range, start]);

  const preset = (days: number) => {
    if (!range?.commonMax) return;
    setEnd(range.commonMax);
    setStart(addDays(range.commonMax, -(days - 1)));
  };

  const last48 = () => {
    setMode("nrt");
    const e = (avail && [avail["MODIS_NRT"]?.max_date, avail[`VIIRS_${viirsSat}_NRT`]?.max_date].filter(Boolean).sort()[0]) || end;
    if (e) { setEnd(e); setStart(addDays(e, -1)); }
  };

  const nDays = start && end ? Math.round((Date.parse(end) - Date.parse(start)) / 864e5) + 1 : 0;
  const rangeWarn = useMemo(() => {
    if (!range || !start || !end) return null;
    const outs = range.per.filter((x) => !x.min || start < x.min! || end > x.max!);
    if (!outs.length) return null;
    const txt = outs.map((x) => (x.min ? `${x.instr} ${x.min} – ${x.max}` : `${x.instr}: ${t("bilgi yok", "no information")}`)).join("; ");
    const hint = mode !== "combined" && start < "2026-01-01" ? t(" Uzun geçmiş için “Arşiv + Güncel” seçin.", " For long histories choose “Archive + NRT”.") : "";
    return t(`Ürün kapsamı: ${txt}. Dışarıda kalan günler indirilmez, takvimde “veri yok” görünür.${hint}`,
      `Product coverage: ${txt}. Days outside it are not downloaded and appear as “no data” in the calendar.${hint}`);
  }, [range, start, end, mode, lang]);

  const resetView = () => {
    setSelEvent(null);
    setSelObs(null);
    setDayFilter(null);
    setEventMembers(null);
  };

  const runJob = useCallback(async (startFn: () => Promise<{ job_id: string }>, onDone: (r: any) => void) => {
    const my = ++runSeq.current;
    setError(null);
    setNotice(null);
    if (jobIdRef.current) {
      api.cancelJob(jobIdRef.current).catch(() => undefined);
      jobIdRef.current = null;
      setJob(null);
    }
    setStarting(true);
    startingRef.current = true;
    let id: string;
    try {
      id = (await startFn()).job_id;
    } catch (e) {
      if (runSeq.current === my) { setStarting(false); startingRef.current = false; setError(errInfo(e)); }
      return;
    }
    if (runSeq.current !== my) {
      api.cancelJob(id).catch(() => undefined);
      return;
    }
    setStarting(false);
    startingRef.current = false;
    jobIdRef.current = id;
    jobShownAt.current = Date.now();
    setJob({ job_id: id, status: "running", phase: "plan", done: 0, total: 0, rows: 0, message: t("Başlatılıyor…", "Starting…"), elapsed_s: 0, result: null, error: null });
    let fails = 0;
    while (jobIdRef.current === id) {
      await new Promise((r) => setTimeout(r, 800));
      if (jobIdRef.current !== id) return;
      try {
        const j = await api.job(id);
        fails = 0;
        if (jobIdRef.current !== id) return;
        if (j.status === "running") { setJob(j); continue; }
        jobIdRef.current = null;
        setJob(null);
        if (j.status === "done" && j.result) onDone(j.result);
        else if (j.status === "error") setError(j.error ?? { kind: "internal", message: t("İş başarısız oldu.", "The job failed.") });
        else if (j.status === "cancelled") setNotice(t("İşlem iptal edildi.", "Cancelled."));
        return;
      } catch (e) {
        if (++fails >= 5) {
          jobIdRef.current = null;
          setJob(null);
          setError({ kind: "backend_unreachable", message: t(`Sunucudan durum alınamadı (${errInfo(e).message}). İş durduruldu; tekrar deneyin.`, `Could not get job status from the server (${errInfo(e).message}). The job was stopped; please try again.`) });
          return;
        }
      }
    }
  }, []);

  const cancelJob = () => {
    if (Date.now() - jobShownAt.current < 800) return;
    const id = jobIdRef.current;
    runSeq.current++;
    jobIdRef.current = null;
    setJob(null);
    setStarting(false);
    startingRef.current = false;
    setNotice(t("İşlem iptal edildi.", "Cancelled."));
    if (id) api.cancelJob(id).catch(() => undefined);
  };

  const runAnalysis = useCallback(
    (datasetId: string, q: QualityParams, m: MatchParams) =>
      runJob(() => api.analyzeJob(datasetId, q, m), (s: Summary) => { setSummary(s); setApplied({ q, m }); }),
    [runJob],
  );

  const onFetch = () => {
    if (jobIdRef.current || startingRef.current) return;
    if (!start || !end) return setError({ kind: "bad_request", message: t("Tarih aralığı seçin.", "Choose a date range.") });
    if (start > end) return setError({ kind: "bad_request", message: t("Başlangıç tarihi bitişten sonra olamaz.", "The start date cannot be after the end date.") });
    const q = quality, m = match;
    runJob(
      () => api.startFetch({ mode, viirs_satellite: viirsSat, use_modis: useModis, use_viirs: useViirs, bbox, start, end, quality: q, match: m }),
      (s: Summary) => { resetView(); setSummary(s); setApplied({ q, m }); setLastRefresh(new Date().toISOString().slice(11, 16)); },
    );
  };

  const onUpload = async (files: FileList | null) => {
    if (!files?.length) return;
    setError(null);
    setBusy(t("Dosyalar okunuyor…", "Reading files…"));
    try {
      const { dataset_id } = await api.upload(Array.from(files));
      setBusy(null);
      resetView();
      await runAnalysis(dataset_id, quality, match);
    } catch (e) {
      setError(errInfo(e));
      setBusy(null);
    }
  };

  const onSample = async () => {
    setError(null);
    try {
      const { dataset_id } = await api.sample();
      resetView();
      await runAnalysis(dataset_id, quality, match);
    } catch (e) {
      setError(errInfo(e));
    }
  };

  const [refreshTick, setRefreshTick] = useState(0);
  const datesRef = useRef({ start, end });
  datesRef.current = { start, end };
  useEffect(() => {
    if (!autoRefresh || mode !== "nrt") return;
    const timer = window.setInterval(async () => {
      if (jobIdRef.current) return;
      try {
        const a = (await api.availability(true)).products;
        setAvail(a);
        const newMax = [a["MODIS_NRT"]?.max_date, a[`VIIRS_${viirsSat}_NRT`]?.max_date].filter(Boolean).sort()[0];
        const { start: s0, end: e0 } = datesRef.current;
        if (newMax && e0 && newMax > e0) {
          const span = Math.round((Date.parse(e0) - Date.parse(s0)) / 864e5);
          setEnd(newMax);
          setStart(addDays(newMax, -span));
        }
      } catch { }
      setRefreshTick((k) => k + 1);
    }, 30 * 60 * 1000);
    return () => window.clearInterval(timer);
  }, [autoRefresh, mode, viirsSat]);

  const enableNotify = async (on: boolean) => {
    if (!on) { setNotify(false); setNotifyState(null); return; }
    if (typeof Notification === "undefined") { setNotifyState("unsupported"); return; }
    const perm = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
    if (perm !== "granted") { setNotifyState("denied"); setNotify(false); return; }
    lastSeenObsRef.current = null;
    setNotify(true);
    setNotifyState("on");
  };
  useEffect(() => {
    if (!summary || !notify || summary.dataset.is_sample) return;
    const newest = summary.events.reduce<string | null>((m, e) => (m === null || e.last_obs_utc > m ? e.last_obs_utc : m), null);
    const seen = lastSeenObsRef.current;
    if (newest && (!seen || newest > seen)) lastSeenObsRef.current = newest;
    if (!seen) return;
    const fresh = summary.events.filter((e) => e.last_obs_utc > seen && Date.now() - Date.parse(e.last_obs_utc) <= 12 * 3.6e6);
    if (fresh.length && typeof Notification !== "undefined" && Notification.permission === "granted") {
      const top = [...fresh].sort((a, b) => (b.max_single_frp_mw ?? 0) - (a.max_single_frp_mw ?? 0))[0];
      new Notification(t(`${fresh.length} yeni yangın tespiti`, `${fresh.length} new fire detection(s)`), {
        body: t(`En güçlüsü ${top.centroid_lat.toFixed(3)}, ${top.centroid_lon.toFixed(3)} · ${top.n_modis + top.n_viirs} tespit · ${fmtNum(top.max_single_frp_mw, 0)} MW`,
          `Strongest at ${top.centroid_lat.toFixed(3)}, ${top.centroid_lon.toFixed(3)} · ${top.n_modis + top.n_viirs} detections · ${fmtNum(top.max_single_frp_mw, 0)} MW`),
        tag: "firms-new-fire",
      });
    }
  }, [summary, notify]);

  useEffect(() => {
    if (refreshTick > 0) onFetch();
  }, [refreshTick]);

  useEffect(() => {
    if (!notice) return;
    const tm = setTimeout(() => setNotice(null), 4000);
    return () => clearTimeout(tm);
  }, [notice]);

  const [mapTotal, setMapTotal] = useState<number | null>(null);
  useEffect(() => {
    if (!summary) { setMapObs([]); setMapNote(null); setMapTotal(null); return; }
    if (selEvent && !dayFilter) {
      if (eventMembers) { setMapObs(eventMembers); setMapNote(null); setMapTotal(null); }
      return;
    }
    let alive = true;
    api.observations(summary.analysis_key, dayFilter).then((r) => {
      if (!alive) return;
      if (r.truncated) {
        setMapObs([]);
        setMapTotal(r.total);
        setMapNote("truncated");
      } else {
        setMapObs(r.observations);
        setMapNote(null);
        setMapTotal(null);
      }
    }).catch(async (e) => {
      if (!alive) return;
      const info = errInfo(e);
      if (info.kind === "analysis_expired") await runAnalysis(summary.dataset.dataset_id, applied.q, applied.m);
      else setError(info);
    });
    return () => { alive = false; };
  }, [summary, dayFilter, selEvent, eventMembers, runAnalysis, applied]);

  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("ornek") === "1") onSample();
  }, []);

  const vmAvailable = !!summary?.virtual_modis?.available;
  useEffect(() => {
    if (!summary || view !== "modis" || !vmAvailable) { setVmClusters([]); return; }
    let alive = true;
    api.virtualModis(summary.analysis_key, dayFilter).then((r) => alive && setVmClusters(r.clusters)).catch((e) => alive && setError(errInfo(e)));
    return () => { alive = false; };
  }, [summary, view, dayFilter, vmAvailable]);

  const visibleEvents = useMemo(
    () => (summary ? (dayFilter ? summary.events.filter((e) => e.days.includes(dayFilter)) : summary.events) : []),
    [summary, dayFilter],
  );
  const highlightDays = useMemo(() => new Set(summary?.events.find((e) => e.event_id === selEvent)?.days ?? []), [summary, selEvent]);
  const dirty = JSON.stringify({ q: quality, m: match }) !== JSON.stringify(applied);
  const ds = summary?.dataset;
  const running = job?.status === "running" || starting;
  const bboxStale = !!ds && !!ds.bbox && !ds.is_sample && ds.kind === "api" && !sameBox(ds.bbox, bbox);
  const selectDay = (d: string | null) => { setDayFilter(d); setSelObs(null); };
  const link = (fn: () => void) => (e: React.MouseEvent) => { e.preventDefault(); fn(); };
  const notifyLabel = notifyState === "on" ? t("açık", "on") : notifyState === "denied" ? t("izin verilmedi", "permission denied") :
    notifyState === "unsupported" ? t("tarayıcı desteklemiyor", "not supported by this browser") : null;
  const errorBox = error && (
    <p className="msg err">
      <b>{ERROR_TITLES[error.kind] ?? t("Hata", "Error")}:</b> {error.message}{" "}
      <a href="#" onClick={link(() => setError(null))}>[{t("kapat", "close")}]</a>
    </p>
  );

  return (
    <div className="page">
      <header className="masthead">
        <div>
          <h1>{t("FIRMS Sıcak Nokta Uyumlaştırma", "FIRMS Hot Spot Harmonization")}</h1>
          <div className="tagline">{t("MODIS ve VIIRS aktif yangın tespitleri · aday yangın olayları · harmonize etkinlik takvimi",
            "MODIS and VIIRS active fire detections · candidate fire events · harmonized activity calendar")}</div>
        </div>
        <div className="mast-right">
          {ds ? (ds.is_sample ? <span className="sample-flag">{t("ÖRNEK VERİ — sentetik", "SAMPLE DATA — synthetic")}</span> : <span>{dsLabel(ds.label)}</span>) : <span>{t("Araştırma prototipi", "Research prototype")}</span>}
          <span className="lang-switch" role="group" aria-label={t("Dil", "Language")}>
            <button className={lang === "tr" ? "on" : ""} aria-pressed={lang === "tr"} onClick={() => setLang("tr")}>TR</button>
            <button className={lang === "en" ? "on" : ""} aria-pressed={lang === "en"} onClick={() => setLang("en")}>EN</button>
          </span>
        </div>
      </header>

      <nav className="menubar">
        {tabs().map((tb) => (
          <a key={tb.id} href={"#" + HASH[tb.id]} className={tab === tb.id ? "on" : ""} onClick={link(() => setTab(tb.id))}>{tb.label}</a>
        ))}
        {summary && (
          <span className="exports">
            {t("Dışa aktar:", "Export:")}{" "}
            <a href={api.exportUrl(summary.analysis_key, "observations")} download>{t("gözlemler", "observations")}</a> ·{" "}
            <a href={api.exportUrl(summary.analysis_key, "events")} download>{t("olaylar", "events")}</a> ·{" "}
            <a href={api.exportUrl(summary.analysis_key, "links")} download>{t("eşleştirmeler", "links")}</a> ·{" "}
            {vmAvailable && <><a href={api.exportUrl(summary.analysis_key, "virtual_modis")} download>{t("harmonize", "harmonized")}</a> ·{" "}</>}
            <a href={api.exportUrl(summary.analysis_key, "metadata")} download>{t("metaveri", "metadata")}</a> ·{" "}
            <a href={api.reportUrl(summary.analysis_key)} download><b>{t("özet rapor (PDF)", "summary report (PDF)")}</b></a>
          </span>
        )}
      </nav>

      <div className="body">
        <aside className="left">
          <div className="box">
            <div className="box-h">{t("Sorgu", "Query")}</div>
            <div className="box-b">
              {keyOk === false && <p className="msg warn">{t("FIRMS anahtarı yapılandırılmamış. CSV yükleyin veya örnek veriyi deneyin.", "No FIRMS key configured. Upload a CSV or try the sample data.")}</p>}
              <div className="field-l">{t("Ürün", "Product")}</div>
              {modes().map((m) => (
                <label key={m.id} className="radio" title={m.desc}>
                  <input type="radio" name="mode" checked={mode === m.id} onChange={() => setMode(m.id)} /> {m.label}
                </label>
              ))}
              {range && (
                <div className="small muted coverage">
                  {range.per.map((x) => <div key={x.instr}>{x.instr}: {x.min ? `${x.min} – ${x.max}` : t("bilgi yok", "no information")}</div>)}
                </div>
              )}
              <div className="field-l">{t("Tarih (UTC)", "Date (UTC)")}</div>
              <div className="two">
                <input type="date" value={start} onChange={(e) => setStart(e.target.value)} aria-label={t("Başlangıç", "Start")} />
                <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} aria-label={t("Bitiş", "End")} />
              </div>
              <div className="small quick">
                <a href="#" onClick={link(() => preset(7))}>{t("son 7 gün", "last 7 days")}</a> ·{" "}
                <a href="#" onClick={link(() => preset(30))}>{t("son 30 gün", "last 30 days")}</a> ·{" "}
                <a href="#" onClick={link(() => preset(365))}>{t("son 1 yıl", "last year")}</a> ·{" "}
                <a href="#" onClick={link(() => { setMode("combined"); setEnd(range?.commonMax ?? end); setStart("2020-01-01"); })}>{t("2020 → bugün", "2020 → today")}</a>
              </div>
              <div className="small quick">
                <a href="#" className="warn-link" onClick={link(last48)} title={t("Erken uyarı: en güncel NRT verisi", "Early warning: freshest NRT data")}>⚑ {t("son 48 saat (erken uyarı)", "last 48 hours (early warning)")}</a>
              </div>
              {mode === "nrt" && (
                <label className="check small">
                  <input type="checkbox" checked={autoRefresh} onChange={(e) => setAutoRefresh(e.target.checked)} />
                  {t("30 dk'da bir otomatik yenile", "auto-refresh every 30 min")}{lastRefresh && <span className="muted"> · {t("son", "last")}: {lastRefresh} UTC</span>}
                </label>
              )}
              {mode === "nrt" && autoRefresh && (
                <label className="check small">
                  <input type="checkbox" checked={notify} onChange={(e) => enableNotify(e.target.checked)} />
                  {t("yeni yangında masaüstü bildirimi", "desktop notification for new fires")}{notifyLabel && <span className="muted"> · {notifyLabel}</span>}
                </label>
              )}
              {rangeWarn && <p className="msg warn small">{rangeWarn}</p>}
              {nDays > 60 && <p className="small muted">{t(`${nDays} gün; ilk indirme birkaç dakika sürebilir (sonra önbellekten).`, `${nDays} days; the first download may take a few minutes (cached afterwards).`)}</p>}

              <div className="field-l">{t("Bölge (B, G, D, K)", "Area (W, S, E, N)")}</div>
              <BboxEditor bbox={bbox} onApply={applyBbox} />
              <PointRadius onApply={applyBbox} />
              <div className="small quick">
                <a href="#" onClick={link(() => setDrawMode((d) => !d))}>{drawMode ? t("çizimi iptal et", "cancel drawing") : t("haritada çiz", "draw on map")}</a> ·{" "}
                <a href="#" onClick={link(() => applyBbox(DEFAULT_BBOX))}>Antalya–Muğla</a>
                <span className="muted"> {t("(çalışma alanı, il sınırı değil)", "(study box, not a province border)")}</span>
              </div>
              {bboxStale && <p className="msg info small">{t("Bölge değişti; gösterilen veriler önceki bölgeye ait. Yeni bölge için “Veriyi getir”e basın.", "The area changed; the data shown belongs to the previous area. Press “Fetch data” for the new area.")}</p>}

              <details className="sub">
                <summary>{t("Sensörler", "Sensors")}</summary>
                <label className="check"><input type="checkbox" checked={useModis} onChange={(e) => setUseModis(e.target.checked)} /> MODIS (Terra, Aqua)</label>
                <label className="check"><input type="checkbox" checked={useViirs} onChange={(e) => setUseViirs(e.target.checked)} /> VIIRS{" "}
                  <select value={viirsSat} onChange={(e) => setViirsSat(e.target.value)}>
                    <option value="NOAA20">NOAA-20</option><option value="SNPP">Suomi NPP</option><option value="NOAA21">NOAA-21</option>
                  </select>
                </label>
              </details>

              {job?.status === "running" ? (
                <button className="go cancel" onClick={cancelJob}>{t("İptal et", "Cancel")}</button>
              ) : (
                <button className="go" disabled={starting || !keyOk || !!busy || nDays <= 0 || (!useModis && !useViirs)}
                  title={!keyOk ? t("FIRMS anahtarı yok", "No FIRMS key") : nDays <= 0 ? t("Geçerli bir tarih aralığı seçin", "Choose a valid date range") : ""} onClick={onFetch}>
                  {starting ? t("Başlatılıyor…", "Starting…") : t("Veriyi getir", "Fetch data")}</button>
              )}
              {job?.status === "running" && <JobProgress job={job} />}
              {notice && <p className="msg info small">{notice}</p>}
              <div className="small alt">
                <label className="link">{t("CSV yükle", "Upload CSV")}<input type="file" accept=".csv" multiple hidden disabled={running} onChange={(e) => { onUpload(e.target.files); e.target.value = ""; }} /></label>
                {" · "}<a href="#" onClick={link(() => { if (!running) onSample(); })}>{t("örnek veri", "sample data")}</a>
              </div>
            </div>
          </div>

          <details className="box">
            <summary className="box-h">{t("Görünüm (erişilebilirlik)", "Display (accessibility)")}</summary>
            <div className="box-b">
              <div className="field-l">{t("Renk paleti", "Colour palette")}</div>
              <label className="radio"><input type="radio" name="pal" checked={palette === "standard"} onChange={() => setPalette("standard")} /> {t("Standart", "Standard")}</label>
              <label className="radio"><input type="radio" name="pal" checked={palette === "cvd"} onChange={() => setPalette("cvd")} /> {t("Renk körü dostu (Okabe–Ito)", "Colour-blind safe (Okabe–Ito)")}</label>
              <label className="check"><input type="checkbox" checked={patterns} onChange={(e) => setPatterns(e.target.checked)} /> {t("Takvimde desenler (renge ek)", "Calendar patterns (in addition to colour)")}</label>
              <p className="hint">{t("Desenler: MODIS yatay çizgi, VIIRS nokta, ikisi düz renk, veri yok taralı. Renk görmeden de okunur; siyah-beyaz baskıya uygundur.",
                "Patterns: MODIS horizontal lines, VIIRS dots, both solid, no data hatched. Readable without colour and suitable for black-and-white printing.")}</p>
            </div>
          </details>

          <details className="box">
            <summary className="box-h">{t("Ayarlar (kalite ve eşleştirme)", "Settings (quality and matching)")}</summary>
            <div className="box-b">
              <div className="field-l">{t("Kalite (sensöre özgü)", "Quality (sensor specific)")}</div>
              <label className="row-l">MODIS confidence ≥
                <input type="number" min={0} max={100} value={quality.modis_min_confidence}
                  onChange={(e) => { const v = parseFloat(e.target.value); if (Number.isFinite(v)) setQuality({ ...quality, modis_min_confidence: Math.max(0, Math.min(100, v)) }); }} />
              </label>
              <div className="row-l">{t("VIIRS sınıfları", "VIIRS classes")}
                <span>{(["l", "n", "h"] as const).map((c) => (
                  <label key={c} className="inline"><input type="checkbox" checked={quality.viirs_classes.includes(c)}
                    onChange={(e) => setQuality({ ...quality, viirs_classes: e.target.checked ? [...quality.viirs_classes, c] : quality.viirs_classes.filter((x) => x !== c) })} />{c}</label>
                ))}</span>
              </div>
              <label className="row-l">{t("Gündüz/gece", "Day/night")}
                <select value={quality.daynight} onChange={(e) => setQuality({ ...quality, daynight: e.target.value as QualityParams["daynight"] })}>
                  <option value="all">{t("hepsi", "all")}</option><option value="D">{t("gündüz", "day")}</option><option value="N">{t("gece", "night")}</option>
                </select>
              </label>
              <div className="field-l">{t("Eşleştirme (deneysel)", "Matching (experimental)")}</div>
              <Num label={t("MODIS↔VIIRS mesafe, km", "MODIS↔VIIRS distance, km")} v={match.cross_dist_km} step={0.1} on={(v) => setMatch({ ...match, cross_dist_km: v })} />
              <Num label={t("MODIS↔VIIRS zaman, dk", "MODIS↔VIIRS time, min")} v={match.cross_dt_min} step={5} on={(v) => setMatch({ ...match, cross_dt_min: v })} />
              <label className="check small"><input type="checkbox" checked={match.use_pixel_size} onChange={(e) => setMatch({ ...match, use_pixel_size: e.target.checked })} /> {t("piksel boyutunu hesaba kat", "account for pixel size")}</label>
              <Num label={t("Aynı geçiş komşuluğu, km", "Same-pass adjacency, km")} v={match.same_pass_adj_km} step={0.1} on={(v) => setMatch({ ...match, same_pass_adj_km: v })} />
              <Num label={t("Maks. küme yayılımı, km", "Max. cluster extent, km")} v={match.max_cluster_extent_km} step={1} on={(v) => setMatch({ ...match, max_cluster_extent_km: v })} />
              <Num label={t("Günler arası devam, km", "Day-to-day continuity, km")} v={match.cont_dist_km} step={0.1} on={(v) => setMatch({ ...match, cont_dist_km: v })} />
              <Num label={t("Maks. boşluk, saat", "Max. gap, hours")} v={match.cont_gap_h} step={6} on={(v) => setMatch({ ...match, cont_gap_h: v })} />
              <Num label={t("Maks. olay yayılımı, km", "Max. event extent, km")} v={match.max_event_extent_km} step={5} on={(v) => setMatch({ ...match, max_event_extent_km: v })} />
              <div className="btns">
                <button disabled={!ds || !!busy || !dirty || running} onClick={() => ds && runAnalysis(ds.dataset_id, quality, match)}>{t("Yeniden hesapla", "Recompute")}</button>
                <button onClick={() => { setQuality(DEFAULT_QUALITY); setMatch(DEFAULT_MATCH); }}>{t("Varsayılan", "Defaults")}</button>
              </div>
            </div>
          </details>
        </aside>

        {tab === "history" ? (
          <>
            <main className="center">
              {errorBox}
              {notice && <p className="msg info">{notice}</p>}
              <HistoryPanel bbox={bbox} running={running} keyOk={keyOk} runJob={runJob} result={history} setResult={setHistory} />
            </main>
            <aside className="right">
              <div className="box">
                <div className="box-h">{t("Nasıl okunur?", "How to read this")}</div>
                <div className="box-b small">
                  {getLang() === "en" ? (
                    <>
                      <p><b>Burning calendar</b>: each row is a year, each column a month. Colour is that month's burning activity (MODIS units).</p>
                      <p><b>Combined record</b>: real Aqua-MODIS wherever it exists; months without MODIS are filled with VIIRS converted to MODIS units by FireA and calibrated on the overlap years. This gives one comparable series from 2003 to today that continues with VIIRS after MODIS is retired.</p>
                      <p><b>Anomaly</b>: how far a month departs from the typical value (median) of the same month in other years. A month above typical + 2 × normal spread is unusual.</p>
                      <p><b>Critical period</b>: the months that hold at least 70% of yearly activity.</p>
                      <p>Change the area in the <b>Query</b> box on the left (box, drawing, or point + radius).</p>
                      <p className="muted">Detection count is not fire count. Clouds and observation opportunities vary between years.</p>
                    </>
                  ) : (
                    <>
                      <p><b>Yanma takvimi</b>: her satır bir yıl, her sütun bir ay. Renk o ayın yanma etkinliğidir (MODIS birimi).</p>
                      <p><b>Birleşik kayıt</b>: gerçek Aqua-MODIS olan her ayda MODIS'in kendisi; MODIS olmayan aylarda FireA ile MODIS birimine çevrilip örtüşme yıllarına göre kalibre edilmiş VIIRS. Böylece 2003'ten bugüne tek ve karşılaştırılabilir bir seri oluşur; MODIS emekliye ayrıldığında kayıt VIIRS ile sürer.</p>
                      <p><b>Anomali</b>: bir ay, aynı ayın diğer yıllardaki tipik değerinden (medyan) ne kadar sapıyor? Tipik + 2 × normal oynamayı geçen ay olağandışıdır.</p>
                      <p><b>Kritik dönem</b>: yıllık etkinliğin en az %70'inin yoğunlaştığı aylar.</p>
                      <p>Bölgeyi soldaki <b>Sorgu</b> kutusundan (kutu, çizim veya nokta + yarıçap) değiştirebilirsiniz.</p>
                      <p className="muted">Tespit sayısı yangın sayısı değildir. Bulut ve gözlem fırsatı farkları yıllar arasında değişkenlik yaratır.</p>
                    </>
                  )}
                </div>
              </div>
            </aside>
          </>
        ) : (
          <>
            <main className="center">
              {errorBox}
              {busy && <p className="msg info">{busy}</p>}
              {ds && <StatusLine ds={ds} summary={summary!} />}
              {summary && <KpiStrip summary={summary} reportUrl={api.reportUrl(summary.analysis_key)} />}

              <div className="mapbox">
                <div className="map-legend">
                  {view === "real" ? (
                    <>
                      <label><input type="checkbox" checked={showM} onChange={(e) => setShowM(e.target.checked)} /><i style={{ background: MAP_COLORS.MODIS }} />MODIS</label>
                      <label><input type="checkbox" checked={showV} onChange={(e) => setShowV(e.target.checked)} /><i style={{ background: MAP_COLORS.VIIRS }} />VIIRS</label>
                      <label><input type="checkbox" checked={showExcl} onChange={(e) => setShowExcl(e.target.checked)} /><i style={{ background: MAP_COLORS.excluded }} />{t("filtrelenen", "filtered out")}</label>
                      {tab === "events" && <span className="small">{t("renk: aday olay · halka: olay merkezi", "colour: candidate event · ring: event centre")}</span>}
                    </>
                  ) : (
                    <>
                      <label><input type="checkbox" checked={showM} onChange={(e) => setShowM(e.target.checked)} /><i style={{ background: MAP_COLORS.MODIS }} />{t("gerçek Aqua-MODIS", "real Aqua-MODIS")}</label>
                      <span><i className="ring" />{t("VIIRS kümesi → harmonize (boyut: MODIS-eşdeğeri)", "VIIRS cluster → harmonized (size: MODIS equivalent)")}</span>
                    </>
                  )}
                </div>
                <MapView
                  observations={mapObs}
                  events={layer === "events" ? visibleEvents : []}
                  mode={layer}
                  view={view}
                  paletteKey={palette + lang}
                  vmClusters={vmClusters}
                  showModis={showM}
                  showViirs={showV}
                  showExcluded={showExcl}
                  selectedEventId={selEvent}
                  selectedObsId={selObs}
                  bbox={bbox}
                  dataBbox={bboxStale ? ds!.bbox : null}
                  drawMode={drawMode}
                  fitKey={`${summary?.analysis_key ?? "none"}|${fitNonce}`}
                  onSelectObs={setSelObs}
                  onSelectEvent={(id) => { setSelObs(null); setSelEvent(id); }}
                  onBboxDrawn={applyBbox}
                />
                {drawMode && <div className="map-msg">{t("Haritada sürükleyerek yeni bölge çizin (Esc: iptal).", "Drag on the map to draw a new area (Esc: cancel).")}</div>}
                {!summary && !drawMode && <div className="map-msg">{getLang() === "en" ? <>Choose dates on the left and press <b>Fetch data</b>.</> : <>Soldan tarih seçip <b>Veriyi getir</b>'e basın.</>}</div>}
                {summary && mapNote && mapTotal !== null && <div className="map-msg bottom">{t(`Bu kapsamda ${fmtInt(mapTotal)} gözlem var; noktaları görmek için takvimden bir gün seçin.`, `There are ${fmtInt(mapTotal)} observations in this selection; pick a day in the calendar to see the points.`)}</div>}
                {summary && summary.stats.n_observations_total === 0 && <div className="map-msg">{t("Bu bölge ve tarihlerde tespit kaydı yok (yangın yokluğunu doğrulamaz).", "No detections for this area and dates (this does not verify the absence of fire).")}</div>}
                {tab === "harmonize" && summary && !vmAvailable && <div className="map-msg">{t("Harmonize kullanılamıyor", "Harmonization unavailable")}: {summary.virtual_modis?.reason}</div>}
              </div>

              {summary && tab === "harmonize" && summary.virtual_modis?.daily && (
                <HarmonyChart daily={summary.virtual_modis.daily} selectedDay={dayFilter} onSelectDay={selectDay} />
              )}
              {summary && (
                <Calendar view={view} vmDaily={summary.virtual_modis?.daily} days={summary.calendar.days}
                  selectedDay={dayFilter} highlightDays={highlightDays} onSelectDay={selectDay} />
              )}
              {summary && <p className="foot">{summary.time_note}</p>}
            </main>

            <aside className="right">
              {!summary ? (
                <div className="box">
                  <div className="box-h">{t("Hakkında", "About")}</div>
                  <div className="box-b small">
                    {getLang() === "en" ? (
                      <>
                        <p>This tool converts MODIS and VIIRS hot spots to a common format, links nearby observations into <b>candidate fire events</b>, and turns VIIRS observations into a <b>harmonized</b> activity indicator comparable with MODIS.</p>
                        <p><b>Observations</b> — raw detections and the daily sensor calendar.<br /><b>Events</b> — linked detection clusters.<br /><b>Harmonize</b> — daily MODIS-equivalent activity from FireA.<br /><b>History</b> — burning calendar 2003 → today, critical periods and anomalies.</p>
                      </>
                    ) : (
                      <>
                        <p>Bu araç MODIS ve VIIRS sıcak noktalarını ortak biçime çevirir, yakın gözlemleri ilişkilendirerek <b>aday yangın olayları</b> oluşturur ve VIIRS gözlemlerini MODIS ile karşılaştırılabilir <b>harmonize</b> bir etkinlik göstergesine dönüştürür.</p>
                        <p><b>Gözlemler</b> — ham tespitler ve günlük sensör takvimi.<br /><b>Olaylar</b> — ilişkilendirilmiş tespit kümeleri.<br /><b>Harmonize</b> — FireA ile MODIS-eşdeğeri günlük etkinlik.<br /><b>Tarihsel</b> — 2003'ten bugüne yanma takvimi, kritik dönemler ve anomaliler.</p>
                      </>
                    )}
                  </div>
                </div>
              ) : tab === "harmonize" && !selObs ? (
                <ModelCard vm={summary.virtual_modis!} dayFilter={dayFilter} />
              ) : (
                <DetailPanel summary={summary} events={visibleEvents} dayFilter={dayFilter} selectedEventId={selEvent} selectedObsId={selObs}
                  onSelectEvent={(id) => { setSelEvent(id); setSelObs(null); }} onSelectObs={setSelObs} onEventMembers={setEventMembers} />
              )}
            </aside>
          </>
        )}
      </div>
    </div>
  );
}

const phases = (): Record<string, string> => ({
  plan: t("Hazırlanıyor", "Preparing"), download: t("FIRMS’ten indiriliyor", "Downloading from FIRMS"),
  normalize: t("Kayıtlar normalize ediliyor", "Normalizing records"), analyze: t("Eşleştiriliyor", "Matching"),
  firea: t("Harmonize hesaplanıyor", "Computing harmonization"), aggregate: t("Aylık toplamlar hesaplanıyor", "Computing monthly totals"),
});

function JobProgress({ job }: { job: JobStatus }) {
  const det = job.phase === "download" && job.total > 0;
  return (
    <div className="progress">
      <div className={"bar" + (det ? "" : " indet")}><span style={det ? { width: `${(100 * job.done) / job.total}%` } : undefined} /></div>
      <div className="small"><b>{phases()[job.phase] ?? job.phase}</b>{det && ` · ${job.done}/${job.total} ${t("parça", "chunks")}`}
        {job.rows > 0 && ` · ${fmtInt(job.rows)} ${t("kayıt", "records")}`} · {Math.round(job.elapsed_s ?? 0)} {t("sn", "s")}</div>
      <div className="small muted ellipsis" title={job.message}>{job.message}</div>
    </div>
  );
}

function parseNum(x: string) {
  return parseFloat(x.trim().replace(",", "."));
}

function BboxEditor({ bbox, onApply }: { bbox: BBox; onApply: (b: BBox) => void }) {
  const [draft, setDraft] = useState<string[]>(bbox.map(String));
  useEffect(() => { setDraft(bbox.map(String)); }, [bbox]);
  const nums = draft.map(parseNum);
  const err = !nums.every(Number.isFinite) ? t("Dört sayı girin.", "Enter four numbers.") :
    !(nums[0] < nums[2]) ? t("Batı < Doğu olmalı.", "West must be < East.") :
    !(nums[1] < nums[3]) ? t("Güney < Kuzey olmalı.", "South must be < North.") :
    nums[0] < -180 || nums[2] > 180 || nums[1] < -90 || nums[3] > 90 ? t("Koordinatlar sınır dışında.", "Coordinates out of range.") : null;
  const changed = draft.some((x, i) => parseNum(x) !== bbox[i]);
  const commit = () => { if (!err && changed) onApply(nums as BBox); };
  const labels = [t("Batı", "West"), t("Güney", "South"), t("Doğu", "East"), t("Kuzey", "North")];
  return (
    <>
      <div className="four">
        {draft.map((v, i) => (
          <input key={i} type="text" inputMode="decimal" value={v} aria-label={labels[i]} title={labels[i]}
            className={err ? "bad" : changed ? "dirty" : ""}
            onChange={(e) => { const nd = [...draft]; nd[i] = e.target.value; setDraft(nd); }}
            onBlur={commit} onKeyDown={(e) => { if (e.key === "Enter") commit(); if (e.key === "Escape") setDraft(bbox.map(String)); }} />
        ))}
      </div>
      {err ? <div className="small err-t">{err} {t("Uygulanan kutu", "Applied box")}: {bbox.join(", ")}</div>
        : changed && <div className="small muted">{t("Uygulamak için Enter'a basın veya alandan çıkın (Esc: geri al).", "Press Enter or leave the field to apply (Esc: undo).")}</div>}
    </>
  );
}

function PointRadius({ onApply }: { onApply: (b: BBox) => void }) {
  const [lat, setLat] = useState("36.79");
  const [lon, setLon] = useState("31.44");
  const [km, setKm] = useState("30");
  const n = [lat, lon, km].map(parseNum);
  const ok = n.every(Number.isFinite) && Math.abs(n[0]) <= 90 && Math.abs(n[1]) <= 180 && n[2] > 0 && n[2] <= 1000;
  const list = places();
  return (
    <details className="sub">
      <summary>{t("Nokta + yarıçap / hazır konum", "Point + radius / preset place")}</summary>
      <select value="" onChange={(e) => {
        const p = list[+e.target.value];
        if (!p) return;
        setLat(String(p.lat)); setLon(String(p.lon)); setKm(String(p.km));
        onApply(bboxFromPoint(p.lat, p.lon, p.km));
      }} style={{ width: "100%", margin: "4px 0" }}>
        <option value="" disabled>{t("Hazır konum seçin…", "Choose a preset place…")}</option>
        {list.map((p, i) => <option key={p.name} value={i}>{p.name} · {p.km} km</option>)}
      </select>
      <div className="three">
        <label>{t("Enlem", "Lat")}<input type="text" inputMode="decimal" value={lat} onChange={(e) => setLat(e.target.value)} /></label>
        <label>{t("Boylam", "Lon")}<input type="text" inputMode="decimal" value={lon} onChange={(e) => setLon(e.target.value)} /></label>
        <label>{t("Yarıçap km", "Radius km")}<input type="text" inputMode="decimal" value={km} onChange={(e) => setKm(e.target.value)} /></label>
      </div>
      <button disabled={!ok} onClick={() => onApply(bboxFromPoint(n[0], n[1], n[2]))} style={{ marginTop: 4 }}>{t("Bölgeyi uygula", "Apply area")}</button>
      {!ok && <div className="small err-t">{t("Enlem −90…90, boylam −180…180, yarıçap 0–1000 km olmalı.", "Latitude −90…90, longitude −180…180, radius 0–1000 km.")}</div>}
    </details>
  );
}

function KpiStrip({ summary, reportUrl }: { summary: Summary; reportUrl: string }) {
  const st = summary.stats;
  const cal = summary.calendar.days;
  const daysWith = cal.filter((d) => d.class === "both" || d.class === "modis_only" || d.class === "viirs_only").length;
  const maxFrp = Math.max(0, ...summary.events.map((e) => e.max_single_frp_mw ?? 0));
  const matched = summary.events.filter((e) => e.n_cross_links > 0).length;
  const vm = summary.virtual_modis;
  const items: [string, string, string?][] = [
    [t("Tespit (filtre sonrası)", "Detections (after filter)"), fmtInt(st.n_observations_used)],
    [t("Aday olay", "Candidate events"), fmtInt(st.n_events), t(`${fmtInt(matched)} tanesinde MODIS–VIIRS eşleşmesi`, `${fmtInt(matched)} with a MODIS–VIIRS match`)],
    [t("Tespitli gün", "Days with detections"), `${daysWith} / ${cal.length}`, t("tespit yok ≠ yangın yok", "no detection ≠ no fire")],
    [t("En yüksek tekil FRP", "Highest single FRP"), `${fmtNum(maxFrp, 0)} MW`],
  ];
  if (vm?.available && vm.totals) {
    const tt = vm.totals as { virtual_mean: number; virtual_lo?: number; virtual_hi?: number; aqua_observed: number };
    items.push([t("Harmonize (MODIS-eşd.)", "Harmonized (MODIS-eq.)"), `≈ ${fmtNum(tt.virtual_mean, 0)}`,
      tt.virtual_lo !== undefined ? t(`%80: ${tt.virtual_lo}–${tt.virtual_hi} · gerçek Aqua ${tt.aqua_observed}`, `80%: ${tt.virtual_lo}–${tt.virtual_hi} · real Aqua ${tt.aqua_observed}`) : undefined]);
  }
  return (
    <div className="kpis">
      {items.map(([k, v, sub]) => (
        <div key={k} className="kpi"><div className="kpi-k">{k}</div><div className="kpi-v">{v}</div>{sub && <div className="kpi-s">{sub}</div>}</div>
      ))}
      <a className="kpi report" href={reportUrl} download>⬇ {t("Özet rapor", "Summary report")}<br /><span className="kpi-s">{t("tek sayfa PDF", "one-page PDF")}</span></a>
    </div>
  );
}

function Num({ label, v, step, on }: { label: string; v: number; step: number; on: (v: number) => void }) {
  const [txt, setTxt] = useState(String(v));
  useEffect(() => { setTxt(String(v)); }, [v]);
  return (
    <label className="row-l">
      {label}
      <input type="number" value={txt} step={step} min={0}
        onChange={(e) => { setTxt(e.target.value); const x = parseFloat(e.target.value); if (Number.isFinite(x) && x > 0) on(x); }}
        onBlur={() => setTxt(String(v))} />
    </label>
  );
}

function StatusLine({ ds, summary }: { ds: Summary["dataset"]; summary: Summary }) {
  const [open, setOpen] = useState(false);
  const cs = ds.chunk_stats;
  const st = summary.stats;
  return (
    <div className={"status" + (ds.is_sample ? " sample" : "")}>
      <span>
        <b>{ds.is_sample ? t("ÖRNEK VERİ", "SAMPLE DATA") : `${fmtDate(ds.start)} – ${fmtDate(ds.end)}`}</b>
        {" — "}{t(`${fmtInt(ds.n_observations)} gözlem, ${fmtInt(st.n_events)} aday olay`, `${fmtInt(ds.n_observations)} observations, ${fmtInt(st.n_events)} candidate events`)}
        {ds.segments.length > 0 && <> · {ds.segments.map((s) => s.source).join(", ")}</>}
        {ds.n_chunks > 0 && <> · {t(`${ds.n_chunks - (cs.out_of_range ?? 0)} istek (${ds.n_from_cache} önbellek)`, `${ds.n_chunks - (cs.out_of_range ?? 0)} requests (${ds.n_from_cache} cached)`)}</>}
        {ds.n_errors > 0 && <span className="err-t"> · {ds.n_errors} {t("hata", "errors")}</span>}
        {ds.warnings.length > 0 && <span className="warn-t"> · {ds.warnings.length} {t("uyarı", "warnings")}</span>}
      </span>{" "}
      {(ds.warnings.length > 0 || ds.n_errors > 0 || Object.keys(ds.products_versions).length > 0) && (
        <a href="#" onClick={(e) => { e.preventDefault(); setOpen(!open); }}>{open ? t("[gizle]", "[hide]") : t("[ayrıntı]", "[details]")}</a>
      )}
      {open && (
        <div className="status-more small">
          {Object.entries(ds.products_versions).map(([k, v]) => <div key={k}>{k}: {t("sürüm", "version")} {v.join(", ")}</div>)}
          {cs.empty ? <div>{t(`${cs.empty} parça sıfır tespit döndü (veri alındı, tespit yok).`, `${cs.empty} chunks returned zero detections (data received, no detections).`)}</div> : null}
          {cs.out_of_range ? <div>{t(`${cs.out_of_range} gün ürün kapsamı dışında (indirilmedi).`, `${cs.out_of_range} days outside product coverage (not downloaded).`)}</div> : null}
          {ds.errors.map((c, i) => <div key={i} className="err-t">{c.source} {c.start}: {ERROR_TITLES[c.error?.kind ?? ""] ?? c.error?.kind} — {c.error?.message}</div>)}
          {ds.warnings.map((w) => <div key={w} className="warn-t">• {w}</div>)}
          {ds.duplicates_removed > 0 && <div>{t(`${ds.duplicates_removed} gerçek kopya kayıt kaldırıldı.`, `${ds.duplicates_removed} exact duplicate records removed.`)}</div>}
        </div>
      )}
    </div>
  );
}
