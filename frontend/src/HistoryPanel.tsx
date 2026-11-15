import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import { api } from "./api";
import { t } from "./i18n";
import type { BBox, ClimCalibration, ClimMonth, ClimResult } from "./types";
import { COLORS, fmtDate, fmtNum, monthsLong, monthsShort } from "./util";

const RAMP = ["#fff1e0", "#fdd0a2", "#fdae6b", "#f16913", "#d94801", "#8c2d04"];
const DIV = ["#2c7bb6", "#abd9e9", "#f7f7f7", "#fdae61", "#d7191c"];

type Key = "combined" | "aqua" | "harm" | "viirs";
type Metric = "count" | "frp";
const SFX: Record<Metric, string> = { count: "", frp: "_frp" };
const series = (): { key: Key; label: string }[] => [
  { key: "combined", label: t("Birleşik kayıt (MODIS birimi)", "Combined record (MODIS units)") },
  { key: "aqua", label: t("Gerçek Aqua-MODIS", "Real Aqua-MODIS") },
  { key: "harm", label: t("Harmonize VIIRS (FireA)", "Harmonized VIIRS (FireA)") },
  { key: "viirs", label: t("Ham VIIRS (S-NPP)", "Raw VIIRS (S-NPP)") },
];
const unitLabel = (m: Metric) => (m === "frp" ? " MW" : "");
const srcLabel = (s: string | null | undefined) => (s === "harmonize VIIRS" ? t("harmonize VIIRS", "harmonized VIIRS") : s ?? "");

function val(m: ClimMonth | undefined | null, field: string): number | null {
  if (!m) return null;
  const v = m[field];
  return typeof v === "number" ? v : null;
}

interface Props {
  bbox: BBox;
  running: boolean;
  keyOk: boolean | null;
  runJob: (start: () => Promise<{ job_id: string }>, onDone: (r: ClimResult) => void) => void;
  result: ClimResult | null;
  setResult: (r: ClimResult | null) => void;
}

function verdict(z: number | null): { text: string; cls: string } {
  if (z === null) return { text: t("değerlendirilemedi", "not assessable"), cls: "" };
  if (z >= 2) return { text: t("olağandışı yüksek", "unusually high"), cls: "v-high" };
  if (z >= 1) return { text: t("tipik değerin üstünde", "above typical"), cls: "v-up" };
  if (z > -1) return { text: t("normal aralıkta", "within the normal range"), cls: "v-norm" };
  if (z > -2) return { text: t("tipik değerin altında", "below typical"), cls: "v-down" };
  return { text: t("olağandışı düşük", "unusually low"), cls: "v-low" };
}

function probText(p: number): string {
  const pct = Math.round(p * 100);
  const word = p >= 0.9 ? t("neredeyse kesin", "very likely") : p >= 0.66 ? t("muhtemel", "likely") : p >= 0.33 ? t("belirsiz", "uncertain") : t("düşük olasılık", "unlikely");
  return t(`olasılık %${pct}, ${word}`, `probability ${pct}%, ${word}`);
}

function median(a: number[]) {
  const s = [...a].sort((x, y) => x - y);
  return s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2;
}

function robust(base: number[], value: number, floor: number) {
  if (base.length < 3) return null;
  const med = median(base);
  const spread = Math.max(floor, 1.4826 * median(base.map((v) => Math.abs(v - med))));
  return { med, spread, z: (value - med) / spread };
}

function erf(x: number) {
  const s = Math.sign(x);
  const a = Math.abs(x);
  const k = 1 / (1 + 0.3275911 * a);
  return s * (1 - (((((1.061405429 * k - 1.453152027) * k) + 1.421413741) * k - 0.284496736) * k + 0.254829592) * k * Math.exp(-a * a));
}
const phi = (x: number) => 0.5 * (1 + erf(x / Math.SQRT2));

function probBeyond(value: number, thrHi: number, thrLo: number, cal: ClimCalibration) {
  const u = value / cal.unit;
  const sd = Math.sqrt(cal.spread[0] + cal.spread[1] / (u + 1));
  const pHi = 1 - phi((Math.log(thrHi / cal.unit + 1) - Math.log(u + 1)) / sd);
  const pLo = thrLo > 0 ? phi((Math.log(thrLo / cal.unit + 1) - Math.log(u + 1)) / sd) : 0;
  return { pHi, pLo };
}

function changeText(value: number, med: number): string {
  if (med <= 0) return value > 0 ? t("tipik değer sıfır", "the typical value is zero") : t("değişim yok", "no change");
  const r = value / med;
  if (r >= 1.5) return t(`tipik değerin ${fmtNum(r, r >= 10 ? 0 : 1)} katı`, `${fmtNum(r, r >= 10 ? 0 : 1)}× the typical value`);
  const pct = Math.round(Math.abs(r - 1) * 100);
  if (pct < 5) return t("tipik değere yakın", "close to typical");
  return r > 1 ? t(`%${pct} fazla`, `${pct}% higher`) : t(`%${pct} az`, `${pct}% lower`);
}

function linePath(pts: (number | null)[], x: (i: number) => number, y: (v: number) => number) {
  let d = "";
  let pen = false;
  pts.forEach((v, i) => {
    if (v === null) {
      pen = false;
      return;
    }
    d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
    pen = true;
  });
  return d;
}

function logTicks(vmax: number) {
  const out = [0];
  for (let p = 1; p <= vmax * 1.0001; p *= 10) out.push(p);
  return out.length > 6 ? out.filter((_, i) => i === 0 || i % 2 === 1) : out;
}

const sameBox = (a: number[], b: number[]) => a.length === b.length && a.every((v, i) => Math.abs(v - b[i]) < 1e-6);

function isHarmonized(m: ClimMonth | undefined | null, k: Key) {
  return !!m && (k === "harm" || (k === "combined" && m.combined_source === "harmonize VIIRS"));
}

function anomalyP(m: ClimMonth | undefined, k: Key, metric: Metric): { hi: number; lo: number; harm: boolean } {
  const f = k + SFX[metric];
  const z = val(m, `z_${f}`);
  const pHi = val(m, `p_hi_${f}`), pLo = val(m, `p_lo_${f}`);
  if (isHarmonized(m, k) && pHi !== null && pLo !== null) return { hi: pHi, lo: pLo, harm: true };
  return { hi: (z ?? 0) >= 2 ? 1 : 0, lo: (z ?? 0) <= -2 ? 1 : 0, harm: false };
}

export default function HistoryPanel({ bbox, running, keyOk, runJob, result, setResult }: Props) {
  const [refresh, setRefresh] = useState(false);
  const start = () => {
    if (running) return;
    runJob(() => api.climatologyJob(bbox, refresh), (r) => setResult(r));
  };
  const stale = result && !sameBox(result.bbox, bbox);
  return (
    <div className="center-stack">
      <div className="card">
        <div className="card-head">
          <h3>{t("Tarihsel yanma takvimi", "Historical burning calendar")}</h3>
          <span className="muted small">{t("Seçili bölge · 2003'ten bugüne · ay ay", "Selected area · 2003 to today · month by month")}</span>
        </div>
        <div className="hist-form">
          <span className="small">{t("Bölge", "Area")}: <b>{bbox.join(", ")}</b></span>
          <label className="check small"><input type="checkbox" checked={refresh} onChange={(e) => setRefresh(e.target.checked)} /> {t("yeniden hesapla (önbelleği yok say)", "recompute (ignore cache)")}</label>
          <button className="go inline-go" disabled={running || !keyOk} onClick={start}>{t("Takvimi hesapla", "Compute calendar")}</button>
        </div>
        <p className="hint">
          {t("Bölgenin 2003'ten bu yana tüm MODIS tespitleri ile VIIRS S-NPP (2012→), NOAA-20 (2018→) ve NOAA-21 (2023→) tespitleri indirilir; VIIRS FireA ile MODIS birimine çevrilir, örtüşme yıllarına göre kalibre edilir ve ay ay toplanır. İl büyüklüğünde bir bölge için ilk hesaplama 20–45 dakika sürebilir; sonra önbellekten gelir. İlerleme sol panelde görünür, istediğiniz an iptal edebilirsiniz.",
            "All MODIS detections since 2003 and VIIRS S-NPP (2012→), NOAA-20 (2018→) and NOAA-21 (2023→) detections are downloaded for the area; VIIRS is converted to MODIS units with FireA, calibrated on the overlap years and summed per month. For a province-sized area the first run can take 20–45 minutes; afterwards it comes from the cache. Progress is shown in the left panel and you can cancel at any time.")}
        </p>
        {stale && <p className="msg info small">{t(`Aşağıdaki sonuç önceki bölgeye ait (${result!.bbox.join(", ")}). Yeni bölge için “Takvimi hesapla”ya basın.`, `The result below belongs to the previous area (${result!.bbox.join(", ")}). Press “Compute calendar” for the new area.`)}</p>}
      </div>
      {result && <ClimView key={result.bbox.join(",") + result.last_day} r={result} />}
    </div>
  );
}

function ClimView({ r }: { r: ClimResult }) {
  const [k, setK] = useState<Key>("combined");
  const [metric, setMetric] = useState<Metric>("count");
  const first = useMemo(() => {
    let best: ClimMonth | null = null;
    for (const m of r.months) if (!m.partial && (val(m, "z_combined") ?? -99) > (val(best, "z_combined") ?? -99)) best = m;
    return best ? { year: best.year, month: best.month } : { year: r.first_year, month: 8 };
  }, [r]);
  const [sel, setSel] = useState(first);
  const notes = [
    t("Birleşik kayıt: Aqua-MODIS'in karşılaştırılabilir olduğu her ayda MODIS'in kendisi, diğer aylarda kalibre edilmiş harmonize VIIRS (MODIS birimi) kullanılır.",
      "Combined record: real Aqua-MODIS wherever it is comparable, calibrated harmonized VIIRS (MODIS units) for the other months."),
    t("Harmonize tespit sayısı, örtüşme yıllarından bu bölgeye göre önce küme boyutuna, sonra mevsime göre kalibre edilir. Harmonize FRP, FireA'nın MODIS algılama olasılığıyla ağırlıklandırılmış VIIRS FRP'sidir; kalibrasyon hatayı artırdığı için ölçeklenmez. Belirsizlik aralıkları örtüşme yıllarındaki gerçek hatalardan hesaplanır.",
      "Harmonized counts are calibrated for this area on the overlap years, first by cluster size and then by season. Harmonized FRP is VIIRS FRP weighted by FireA's MODIS detection probability; it is not rescaled because calibration increased its error. Uncertainty intervals come from the real errors in the overlap years."),
    t("S-NPP, NOAA-20 ve NOAA-21 tahminlerinin ortalaması alınır. FireA NOAA-21 ile eğitilmediği için NOAA-21, ortak aylarda S-NPP/NOAA-20 ortalamasına göre ölçeklenir.",
      "S-NPP, NOAA-20 and NOAA-21 estimates are averaged. FireA was not trained on NOAA-21, so NOAA-21 is scaled to the S-NPP/NOAA-20 average over common months."),
    t("Aqua'nın geçiş saati kaydığı yıllarda (S-NPP'ye göre 15 dakikadan fazla) birleşik kayıt harmonize VIIRS'i kullanır. Harmonize aylarda olağandışılık bir olasılık olarak verilir.",
      "In years when Aqua's overpass time has drifted (more than 15 minutes from S-NPP), the combined record uses harmonized VIIRS. For harmonized months unusualness is given as a probability."),
    t("Günlerinin %90'ından azında veri bulunan aylar boş bırakılır; tespit olmaması yangın olmadığı anlamına gelmez.",
      "Months with data on fewer than 90% of days are left empty; no detection does not mean no fire."),
  ];
  return (
    <>
      <div className="card metric-bar">
        <span className="small">{t("Gösterilen ölçü", "Metric shown")}:</span>
        <span className="seg" role="group" aria-label={t("Ölçü", "Metric")}>
          <button className={metric === "count" ? "on" : ""} aria-pressed={metric === "count"} onClick={() => setMetric("count")}>{t("Tespit sayısı", "Detection count")}</button>
          <button className={metric === "frp" ? "on" : ""} aria-pressed={metric === "frp"} onClick={() => setMetric("frp")}>{t("Yangın gücü (FRP, MW)", "Fire power (FRP, MW)")}</button>
        </span>
        <span className="muted small">{metric === "frp"
          ? t("Aylık FRP toplamı: o aydaki tüm tespitlerin yayınım gücü toplamı (MW). Yangının şiddetini de yansıtır.", "Monthly FRP sum: total radiative power of all detections in the month (MW). It also reflects fire intensity.")
          : t("Aylık aktif yangın tespiti sayısı (MODIS birimi).", "Monthly number of active-fire detections (MODIS units).")}</span>
      </div>
      <RecentCard r={r} />
      <MonthVsMedianCard r={r} metric={metric} onSelect={setSel} />
      <ValidationCard r={r} metric={metric} />
      <Heatmap r={r} k={k} setK={setK} metric={metric} sel={sel} onSelect={setSel} />
      <AnomalyMap r={r} metric={metric} sel={sel} />
      <SeasonCard r={r} metric={metric} />
      <TopCard r={r} metric={metric} onSelect={setSel} />
      <ContinuousCard r={r} metric={metric} />
      <CellAnomaly r={r} k={k} metric={metric} sel={sel} />
      <AgreementCard r={r} metric={metric} month={sel.month} year={sel.year} />
      <div className="card"><ul className="flags plain">{notes.map((n) => <li key={n}>{n}</li>)}</ul></div>
    </>
  );
}

function RecentCard({ r }: { r: ClimResult }) {
  const rc = r.recent;
  if (!rc) return null;
  const v = verdict(rc.robust_z);
  const up = (rc.robust_z ?? 0) >= 0;
  const p = rc.source === "harmonize VIIRS" && rc.p_high !== null && rc.p_low !== null ? (up ? rc.p_high : rc.p_low) : null;
  return (
    <div className="card">
      <div className="card-head"><h3>{t("Yılın bu zamanı: son 7 gün normal mi?", "This time of year: are the last 7 days normal?")}</h3><span className="muted small">{t("erken uyarı için mevsimsel karşılaştırma", "seasonal comparison for early warning")}</span></div>
      <p className={"verdict " + v.cls}>
        <b>{fmtDate(rc.start)} – {fmtDate(rc.end)}: {v.text}.</b>{" "}
        {t(`Bu 7 günde ${fmtNum(rc.value, 0)} tespit (MODIS birimi, ${srcLabel(rc.source)}).`, `${fmtNum(rc.value, 0)} detection${Math.round(rc.value) === 1 ? "" : "s"} in these 7 days (MODIS units, ${srcLabel(rc.source)}).`)}
        {rc.baseline_median !== null && <> {t(`Önceki ${rc.n_years} yılın aynı 7 gününde tipik değer ${fmtNum(rc.baseline_median, 0)}; bu yıl ${changeText(rc.value, rc.baseline_median)}.`,
          `Typical value for the same 7 days in the previous ${rc.n_years} years: ${fmtNum(rc.baseline_median, 0)}; this year ${changeText(rc.value, rc.baseline_median)}.`)}</>}
        {p !== null && <> {t(`Olağandışı ${up ? "yüksek" : "düşük"} olma olasılığı yaklaşık %${Math.round(p * 100)}.`, `Probability of being unusually ${up ? "high" : "low"}: about ${Math.round(p * 100)}%.`)}</>}
      </p>
      <p className="hint">{t("Son günlerin verisi NRT'dir ve birkaç saat gecikmeli olabilir. Tespit olmaması yangın olmadığı anlamına gelmez.", "The latest days use NRT data and can be a few hours late. No detection does not mean no fire.")}</p>
    </div>
  );
}

function ValidationCard({ r, metric }: { r: ClimResult; metric: Metric }) {
  const c = metric === "frp" ? r.calibration_frp : r.calibration;
  const v = c?.validation;
  const ML = monthsShort();
  const pct = (x: number | null | undefined) => (x === null || x === undefined ? "—" : t(`%${fmtNum(x, 1)}`, `${fmtNum(x, 1)}%`));
  const sats = (r.calibration?.viirs_sats ?? ["S-NPP"]).join(" + ");
  const drift = r.calibration?.drifted_years ?? [];
  const dm = r.calibration?.aqua_drift_min ?? {};
  const sf = c?.season_factor ?? [];
  const sc = r.calibration?.size_correction;
  const mc = r.model_choice;
  const other = mc?.candidates.find((x) => x.name !== mc.chosen);
  const chosen = mc?.candidates.find((x) => x.name === mc.chosen);
  const ms = v?.multi_sat;
  const winter = sf.length ? Math.max(...sf) : null;
  const summer = sf.length ? Math.min(...sf) : null;
  const driftList = (unit: string) => drift.map((y) => `${y}: +${Math.round(dm[String(y)] ?? 0)} ${unit}`).join(", ");
  const driftNote = drift.length > 0 && (
    <p className="hint">{t(`Aqua uydusunun geçiş saati ${drift[0]}'ten itibaren kaydı (S-NPP'ye göre ${driftList("dk")}). Aqua bu yıllarda yangınlara günün daha geç bir saatinde baktığı için karşılaştırılabilir değil; birleşik kayıt bu yıllarda harmonize VIIRS'i kullanır ve bu yıllar doğrulamaya katılmaz. Kaymış Aqua'yı düzeltmeyi denedik; ama FireA saat farkının etkisini fiziksel olarak öğrenmediği için düzeltme doğrulanamadı ve uygulanmadı.`,
      `Aqua's overpass time has drifted since ${drift[0]} (relative to S-NPP: ${driftList("min")}). In these years Aqua looks at fires later in the day, so it is not comparable; the combined record uses harmonized VIIRS for these years and they are left out of the validation. We tried to correct the drifted Aqua data, but FireA has not learned the time-difference effect physically, so the correction could not be validated and is not applied.`)}</p>
  );
  if (!v) {
    return (
      <div className="card">
        <div className="card-head"><h3>{t("Doğrulama", "Validation")}</h3></div>
        <p className="hint">{t("Bu bölgede iki sensörün birlikte çalıştığı yeterli ay yok; yerel kalibrasyon yapılamadı ve genel (varsayılan) belirsizlik kullanıldı.",
          "This area has too few months in which both sensors operated; no local calibration was possible and the general (default) uncertainty is used.")}</p>
        {driftNote}
      </div>
    );
  }
  const frp = metric === "frp";
  return (
    <div className="card">
      <div className="card-head"><h3>{frp ? t("Doğrulama: harmonize FRP gerçek MODIS FRP'sine ne kadar yakın?", "Validation: how close is harmonized FRP to real MODIS FRP?")
        : t("Doğrulama: harmonize VIIRS gerçek MODIS'e ne kadar yakın?", "Validation: how close is harmonized VIIRS to real MODIS?")}</h3>
        <span className="muted small">{t(`${v.years[0]}–${v.years[1]} · ${v.n_months} ay · her yıl dışarıda bırakılarak`, `${v.years[0]}–${v.years[1]} · ${v.n_months} months · leave-one-year-out`)}</span></div>
      <div className="table-wrap">
        <table className="tbl valid">
          <thead><tr><th />
            <th>{t("Bu yöntem", "This method")}<div className="muted small">{frp ? t(`FireA algılama olasılığı × VIIRS FRP (${sats})`, `FireA detection probability × VIIRS FRP (${sats})`) : sc?.applied ? t(`FireA (${sats}) + küme boyutu düzeltmesi + mevsimsel kalibrasyon`, `FireA (${sats}) + cluster-size correction + seasonal calibration`) : t(`FireA (${sats}) + mevsimsel kalibrasyon`, `FireA (${sats}) + seasonal calibration`)}</div></th>
            {!frp && sc?.applied && <th>{t("Yalnızca mevsimsel", "Seasonal only")}<div className="muted small">{t("küme boyutu düzeltmesi olmadan", "without the cluster-size correction")}</div></th>}
            {!frp && <th>{t("FireA (ham)", "FireA (raw)")}<div className="muted small">{t("kalibrasyonsuz", "uncalibrated")}</div></th>}
            <th>{t("Sabit oran", "Constant ratio")}<div className="muted small">{frp ? t("VIIRS FRP × yerel oran", "VIIRS FRP × local ratio") : "VIIRS × 0.149"}</div></th></tr></thead>
          <tbody>
            <tr><th>{t(`Aylık medyan hata (≥20 tespitli ${v.n_active_months} ay)`, `Median monthly error (${v.n_active_months} months with ≥20 detections)`)}</th>
              <td><b>{pct(v.harm_month_err_pct)}</b></td>{!frp && sc?.applied && <td>{pct(v.season_month_err_pct)}</td>}{!frp && <td>{pct(v.raw_month_err_pct)}</td>}<td>{pct(v.const_month_err_pct)}</td></tr>
            <tr><th>{t("Yıllık toplamda medyan hata", "Median error of yearly totals")}</th>
              <td><b>{pct(v.harm_year_err_pct)}</b></td>{!frp && sc?.applied && <td>{pct(v.season_year_err_pct)}</td>}{!frp && <td>{pct(v.raw_year_err_pct)}</td>}<td>{pct(v.const_year_err_pct)}</td></tr>
            {!frp && <tr><th>{t("Yıllık toplamda ortalama hata", "Mean error of yearly totals")}</th>
              <td><b>{pct(v.harm_year_err_mean_pct)}</b></td>{sc?.applied && <td>{pct(v.season_year_err_mean_pct)}</td>}<td>—</td><td>—</td></tr>}
            <tr><th>{frp ? t("Ortalama mutlak hata (MW/ay)", "Mean absolute error (MW/month)") : t("Ortalama mutlak hata (tespit/ay)", "Mean absolute error (detections/month)")}</th>
              <td><b>{fmtNum(v.harm_mae, frp ? 0 : 1)}</b></td>{!frp && sc?.applied && <td>{fmtNum(v.season_mae, 1)}</td>}{!frp && <td>{fmtNum(v.raw_mae, 1)}</td>}<td>{fmtNum(v.const_mae, frp ? 0 : 1)}</td></tr>
            <tr><th>{t("%80 aralığın gerçek değeri kapsaması", "Share of real values inside the 80% interval")}</th>
              <td><b>{pct(v.coverage80_calibrated * 100)}</b></td>{!frp && sc?.applied && <td>—</td>}{!frp && <td>{v.coverage80_raw === null ? "—" : pct(v.coverage80_raw * 100)}</td>}<td>—</td></tr>
          </tbody>
        </table>
      </div>
      {!frp && mc && chosen && other && (
        <p className="hint">{t(`Model seçimi: bu bölgede iki FireA sürümü denendi; her yıl dışarıda bırakılarak yapılan testte aylık ortalama mutlak hatası düşük olan kullanıldı: ${chosen.name} (${fmtNum(chosen.harm_mae, 1)} tespit/ay). Diğeri ${other.name}: ${fmtNum(other.harm_mae, 1)}. Kalibrasyonsuz (ham) hâlleriyle aylık medyan hata: ${chosen.name} ${pct(chosen.raw_month_err_pct)}, ${other.name} ${pct(other.raw_month_err_pct)}.`,
          `Model choice: two FireA versions were tried in this area; the one with the lower monthly mean absolute error in the leave-one-year-out test is used: ${chosen.name} (${fmtNum(chosen.harm_mae, 1)} detections/month). The other, ${other.name}: ${fmtNum(other.harm_mae, 1)}. Median monthly error without calibration (raw): ${chosen.name} ${pct(chosen.raw_month_err_pct)}, ${other.name} ${pct(other.raw_month_err_pct)}.`)}</p>
      )}
      {!frp && sc && !sc.applied && sc.check && (
        <p className="hint">{t(`Küme boyutu düzeltmesi denendi (${fmtNum(sc.n_labels, 0)} eşleşmiş Aqua tespitiyle), ama bu bölgede çapraz doğrulamada hatayı azaltmadığı için uygulanmadı: ortalama mutlak hata düzeltmeyle ${fmtNum(sc.check.mae_with, 2)}, düzeltmesiz ${fmtNum(sc.check.mae_without, 2)} tespit/ay.`,
          `A cluster-size correction was tested (with ${fmtNum(sc.n_labels, 0)} matched Aqua detections) but not applied, because it did not lower the cross-validated error in this area: mean absolute error ${fmtNum(sc.check.mae_with, 2)} with it, ${fmtNum(sc.check.mae_without, 2)} detections/month without.`)}</p>
      )}
      {!frp && sc?.applied && (
        <p className="hint">{t(`Küme boyutu düzeltmesi: bu bölgede ${fmtNum(sc.n_labels, 0)} eşleşmiş Aqua tespitiyle karşılaştırıldığında FireA, küçük VIIRS kümeleri için fazla tahmin yapıyor. Tahminler küme boyutuna göre düzeltilir: gündüz tek pikselli kümeler ×${fmtNum(sc.day[0], 2)}, 20'den büyük kümeler ×${fmtNum(sc.day[4], 2)}; gece tek pikselli ×${fmtNum(sc.night[0], 2)}, 20'den büyük ×${fmtNum(sc.night[4], 2)}. Doğrulamada bu çarpanlar da dışarıda bırakılan yıl olmadan yeniden öğrenilir.`,
          `Cluster-size correction: compared with ${fmtNum(sc.n_labels, 0)} matched Aqua detections in this area, FireA overestimates small VIIRS clusters. Estimates are corrected by cluster size: daytime single-pixel clusters ×${fmtNum(sc.day[0], 2)}, clusters above 20 pixels ×${fmtNum(sc.day[4], 2)}; night single-pixel ×${fmtNum(sc.night[0], 2)}, above 20 pixels ×${fmtNum(sc.night[4], 2)}. In the validation these factors are also re-learned without the held-out year.`)}</p>
      )}
      {!frp && ms && (
        <p className="hint">{t(`İki VIIRS uydusunun birlikte çalıştığı dönemde (${ms.years[0]}–${ms.years[1]}, ${ms.n_months} ay) yıllık hata daha düşük: medyan ${pct(ms.harm_year_err_pct)}, ortalama ${pct(ms.harm_year_err_mean_pct)}. Aynı dönemde aylık medyan hata ${pct(ms.harm_month_err_pct)} (${ms.n_active_months} ay). Bugünkü kayıt da birden çok uyduya dayanıyor.`,
          `When two VIIRS satellites operated together (${ms.years[0]}–${ms.years[1]}, ${ms.n_months} months) the yearly error is lower: median ${pct(ms.harm_year_err_pct)}, mean ${pct(ms.harm_year_err_mean_pct)}. The median monthly error in that period is ${pct(ms.harm_month_err_pct)} (${ms.n_active_months} months). Today's record also relies on several satellites.`)}</p>
      )}
      {!frp && winter !== null && summer !== null && (
        <p className="hint">{t(`Mevsimsel kalibrasyon: Aqua tespitlerinin bir bölümünün yakınında hiç VIIRS tespiti yoktur ve bu pay kışın çok daha yüksektir. Bu yüzden harmonize değer kışın en çok ×${fmtNum(winter, 2)} (${ML[sf.indexOf(winter)]}), yazın en az ×${fmtNum(summer, 2)} (${ML[sf.indexOf(summer)]}) ile çarpılır. Düzeltme bu bölgenin örtüşme yıllarından öğrenilir.`,
          `Seasonal calibration: part of the Aqua detections have no VIIRS detection nearby, and this share is much higher in winter. Harmonized values are therefore multiplied by up to ×${fmtNum(winter, 2)} in winter (${ML[sf.indexOf(winter)]}) and down to ×${fmtNum(summer, 2)} in summer (${ML[sf.indexOf(summer)]}). The correction is learned from this area's overlap years.`)}</p>
      )}
      {frp && (
        <p className="hint">{t(`MODIS yalnızca algıladığı yangınların FRP'sini görür. Bu yüzden her VIIRS kümesinin FRP'si, FireA'nın "MODIS bu kümeyi algılar" olasılığıyla çarpılıp toplanır. Örtüşme yıllarında seviye kalibrasyonu hatayı artırdığı için uygulanmaz; yalnızca belirsizlik aralığı kalibre edilir. FRP büyük yangınlarda çok oynak olduğundan hatası tespit sayısından yüksektir. Bu bölgede 1 tespit ≈ ${fmtNum(c.unit, 0)} MW.`,
          `MODIS only sees the FRP of fires it detects, so each VIIRS cluster's FRP is multiplied by FireA's probability that MODIS detects it, and summed. A level calibration on the overlap years increased the error, so it is not applied; only the uncertainty interval is calibrated. FRP is very variable in large fires, so its error is higher than for counts. In this area 1 detection ≈ ${fmtNum(c.unit, 0)} MW.`)}</p>
      )}
      {!frp && r.calibration?.xcal_noaa21 && (
        <p className="hint">{t(`NOAA-21: FireA bu uyduyla eğitilmediği için tahminleri, ortak aylarda S-NPP/NOAA-20 ortalamasına göre ×${fmtNum(r.calibration.xcal_noaa21, 2)} ile ölçeklenip ortalamaya katılır.`,
          `NOAA-21: FireA was not trained on this satellite, so its estimates are scaled by ×${fmtNum(r.calibration.xcal_noaa21, 2)} to the S-NPP/NOAA-20 average over common months before averaging.`)}</p>
      )}
      {driftNote}
      <p className="hint">{t("Birleşik kayıtta Aqua'nın karşılaştırılabilir olduğu aylar gerçek MODIS'tir; bu tablo MODIS olmayan aylarda (ve MODIS emekliye ayrıldıktan sonra) beklenen hatayı gösterir. Kalan hatanın büyük kısmı, MODIS'in her gün farklı açıdan bakmasından kaynaklanan ölçüm gürültüsüdür.",
        "In the combined record, months where Aqua is comparable are real MODIS; this table shows the error to expect for months without MODIS (and after MODIS retires). Most of the remaining error is measurement noise from MODIS's changing daily viewing geometry.")}</p>
    </div>
  );
}

function Heatmap({ r, k, setK, metric, sel, onSelect }: {
  r: ClimResult; k: Key; setK: (k: Key) => void; metric: Metric; sel: { year: number; month: number }; onSelect: (s: { year: number; month: number }) => void;
}) {
  const MS = monthsShort();
  const ML = monthsLong();
  const f = k + SFX[metric];
  const u = unitLabel(metric);
  const years = Array.from(new Set(r.months.map((m) => m.year))).sort((a, b) => a - b);
  const cell = new Map(r.months.map((m) => [`${m.year}-${m.month}`, m]));
  const color = heatScale(r, f);
  const season = metric === "frp" ? r.season_frp : r.season;
  const crit = new Set(metric === "frp" ? r.critical_months_frp : r.critical_months);
  const drifted = new Set(r.calibration?.drifted_years ?? []);
  return (
    <div className="card">
      <div className="card-head">
        <h3>{t("Yanma takvimi (ay × yıl)", "Burning calendar (month × year)")}{metric === "frp" ? " · FRP" : ""}</h3>
        <span className="small dl-links" style={{ marginLeft: "auto" }}>{t("Takvimi indir", "Download calendar")}:{" "}
          <button className="link" onClick={() => downloadCalendarCsv(r)} title={t("Ay × yıl tablosu: tüm seriler, kaynak, %80 aralık ve olağandışılık", "Month × year table: all series, source, 80% interval and unusualness")}>CSV</button>{" · "}
          <button className="link" onClick={() => downloadCalendarPng(r, k, metric)} title={t("Ekrandaki takvimin aynı renklerle resmi", "Image of the calendar as shown, with the same colours")}>PNG</button>
        </span>
        <label className="small">{t("Seri", "Series")}{" "}
          <select value={k} onChange={(e) => setK(e.target.value as Key)}>
            {series().map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
        </label>
      </div>
      <div className="heat">
        <div className="heat-row head">
          <span className="heat-y" />
          {MS.map((m, i) => <span key={m} className={"heat-m" + (crit.has(i + 1) ? " crit" : "")}>{m}{crit.has(i + 1) ? "★" : ""}</span>)}
          <span className="heat-t">{t("yıllık", "year")}</span>
        </div>
        {years.map((y) => {
          const ann = r.annual.find((a) => a.year === y);
          const annV = ann && !ann.partial ? (ann[f] as number | null) : null;
          return (
            <div key={y} className="heat-row">
              <span className="heat-y">{y}{y === r.viirs_start_year ? " ▸" : ""}{drifted.has(y) ? " ⌁" : ""}</span>
              {MS.map((_, i) => {
                const m = cell.get(`${y}-${i + 1}`);
                const v = val(m, f);
                const z = val(m, `z_${f}`);
                const a = anomalyP(m, k, metric);
                const isSel = sel.year === y && sel.month === i + 1;
                const p = Math.max(a.hi, a.lo);
                const cls = "heat-c" + (v === null ? " na" : "") + (m?.partial ? " partial" : "") + (a.hi >= 0.5 ? " hi" : "") +
                  (a.lo >= 0.5 ? " lo" : "") + (a.harm && p >= 0.5 && p < 0.9 ? " maybe" : "") + (isSel ? " sel" : "");
                const title = m
                  ? `${ML[i]} ${y}: ${v === null ? t("veri yetersiz", "insufficient data") : fmtNum(v, 0) + u}${k === "combined" && m.combined_source ? ` (${srcLabel(m.combined_source)})` : ""}` +
                    `${z !== null ? ` · ${verdict(z).text}` : ""}${a.harm && p >= 0.05 ? ` (${probText(p)})` : ""}${m.partial ? t(" · ay henüz bitmedi", " · month not finished") : ""}` +
                    `${drifted.has(y) ? t(" · Aqua geçiş saati kaymış", " · Aqua overpass time drifted") : ""}`
                  : `${ML[i]} ${y}: ${t("kapsam dışı", "out of range")}`;
                return (
                  <button key={i} className={cls} style={v !== null ? { background: color(v) } : undefined} title={title} aria-label={title}
                    onClick={() => m && onSelect({ year: y, month: i + 1 })}>{a.hi >= 0.9 ? "!" : a.hi >= 0.5 ? "?" : ""}</button>
                );
              })}
              <span className="heat-t">{annV !== null && annV !== undefined ? fmtNum(annV, 0) : ""}</span>
            </div>
          );
        })}
        <div className="heat-row foot">
          <span className="heat-y"><b>{t("Tipik", "Typical")}</b></span>
          {season.map((s) => (
            <span key={s.month} className={"heat-c typ" + (s.critical ? " crit" : "")} style={{ background: s.median !== null ? color(s.median) : undefined }}
              title={t(`${ML[s.month - 1]}: tipik ${fmtNum(s.median, 0)}${u} (medyan, ${s.n_years} yıl)${s.critical ? " · kritik ay" : ""}`,
                `${ML[s.month - 1]}: typical ${fmtNum(s.median, 0)}${u} (median, ${s.n_years} years)${s.critical ? " · critical month" : ""}`)} />
          ))}
          <span className="heat-t" />
        </div>
      </div>
      <div className="harmony-foot">
        <span className="legend-item">{t("az", "low")}</span>
        {RAMP.map((c) => <span key={c} className="swatch" style={{ background: c }} />)}
        <span className="legend-item">{t("çok (log ölçek)", "high (log scale)")}</span>
        <span className="legend-item"><span className="swatch hi-sw">!</span>{t("olağandışı yüksek (kesin ya da olasılık ≥%90)", "unusually high (certain or probability ≥90%)")}</span>
        <span className="legend-item"><span className="swatch hi-sw maybe-sw">?</span>{t("muhtemelen olağandışı (%50–90)", "possibly unusual (50–90%)")}</span>
        <span className="legend-item"><span className="swatch lo-sw" />{t("olağandışı düşük", "unusually low")}</span>
        <span className="legend-item"><span className="swatch na-sw" />{t("veri yetersiz", "insufficient data")}</span>
        <span className="legend-item">{t("★ kritik ay · ▸ VIIRS başlangıcı · ⌁ Aqua saat kayması", "★ critical month · ▸ VIIRS start · ⌁ Aqua time drift")}</span>
      </div>
      <p className="hint">{t("Her satır bir yıl, her sütun bir ay; renk o ayın yanma etkinliğidir. \"Olağandışı\", o ayın diğer yıllardaki tipik değerine göre belirlenir. Gerçek MODIS aylarında hüküm kesindir; harmonize aylarda tahminin belirsizliği nedeniyle olasılık olarak verilir. Bir hücreye tıklayınca aşağıda ayrıntılı analizi ve anomali haritası açılır.",
        "Each row is a year, each column a month; colour is that month's burning activity. \"Unusual\" is judged against the typical value of the same month in other years. For real MODIS months the verdict is definite; for harmonized months it is a probability because of the estimate's uncertainty. Click a cell to open its detailed analysis and anomaly map below.")}</p>
    </div>
  );
}

function AnomalyMap({ r, metric, sel }: { r: ClimResult; metric: Metric; sel: { year: number; month: number } }) {
  const div = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const layer = useRef<L.LayerGroup | null>(null);
  const g = r.grid;
  const ym = `${sel.year}-${String(sel.month).padStart(2, "0")}`;
  const cal = metric === "frp" ? r.calibration_frp : r.calibration;
  const ML = monthsLong();
  const u = unitLabel(metric);
  const stats = useMemo(() => {
    if (!g) return null;
    const n = g.nx * g.ny;
    const toArr = (key: string) => {
      const rows = g[metric][key];
      if (!rows) return null;
      const a = new Float64Array(n);
      rows.forEach(([i, v]) => { a[i] = v; });
      return a;
    };
    const cur = g.source[ym] ? toArr(ym) : null;
    if (!cur) return null;
    const partial = new Set(r.months.filter((m) => m.partial).map((m) => m.ym));
    const others = Object.keys(g.source).filter((key) => key.slice(5) === ym.slice(5) && key !== ym && !partial.has(key))
      .map(toArr).filter((a): a is Float64Array => a !== null);
    const harm = g.source[ym] === "h";
    const floor = g.cell_min_spread[metric];
    const cells: { i: number; v: number; med: number; z: number; p: number | null }[] = [];
    for (let i = 0; i < n; i++) {
      const st = robust(others.map((a) => a[i]), cur[i], floor);
      if (!st || (cur[i] === 0 && st.med === 0)) continue;
      let p: number | null = null;
      if (harm) {
        const pb = probBeyond(cur[i], st.med + 2 * st.spread, st.med - 2 * st.spread, cal);
        p = st.z >= 0 ? pb.pHi : pb.pLo;
      }
      cells.push({ i, v: cur[i], med: st.med, z: st.z, p });
    }
    return { cells, harm, nYears: others.length };
  }, [g, ym, metric, cal, r.months]);

  useEffect(() => {
    if (!div.current || mapRef.current) return;
    const map = L.map(div.current, { zoomControl: true, scrollWheelZoom: false });
    L.tileLayer("https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 13, attribution: "Esri, HERE, Garmin, © OpenStreetMap contributors" }).addTo(map);
    map.createPane("labels");
    map.getPane("labels")!.style.zIndex = "450";
    map.getPane("labels")!.style.pointerEvents = "none";
    L.tileLayer("https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 13, pane: "labels" }).addTo(map);
    layer.current = L.layerGroup().addTo(map);
    const [w, s, e, n] = r.bbox;
    map.fitBounds([[s, w], [n, e]], { padding: [8, 8] });
    mapRef.current = map;
    return () => {
      layer.current?.remove();
      map.remove();
      mapRef.current = null;
    };
  }, [r.bbox]);

  useEffect(() => {
    const lg = layer.current;
    if (!lg || !g) return;
    lg.clearLayers();
    if (!stats) return;
    for (const c of stats.cells) {
      const s = g.south + Math.floor(c.i / g.nx) * g.size;
      const w = g.west + (c.i % g.nx) * g.size;
      const idx = c.z >= 2 ? 4 : c.z >= 1 ? 3 : c.z > -1 ? 2 : c.z > -2 ? 1 : 0;
      const strong = stats.harm ? (c.p ?? 0) >= 0.9 && Math.abs(c.z) >= 1 : Math.abs(c.z) >= 2;
      const tip = `<b>${fmtNum(c.v, 0)}${u}</b> · ${t("tipik", "typical")} ${fmtNum(c.med, 0)}${u}<br>${verdict(c.z).text}` +
        (stats.harm && c.p !== null && Math.abs(c.z) >= 1 ? ` (${probText(c.p)})` : "") +
        `<br><span class="muted">${s.toFixed(2)}–${(s + g.size).toFixed(2)}°N, ${w.toFixed(2)}–${(w + g.size).toFixed(2)}°E</span>`;
      L.rectangle([[s, w], [s + g.size, w + g.size]], {
        color: strong ? (c.z > 0 ? "#ffef5a" : "#8fd3ff") : "rgba(255,255,255,0.25)", weight: strong ? 2.2 : 0.5,
        fillColor: DIV[idx], fillOpacity: idx === 2 ? 0.18 : 0.62,
      }).bindTooltip(tip, { sticky: true }).addTo(lg);
    }
  }, [stats, g, u]);

  const nUnusual = stats ? stats.cells.filter((c) => c.z >= 2 && (!stats.harm || (c.p ?? 0) >= 0.9)).length : 0;
  return (
    <div className="card">
      <div className="card-head"><h3>{t("Anomali haritası", "Anomaly map")}: {ML[sel.month - 1]} {sel.year}{metric === "frp" ? " · FRP" : ""}</h3>
        <span className="muted small">{t(`${g ? g.size : "—"}° hücreler · her hücre kendi geçmişiyle karşılaştırılır`, `${g ? g.size : "—"}° cells · each cell is compared with its own history`)}</span></div>
      {!stats && <p className="hint">{t("Bu ay için harita verisi yok.", "No map data for this month.")}</p>}
      {stats && (
        <p className="hint">
          {t(`Renk, hücrenin bu ayki değerinin aynı ayın diğer ${stats.nYears} yılındaki tipik değerinden sapmasıdır. Kalın çerçeveli hücreler olağandışıdır`, `Colour shows how far each cell's value departs from its typical value for the same month in the other ${stats.nYears} years. Cells with a thick outline are unusual`)}
          {stats.harm ? t(" (harmonize ay: olasılık ≥%90).", " (harmonized month: probability ≥90%).") : t(" (gerçek MODIS ayı).", " (real MODIS month).")}{" "}
          <b>{t(`${nUnusual} hücre olağandışı yüksek.`, `${nUnusual} cell${nUnusual === 1 ? "" : "s"} unusually high.`)}</b>
        </p>
      )}
      <div ref={div} className="anom-map" />
      <div className="harmony-foot">
        {DIV.map((c, i) => <span key={c} className="legend-item"><span className="swatch" style={{ background: c }} />{[t("olağandışı düşük", "unusually low"), t("tipik altı", "below typical"), t("normal", "normal"), t("tipik üstü", "above typical"), t("olağandışı yüksek", "unusually high")][i]}</span>)}
        <span className="legend-item muted">{t("Hücre düzeyinde sayılar küçük olduğundan gürültü daha fazladır.", "Cell-level counts are small, so they are noisier.")}</span>
      </div>
    </div>
  );
}

function SeasonCard({ r, metric }: { r: ClimResult; metric: Metric }) {
  const MS = monthsShort();
  const ML = monthsLong();
  const u = unitLabel(metric);
  const season = metric === "frp" ? r.season_frp : r.season;
  const share = metric === "frp" ? r.critical_share_frp : r.critical_share;
  const W = 900, H = 200, Lm = 60, R = 12, T = 14, B = 26;
  const vmax = Math.max(1, ...season.map((s) => s.q75 ?? s.median ?? 0));
  const bw = (W - Lm - R) / 12;
  const y = (v: number) => T + (1 - v / vmax) * (H - T - B);
  const crit = season.filter((s) => s.critical).map((s) => ML[s.month - 1]);
  return (
    <div className="card">
      <div className="card-head"><h3>{t("Kritik dönem: tipik yangın mevsimi", "Critical period: typical fire season")}{metric === "frp" ? " · FRP" : ""}</h3><span className="muted small">{t("aylık medyan · çizgi: çeyrekler arası aralık", "monthly median · line: interquartile range")}</span></div>
      {crit.length > 0 && (
        <p className="keymsg">{metric === "frp"
          ? t(`Bu bölgede yıllık yangın gücünün (FRP) %${Math.round(share * 100)}'i şu aylarda yoğunlaşır: `, `In this area ${Math.round(share * 100)}% of yearly fire power (FRP) is concentrated in: `)
          : t(`Bu bölgede yıllık yanma etkinliğinin %${Math.round(share * 100)}'i şu aylarda yoğunlaşır: `, `In this area ${Math.round(share * 100)}% of yearly burning activity is concentrated in: `)}
          <b>{crit.join(", ")}</b>. {t("İzleme ve hazırlık bu dönemde artırılmalıdır.", "Monitoring and preparedness should be stepped up in this period.")}</p>
      )}
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" className="harmony-svg" role="img" aria-label={t("Aylara göre tipik yanma etkinliği", "Typical burning activity by month")}>
        {[0, vmax / 2, vmax].map((v) => (
          <g key={v}><line x1={Lm} x2={W - R} y1={y(v)} y2={y(v)} stroke="var(--grid)" /><text x={Lm - 6} y={y(v) + 3} textAnchor="end" className="axis">{fmtNum(v, 0)}</text></g>
        ))}
        {season.map((s, i) => (
          <g key={s.month}>
            <rect x={Lm + i * bw + bw * 0.2} y={y(s.median ?? 0)} width={bw * 0.6} height={Math.max(0.5, y(0) - y(s.median ?? 0))}
              fill={s.critical ? "#d94801" : "#9aa4ae"}><title>{t(`${ML[i]}: tipik ${fmtNum(s.median, 0)}${u} · yıllık payı %${Math.round(s.share * 100)}`, `${ML[i]}: typical ${fmtNum(s.median, 0)}${u} · share of year ${Math.round(s.share * 100)}%`)}</title></rect>
            {s.q25 !== null && s.q75 !== null && <line x1={Lm + i * bw + bw / 2} x2={Lm + i * bw + bw / 2} y1={y(s.q25)} y2={y(s.q75)} stroke="#222" strokeWidth={1.2} />}
            <text x={Lm + i * bw + bw / 2} y={H - 8} textAnchor="middle" className="axis" fontWeight={s.critical ? "bold" : undefined}>{MS[i]}</text>
          </g>
        ))}
      </svg>
      <div className="harmony-foot">
        <span className="legend-item"><span className="swatch" style={{ background: "#d94801" }} />{t("kritik ay", "critical month")}</span>
        <span className="legend-item"><span className="swatch" style={{ background: "#9aa4ae" }} />{t("diğer aylar", "other months")}</span>
        <span className="legend-item muted">{t("birleşik kayıt, tamamlanmış aylar", "combined record, completed months")}{u && " · MW"}</span>
      </div>
    </div>
  );
}

function ContinuousCard({ r, metric }: { r: ClimResult; metric: Metric }) {
  const [log, setLog] = useState(true);
  const sfx = SFX[metric];
  const rows = r.annual.filter((a) => !a.partial);
  const get = (a: (typeof rows)[number], k: string) => (a[k + sfx] as number | null) ?? null;
  const W = 900, H = 230, Lm = 60, R = 14, T = 14, B = 26;
  const n = rows.length;
  const vmax = Math.max(1, ...rows.flatMap((a) => [get(a, "aqua") ?? 0, get(a, "harm") ?? 0, get(a, "viirs") ?? 0]));
  const tf = (v: number) => (log ? Math.log10(1 + v) : v);
  const x = (i: number) => Lm + (n <= 1 ? 0 : (i / (n - 1)) * (W - Lm - R));
  const y = (v: number) => T + (1 - tf(v) / tf(vmax)) * (H - T - B);
  const ticks = log ? logTicks(vmax) : [0, vmax / 2, vmax];
  const vi = rows.findIndex((a) => a.year >= r.viirs_start_year);
  const di = rows.findIndex((a) => r.calibration?.drifted_years?.includes(a.year));
  return (
    <div className="card">
      <div className="card-head">
        <h3>{t("Kesintisiz kayıt: yıllık yanma etkinliği", "Continuous record: yearly burning activity")}{metric === "frp" ? " · FRP (MW)" : ""}</h3>
        <label className="check small" style={{ marginLeft: "auto" }}><input type="checkbox" checked={log} onChange={(e) => setLog(e.target.checked)} /> {t("logaritmik ölçek", "log scale")}</label>
      </div>
      <p className="hint">
        {t("Ham VIIRS (kesikli) devreye girince sayılar sıçrar; bu yangın artışı değil sensör farkıdır. Harmonize VIIRS (yeşil) MODIS birimindedir ve gerçek MODIS (turuncu) ile aynı seviyede ilerler. MODIS uyduları emekliye ayrıldığında kayıt harmonize VIIRS ile sürer.",
          "When raw VIIRS (dashed) starts, the numbers jump; this is a sensor difference, not more fire. Harmonized VIIRS (green) is in MODIS units and runs at the same level as real MODIS (orange). When the MODIS satellites retire, the record continues with harmonized VIIRS.")}
        {metric === "count" && r.ratio_viirs_to_aqua_median !== null && <> {t(`Örtüşen aylarda ham VIIRS gerçek MODIS'in medyan ${fmtNum(r.ratio_viirs_to_aqua_median, 1)} katı (${r.n_overlap_months} ay).`,
          `In overlapping months raw VIIRS is a median ${fmtNum(r.ratio_viirs_to_aqua_median, 1)}× real MODIS (${r.n_overlap_months} months).`)}</>}
      </p>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" className="harmony-svg" role="img" aria-label={t("Yıllara göre gerçek MODIS, ham VIIRS ve harmonize VIIRS", "Real MODIS, raw VIIRS and harmonized VIIRS by year")}>
        {vi > 0 && <><line x1={x(vi)} x2={x(vi)} y1={T} y2={H - B} stroke="#888" strokeDasharray="4 3" /><text x={x(vi) + 4} y={T + 10} className="axis">{t("VIIRS başlangıcı", "VIIRS start")}</text></>}
        {di >= 0 && <><rect x={x(di) - 6} y={T} width={Math.max(12, x(n - 1) - x(di) + 12)} height={H - T - B} fill="#999" opacity={0.12} />
          <text x={x(di) - 2} y={H - B - 6} className="axis" textAnchor="end">{t("Aqua saat kayması →", "Aqua time drift →")}</text></>}
        {ticks.map((v) => <g key={v}><line x1={Lm} x2={W - R} y1={y(v)} y2={y(v)} stroke="var(--grid)" /><text x={Lm - 6} y={y(v) + 3} textAnchor="end" className="axis">{fmtNum(v, 0)}</text></g>)}
        {rows.map((a, i) => (i % 2 === 0 || i === n - 1) && <text key={a.year} x={x(i)} y={H - 8} textAnchor="middle" className="axis">{a.year}</text>)}
        <path d={linePath(rows.map((a) => get(a, "viirs")), x, y)} fill="none" stroke={COLORS.VIIRS} strokeWidth={1.6} strokeDasharray="5 4" />
        <path d={linePath(rows.map((a) => get(a, "harm")), x, y)} fill="none" stroke={COLORS.both} strokeWidth={2.4} />
        <path d={linePath(rows.map((a) => get(a, "aqua")), x, y)} fill="none" stroke={COLORS.MODIS} strokeWidth={2.2} />
        {rows.map((a, i) => get(a, "aqua") !== null && (
          <circle key={a.year} cx={x(i)} cy={y(get(a, "aqua")!)} r={2.6} fill={COLORS.MODIS} stroke="#fff">
            <title>{`${a.year}: Aqua ${fmtNum(get(a, "aqua"), 0)} · ${t("harmonize", "harmonized")} ${fmtNum(get(a, "harm"), 0)} · ${t("ham VIIRS", "raw VIIRS")} ${fmtNum(get(a, "viirs"), 0)}`}</title>
          </circle>
        ))}
      </svg>
      <div className="harmony-foot">
        <span className="legend-item"><span className="line-sw" style={{ background: COLORS.MODIS }} />{t("Gerçek Aqua-MODIS", "Real Aqua-MODIS")} ({r.first_year}→)</span>
        <span className="legend-item"><span className="line-sw" style={{ background: COLORS.both }} />{t("Harmonize VIIRS", "Harmonized VIIRS")} ({r.viirs_start_year}→)</span>
        <span className="legend-item"><span className="line-sw dashed" style={{ borderColor: COLORS.VIIRS }} />{t("Ham VIIRS (S-NPP)", "Raw VIIRS (S-NPP)")}</span>
        <span className="legend-item muted">{t("tamamlanmış yıllar", "completed years")}</span>
      </div>
    </div>
  );
}

function CellAnomaly({ r, k, metric, sel }: { r: ClimResult; k: Key; metric: Metric; sel: { year: number; month: number } }) {
  const ML = monthsLong();
  const mName = ML[sel.month - 1];
  const f = k + SFX[metric];
  const u = unitLabel(metric);
  const rows = r.months.filter((m) => m.month === sel.month && !m.partial && val(m, f) !== null).sort((a, b) => a.year - b.year);
  const target = r.months.find((m) => m.year === sel.year && m.month === sel.month);
  const tv = val(target, f);
  const base = rows.filter((m) => m.year !== sel.year).map((m) => val(m, f) as number);
  const st = tv !== null ? robust(base, tv, r.anomaly_floor?.[metric] ?? 5) : null;
  const cal = metric === "frp" ? r.calibration_frp : r.calibration;
  const pb = st && tv !== null && isHarmonized(target, k) ? probBeyond(tv, st.med + 2 * st.spread, st.med - 2 * st.spread, cal) : null;
  const label = series().find((s) => s.key === k)!.label;
  const W = 900, H = 220, Lm = 60, R = 14, T = 14, B = 26;
  const thr = st ? st.med + 2 * st.spread : 0;
  const vmax = Math.max(1, thr * 1.15, ...rows.map((m) => val(m, f) as number));
  const log = vmax > 6 * Math.max(thr, 1);
  const tf = (v: number) => (log ? Math.log10(1 + Math.max(0, v)) : v);
  const y = (v: number) => T + (1 - tf(v) / tf(vmax)) * (H - T - B);
  const bw = (W - Lm - R) / Math.max(rows.length, 1);
  const ticks = log ? logTicks(vmax) : [0, vmax / 2, vmax];
  const v = verdict(st?.z ?? null);
  return (
    <div className="card">
      <div className="card-head"><h3>{t("Anomali", "Anomaly")}: {mName} {sel.year}{metric === "frp" ? " · FRP" : ""}</h3><span className="muted small">{t("takvimden başka bir hücre seçebilirsiniz · seri", "pick another cell in the calendar · series")}: {label}</span></div>
      {st && tv !== null ? (
        <p className={"verdict " + v.cls}>
          <b>{mName} {sel.year}: {v.text}{pb ? ` (${probText(st.z >= 0 ? pb.pHi : pb.pLo)})` : ""}.</b>{" "}
          {t(`Değer ${fmtNum(tv, 0)}${u}; diğer ${base.length} yılın ${mName.toLocaleLowerCase("tr-TR")} aylarında tipik değer (medyan) ${fmtNum(st.med, 0)}${u}, bu yıl ${changeText(tv, st.med)}.`,
            `Value ${fmtNum(tv, 0)}${u}; the typical (median) ${mName} value over the other ${base.length} years is ${fmtNum(st.med, 0)}${u}, this year ${changeText(tv, st.med)}.`)}
          {Math.abs(st.z) >= 1 && <> {t(`Normal oynama ±${fmtNum(st.spread, 0)}${u}; bu yıl bunun ${fmtNum(Math.abs(st.z), 1)} katı ${st.z > 0 ? "üstünde" : "altında"}.`,
            `The normal year-to-year spread is ±${fmtNum(st.spread, 0)}${u}; this year lies ${fmtNum(Math.abs(st.z), 1)} spreads ${st.z > 0 ? "above" : "below"} typical.`)}</>}
          {pb && <> {t("Bu ay harmonize bir tahmin olduğu için hüküm, tahminin kalibre belirsizliği hesaba katılarak olasılık olarak verilmiştir.", "Because this month is a harmonized estimate, the verdict is given as a probability that accounts for the estimate's calibrated uncertainty.")}</>}
        </p>
      ) : <p className="verdict">{t("Bu hücre için yeterli veri yok.", "Not enough data for this cell.")}</p>}
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" className="harmony-svg" role="img" aria-label={t("Seçilen ayın yıllara göre değerleri, tipik değer ve olağandışı eşiği", "Values of the selected month by year, typical value and unusual threshold")}>
        {ticks.map((tk) => <g key={tk}><line x1={Lm} x2={W - R} y1={y(tk)} y2={y(tk)} stroke="var(--grid)" /><text x={Lm - 6} y={y(tk) + 3} textAnchor="end" className="axis">{fmtNum(tk, 0)}</text></g>)}
        {rows.map((m, i) => {
          const value = val(m, f) as number;
          const isT = m.year === sel.year;
          const unusual = st ? value >= thr : false;
          return (
            <g key={m.year}>
              <rect x={Lm + i * bw + bw * 0.18} y={y(value)} width={bw * 0.64} height={Math.max(0.5, y(0) - y(value))}
                fill={unusual ? "#c0392b" : isT ? "#1c3557" : "#9aa4ae"} stroke={isT ? "#000" : "none"} strokeWidth={isT ? 1.5 : 0}
                opacity={isHarmonized(m, k) ? 0.8 : 1}>
                <title>{`${m.year}: ${fmtNum(value, 0)}${u}${isHarmonized(m, k) ? ` (${t("harmonize", "harmonized")})` : ""}`}</title>
              </rect>
              {(i % 2 === 0 || isT) && <text x={Lm + i * bw + bw / 2} y={H - 8} textAnchor="middle" className="axis" fontWeight={isT ? "bold" : undefined}>{m.year}</text>}
            </g>
          );
        })}
        {st && <line x1={Lm} x2={W - R} y1={y(st.med)} y2={y(st.med)} stroke="#1c3557" strokeWidth={1.8} />}
        {st && thr < vmax && <line x1={Lm} x2={W - R} y1={y(thr)} y2={y(thr)} stroke="#c0392b" strokeDasharray="6 4" strokeWidth={1.5} />}
      </svg>
      <div className="harmony-foot">
        {st && <span className="legend-item"><span className="line-sw" style={{ background: "#1c3557" }} />{t("tipik değer (medyan)", "typical value (median)")}: <b>{fmtNum(st.med, 0)}{u}</b></span>}
        {st && <span className="legend-item"><span className="line-sw dashed" style={{ borderColor: "#c0392b" }} />{t("olağandışı eşiği", "unusual threshold")}: <b>{fmtNum(thr, 0)}{u}</b></span>}
        <span className="legend-item"><span className="swatch" style={{ background: "#c0392b" }} />{t("olağandışı yıl", "unusual year")}</span>
        <span className="legend-item"><span className="swatch" style={{ background: "#1c3557", outline: "1.5px solid #000" }} />{t("seçilen yıl", "selected year")}</span>
        {log && <span className="legend-item muted">{t("logaritmik ölçek", "log scale")}</span>}
      </div>
    </div>
  );
}

function AgreementCard({ r, metric, month, year }: { r: ClimResult; metric: Metric; month: number; year: number }) {
  const ML = monthsLong();
  const sfx = SFX[metric];
  const rows = r.months.filter((m) => m.month === month && !m.partial).sort((a, b) => a.year - b.year);
  const W = 900, H = 220, Lm = 60, R = 14, T = 14, B = 26;
  const n = rows.length;
  const g = (m: ClimMonth, k: string) => val(m, k + sfx);
  const vmax = Math.max(1, ...rows.flatMap((m) => [g(m, "aqua") ?? 0, g(m, "viirs") ?? 0, val(m, `harm${sfx}_hi`) ?? 0]));
  const tf = (v: number) => Math.log10(1 + v);
  const x = (i: number) => Lm + (n <= 1 ? 0 : (i / (n - 1)) * (W - Lm - R));
  const y = (v: number) => T + (1 - tf(v) / tf(vmax)) * (H - T - B);
  const ti = rows.findIndex((m) => m.year === year);
  return (
    <div className="card">
      <div className="card-head"><h3>{t(`Sensör uyumu: ${ML[month - 1]} ayları`, `Sensor agreement: every ${ML[month - 1]}`)}{metric === "frp" ? " · FRP" : ""}</h3><span className="muted small">{t("logaritmik ölçek", "log scale")}</span></div>
      <p className="hint">{t("Bu grafik anomaliyi değil, uyumlaştırmanın işe yarayıp yaramadığını gösterir: yeşil (harmonize VIIRS) çizginin turuncuya (gerçek MODIS) oturması beklenir.",
        "This chart shows whether harmonization works, not anomalies: the green line (harmonized VIIRS) should sit on the orange one (real MODIS).")}</p>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" className="harmony-svg" role="img" aria-label={t("Seçilen ay için sensör uyumu", "Sensor agreement for the selected month")}>
        {ti >= 0 && <rect x={x(ti) - 10} y={T} width={20} height={H - T - B} fill="#ffc94a" opacity={0.25} />}
        {logTicks(vmax).map((v) => <g key={v}><line x1={Lm} x2={W - R} y1={y(v)} y2={y(v)} stroke="var(--grid)" /><text x={Lm - 6} y={y(v) + 3} textAnchor="end" className="axis">{fmtNum(v, 0)}</text></g>)}
        {rows.map((m, i) => (i % 2 === 0 || i === ti) && <text key={m.year} x={x(i)} y={H - 8} textAnchor="middle" className="axis" fontWeight={i === ti ? "bold" : undefined}>{m.year}</text>)}
        <path d={linePath(rows.map((m) => g(m, "viirs")), x, y)} fill="none" stroke={COLORS.VIIRS} strokeWidth={1.6} strokeDasharray="5 4" />
        {rows.map((m, i) => {
          const lo = val(m, `harm${sfx}_lo`), hi = val(m, `harm${sfx}_hi`);
          return lo !== null && hi !== null && <line key={"b" + m.year} x1={x(i)} x2={x(i)} y1={y(lo)} y2={y(hi)} stroke={COLORS.both} strokeWidth={5} opacity={0.25} />;
        })}
        <path d={linePath(rows.map((m) => g(m, "harm")), x, y)} fill="none" stroke={COLORS.both} strokeWidth={2.2} />
        <path d={linePath(rows.map((m) => g(m, "aqua")), x, y)} fill="none" stroke={COLORS.MODIS} strokeWidth={2.2} />
      </svg>
      <div className="harmony-foot">
        <span className="legend-item"><span className="line-sw" style={{ background: COLORS.MODIS }} />{t("Gerçek Aqua-MODIS", "Real Aqua-MODIS")}</span>
        <span className="legend-item"><span className="line-sw" style={{ background: COLORS.both }} />{t("Harmonize VIIRS (çubuk: %80 aralık)", "Harmonized VIIRS (bar: 80% interval)")}</span>
        <span className="legend-item"><span className="line-sw dashed" style={{ borderColor: COLORS.VIIRS }} />{t("Ham VIIRS (S-NPP)", "Raw VIIRS (S-NPP)")}</span>
        <span className="legend-item"><span className="swatch" style={{ background: "#ffc94a", opacity: 0.5 }} />{t("seçilen yıl", "selected year")}</span>
      </div>
    </div>
  );
}

function heatScale(r: ClimResult, f: string) {
  const vals = r.months.map((m) => val(m, f)).filter((v): v is number => v !== null).sort((a, b) => a - b);
  const p95 = vals.length ? vals[Math.floor(0.95 * (vals.length - 1))] : 1;
  return (v: number) => {
    if (v <= 0) return "#f6f5f1";
    const tt = Math.min(1, Math.log1p(v) / Math.log1p(Math.max(1, p95)));
    return RAMP[Math.min(RAMP.length - 1, Math.floor(tt * (RAMP.length - 0.01)))];
  };
}

const doneMonths = (r: ClimResult, f: string) =>
  r.months.filter((m) => !m.partial && val(m, f) !== null).sort((a, b) => a.ym.localeCompare(b.ym));

function typicalOf(r: ClimResult, f: string, m: ClimMonth, metric: Metric) {
  const base = r.months.filter((x) => x.month === m.month && x.year !== m.year && !x.partial && val(x, f) !== null).map((x) => val(x, f) as number);
  const v = val(m, f);
  return { base, st: v === null ? null : robust(base, v, r.anomaly_floor?.[metric] ?? 5) };
}

const sourceText = (m: ClimMonth) => (m.combined_source === "Aqua-MODIS" ? t("gerçek MODIS", "real MODIS") : t("harmonize VIIRS", "harmonized VIIRS"));

function ratioText(v: number, med: number) {
  if (med <= 0) return "—";
  const q = v / med;
  if (q >= 2) return `${fmtNum(q, q >= 10 ? 0 : 1)}×`;
  const p = Math.round((q - 1) * 100);
  return (p >= 0 ? "+" : "−") + t(`%${Math.abs(p)}`, `${Math.abs(p)}%`);
}

function MonthVsMedianCard({ r, metric, onSelect }: { r: ClimResult; metric: Metric; onSelect: (s: { year: number; month: number }) => void }) {
  const ML = monthsLong();
  const f = "combined" + SFX[metric];
  const u = unitLabel(metric);
  const done = doneMonths(r, f);
  const last = done[done.length - 1];
  if (!last) return null;
  const tv = val(last, f) as number;
  const { base, st } = typicalOf(r, f, last, metric);
  const harm = isHarmonized(last, "combined");
  const cal = metric === "frp" ? r.calibration_frp : r.calibration;
  const pb = st && harm ? probBeyond(tv, st.med + 2 * st.spread, st.med - 2 * st.spread, cal) : null;
  const lo = harm ? val(last, metric === "frp" ? "harm_frp_lo" : "harm_lo") : null;
  const hi = harm ? val(last, metric === "frp" ? "harm_frp_hi" : "harm_hi") : null;
  const n = base.length + 1;
  const rank = 1 + base.filter((b) => b > tv).length;
  const v = verdict(st?.z ?? null);
  const mName = ML[last.month - 1];
  const mLower = mName.toLocaleLowerCase("tr-TR");
  const cur = r.months.filter((m) => m.partial).sort((a, b) => a.ym.localeCompare(b.ym)).pop();
  return (
    <div className="card">
      <div className="card-head"><h3>{t("Bu ay ve tarihsel medyan", "This month vs historical median")}{metric === "frp" ? " · FRP" : ""}</h3>
        <span className="muted small">{t("son tamamlanan ay · birleşik kayıt (MODIS birimi)", "last completed month · combined record (MODIS units)")}</span></div>
      <div className="kpis mstats">
        <div className="kpi"><div className="kpi-k">{mName} {last.year}</div><div className="kpi-v">{fmtNum(tv, 0)}{u}</div>
          <div className="kpi-k">{sourceText(last)}{lo !== null && hi !== null ? t(` · %80 aralık ${fmtNum(lo, 0)}–${fmtNum(hi, 0)}`, ` · 80% interval ${fmtNum(lo, 0)}–${fmtNum(hi, 0)}`) : ""}</div></div>
        <div className="kpi"><div className="kpi-k">{t(`Tarihsel medyan (${base.length} yıl)`, `Historical median (${base.length} years)`)}</div><div className="kpi-v">{st ? fmtNum(st.med, 0) + u : "—"}</div>
          <div className="kpi-k">{t(`diğer yılların ${mLower} ayları`, `${mName} in the other years`)}</div></div>
        <div className="kpi"><div className="kpi-k">{t("Fark", "Difference")}</div><div className="kpi-v">{st ? ratioText(tv, st.med) : "—"}</div>
          <div className="kpi-k">{st ? changeText(tv, st.med) : ""}</div></div>
        <div className="kpi"><div className="kpi-k">{t("Sıra", "Rank")}</div><div className="kpi-v">{rank} / {n}</div>
          <div className="kpi-k">{t(`${n} ${mLower} ayı içinde (1 = en yüksek)`, `among ${n} ${mName}s (1 = highest)`)}</div></div>
      </div>
      <p className={"verdict " + v.cls}>
        <b>{mName} {last.year}: {v.text}.</b>{" "}
        {harm ? t("Bu ay harmonize VIIRS tahminidir.", "This month is a harmonized VIIRS estimate.") : t("Bu ay gerçek Aqua-MODIS ölçümüdür.", "This month is a real Aqua-MODIS measurement.")}
        {st && pb && <> {st.z >= 0
          ? t(`Kalibre belirsizlik hesaba katıldığında olağandışı yüksek olma olasılığı yaklaşık %${Math.round(pb.pHi * 100)}.`, `Accounting for the calibrated uncertainty, the probability of being unusually high is about ${Math.round(pb.pHi * 100)}%.`)
          : t(`Kalibre belirsizlik hesaba katıldığında olağandışı düşük olma olasılığı yaklaşık %${Math.round(pb.pLo * 100)}.`, `Accounting for the calibrated uncertainty, the probability of being unusually low is about ${Math.round(pb.pLo * 100)}%.`)}</>}{" "}
        <button className="link" onClick={() => onSelect({ year: last.year, month: last.month })}>{t("takvimde ve haritada göster", "show in calendar and map")}</button>
      </p>
      {cur && <p className="hint">{t(`${ML[cur.month - 1]} ${cur.year} henüz bitmediği için karşılaştırmaya alınmadı. Son günlerin durumu için "Yılın bu zamanı" kartına bakın.`,
        `${ML[cur.month - 1]} ${cur.year} is not finished yet, so it is not compared. For the latest days, see the "This time of year" card.`)}</p>}
    </div>
  );
}

function TopCard({ r, metric, onSelect }: { r: ClimResult; metric: Metric; onSelect: (s: { year: number; month: number }) => void }) {
  const ML = monthsLong();
  const f = "combined" + SFX[metric];
  const u = unitLabel(metric);
  const done = doneMonths(r, f);
  const byVal = (a: ClimMonth, b: ClimMonth) => (val(b, f) as number) - (val(a, f) as number);
  const allYears = r.annual.filter((a) => !a.partial && typeof a[f] === "number").map((a) => ({ year: a.year, v: a[f] as number }));
  const medYear = allYears.length ? median(allYears.map((a) => a.v)) : 0;
  const topYears = [...allYears].sort((a, b) => b.v - a.v).slice(0, 5);
  const topMonths = [...done].sort(byVal).slice(0, 5);
  const yearSource = (y: number) => {
    const ms = done.filter((m) => m.year === y);
    const a = ms.filter((m) => m.combined_source === "Aqua-MODIS").length;
    return a === ms.length ? t("gerçek MODIS", "real MODIS") : a === 0 ? t("harmonize VIIRS", "harmonized VIIRS") : t("karışık", "mixed");
  };
  const peak = (y: number) => done.filter((m) => m.year === y).sort(byVal)[0];
  return (
    <div className="card">
      <div className="card-head"><h3>{t("En yoğun yıllar ve aylar", "Highest-activity years and months")}{metric === "frp" ? " · FRP" : ""}</h3>
        <span className="muted small">{t("birleşik kayıt (MODIS birimi) · tamamlanmış yıllar ve aylar · tıklayınca takvimde seçilir", "combined record (MODIS units) · completed years and months · click to select in the calendar")}</span></div>
      <div className="top-grid">
        <table className="tbl top-tbl">
          <thead><tr><th>#</th><th>{t("Yıl", "Year")}</th><th>{t("Yıllık toplam", "Yearly total")}</th><th>{t("Medyan yıla göre", "vs median year")}</th><th>{t("En yoğun ay", "Peak month")}</th><th>{t("Kaynak", "Source")}</th></tr></thead>
          <tbody>
            {topYears.map((a, i) => {
              const pk = peak(a.year);
              return (
                <tr key={a.year}><td>{i + 1}</td>
                  <td><button className="link" onClick={() => pk && onSelect({ year: a.year, month: pk.month })}>{a.year}</button></td>
                  <td className="num">{fmtNum(a.v, 0)}{u}</td><td className="num">{ratioText(a.v, medYear)}</td>
                  <td>{pk ? ML[pk.month - 1] : "—"}</td><td>{yearSource(a.year)}</td></tr>
              );
            })}
          </tbody>
        </table>
        <table className="tbl top-tbl">
          <thead><tr><th>#</th><th>{t("Ay", "Month")}</th><th>{t("Değer", "Value")}</th><th>{t("Aynı ayın medyanına göre", "vs median of that month")}</th><th>{t("Kaynak", "Source")}</th></tr></thead>
          <tbody>
            {topMonths.map((m, i) => {
              const { st } = typicalOf(r, f, m, metric);
              return (
                <tr key={m.ym}><td>{i + 1}</td>
                  <td><button className="link" onClick={() => onSelect({ year: m.year, month: m.month })}>{ML[m.month - 1]} {m.year}</button></td>
                  <td className="num">{fmtNum(val(m, f), 0)}{u}</td><td className="num">{st ? ratioText(val(m, f) as number, st.med) : "—"}</td><td>{sourceText(m)}</td></tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="hint">{t(`Medyan yıl: ${fmtNum(medYear, 0)}${u} (${allYears.length} tamamlanmış yıl). Bitmemiş yıl ve aylar sıralamaya alınmaz.`,
        `Median year: ${fmtNum(medYear, 0)}${u} (${allYears.length} completed years). Unfinished years and months are not ranked.`)}</p>
    </div>
  );
}

function downloadBlob(name: string, blob: Blob) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

function calendarCsv(r: ClimResult): string {
  const cols = ["year", "month", "month_complete", "combined", "combined_source", "aqua_modis", "harmonized_viirs", "harmonized_lo80", "harmonized_hi80",
    "raw_viirs_snpp", "typical_median", "unusual", "p_unusual_high", "p_unusual_low", "combined_frp_mw", "aqua_frp_mw", "harmonized_frp_mw",
    "harmonized_frp_lo80_mw", "harmonized_frp_hi80_mw", "raw_viirs_frp_mw", "products"];
  const num = (v: number | null | undefined, d = 1) => (v === null || v === undefined ? "" : String(Math.round(v * 10 ** d) / 10 ** d));
  const rows = [...r.months].sort((a, b) => a.ym.localeCompare(b.ym)).map((m) => {
    const c = val(m, "combined");
    const st = c !== null && !m.partial ? typicalOf(r, "combined", m, "count").st : null;
    const a = anomalyP(m, "combined", "count");
    const unusual = m.partial || c === null ? "" : a.hi >= 0.9 ? "high" : a.hi >= 0.5 ? "possibly_high" : a.lo >= 0.9 ? "low" : a.lo >= 0.5 ? "possibly_low" : "normal";
    const src = m.combined_source === "Aqua-MODIS" ? "modis" : m.combined_source ? "harmonized" : "";
    return [m.year, m.month, m.partial ? 0 : 1, num(c), src, num(m.aqua), num(m.harm), num(m.harm_lo), num(m.harm_hi), num(m.viirs), num(st?.med),
      unusual, a.harm && !m.partial ? num(a.hi, 3) : "", a.harm && !m.partial ? num(a.lo, 3) : "", num(m.combined_frp, 0), num(m.aqua_frp, 0),
      num(m.harm_frp, 0), num(m.harm_frp_lo, 0), num(m.harm_frp_hi, 0), num(m.viirs_frp, 0), `"${(m.products ?? []).join(";")}"`].join(",");
  });
  return [cols.join(","), ...rows].join("\r\n") + "\r\n";
}

function downloadCalendarCsv(r: ClimResult) {
  downloadBlob(`burning_calendar_${r.bbox.join("_")}.csv`, new Blob(["﻿" + calendarCsv(r)], { type: "text/csv;charset=utf-8" }));
}

const PNG_LAYOUT = { S: 2, PAD: 24, YW: 78, CW: 64, CH: 20, G: 2, TW: 80, TOP: 74, HEAD: 20 };

function downloadCalendarPng(r: ClimResult, k: Key, metric: Metric) {
  const { S, PAD, YW, CW, CH, G, TW, TOP, HEAD } = PNG_LAYOUT;
  const MS = monthsShort();
  const f = k + SFX[metric];
  const color = heatScale(r, f);
  const years = Array.from(new Set(r.months.map((m) => m.year))).sort((a, b) => a - b);
  const cell = new Map(r.months.map((m) => [`${m.year}-${m.month}`, m]));
  const season = metric === "frp" ? r.season_frp : r.season;
  const crit = new Set(metric === "frp" ? r.critical_months_frp : r.critical_months);
  const drifted = new Set(r.calibration?.drifted_years ?? []);
  const W = PAD * 2 + YW + 12 * (CW + G) + TW;
  const H = TOP + HEAD + years.length * (CH + G) + 6 + 30 + 24 + 30 + PAD;
  const cv = document.createElement("canvas");
  cv.width = W * S;
  cv.height = H * S;
  const c = cv.getContext("2d")!;
  c.scale(S, S);
  const font = (w: string, px: number) => `${w} ${px}px "Segoe UI", Arial, sans-serif`;
  c.fillStyle = "#ffffff";
  c.fillRect(0, 0, W, H);
  c.fillStyle = "#1c3557";
  c.font = font("600", 18);
  c.textAlign = "left";
  c.fillText(t("Yanma takvimi (ay × yıl)", "Burning calendar (month × year)") + (metric === "frp" ? " · FRP (MW)" : ""), PAD, PAD + 14);
  c.fillStyle = "#555";
  c.font = font("400", 12);
  c.fillText(`${series().find((s) => s.key === k)!.label} · ${t("Bölge", "Area")}: ${r.bbox.join(", ")} · ${years[0]}–${years[years.length - 1]}`, PAD, PAD + 34);
  const x0 = PAD + YW;
  let y = TOP;
  MS.forEach((m, i) => {
    const cr = crit.has(i + 1);
    c.fillStyle = cr ? "#b3261e" : "#777";
    c.font = font(cr ? "700" : "400", 11);
    c.textAlign = "center";
    c.fillText(m + (cr ? "★" : ""), x0 + i * (CW + G) + CW / 2, y + 12);
  });
  c.fillStyle = "#777";
  c.font = font("400", 11);
  c.textAlign = "right";
  c.fillText(t("yıllık", "year"), W - PAD, y + 12);
  y += HEAD;
  const hatch = (x: number, yy: number, w: number, h: number) => {
    c.fillStyle = "#f6f5f1";
    c.fillRect(x, yy, w, h);
    c.save();
    c.beginPath();
    c.rect(x, yy, w, h);
    c.clip();
    c.strokeStyle = "#dddbd3";
    c.lineWidth = 2.2;
    for (let d = -h; d < w; d += 6) { c.beginPath(); c.moveTo(x + d, yy + h); c.lineTo(x + d + h, yy); c.stroke(); }
    c.restore();
  };
  for (const yr of years) {
    c.textAlign = "left";
    c.fillStyle = "#333";
    c.font = font("400", 11);
    c.fillText(`${yr}${yr === r.viirs_start_year ? " ▸" : ""}${drifted.has(yr) ? " ⌁" : ""}`, PAD, y + CH - 6);
    for (let i = 0; i < 12; i++) {
      const m = cell.get(`${yr}-${i + 1}`);
      const v = val(m, f);
      const x = x0 + i * (CW + G);
      if (v === null) hatch(x, y, CW, CH);
      else { c.fillStyle = color(v); c.fillRect(x, y, CW, CH); }
      const a = anomalyP(m, k, metric);
      const p = Math.max(a.hi, a.lo);
      if (a.hi >= 0.5 || a.lo >= 0.5) {
        c.strokeStyle = a.hi >= 0.5 ? "#b3261e" : "#1a4f9c";
        c.lineWidth = 2;
        c.setLineDash(a.harm && p < 0.9 ? [4, 3] : []);
        c.strokeRect(x + 1, y + 1, CW - 2, CH - 2);
      } else {
        c.strokeStyle = m?.partial ? "#666" : "rgba(0,0,0,.08)";
        c.lineWidth = 1;
        c.setLineDash(m?.partial ? [3, 2] : []);
        c.strokeRect(x + 0.5, y + 0.5, CW - 1, CH - 1);
      }
      c.setLineDash([]);
      const mark = a.hi >= 0.9 ? "!" : a.hi >= 0.5 ? "?" : "";
      if (mark) { c.fillStyle = "#fff"; c.font = font("700", 11); c.textAlign = "center"; c.fillText(mark, x + CW / 2, y + CH - 6); }
    }
    const ann = r.annual.find((a) => a.year === yr);
    const annV = ann && !ann.partial ? (ann[f] as number | null) : null;
    if (annV !== null && annV !== undefined) { c.fillStyle = "#777"; c.font = font("400", 11); c.textAlign = "right"; c.fillText(fmtNum(annV, 0), W - PAD, y + CH - 6); }
    y += CH + G;
  }
  y += 6;
  c.strokeStyle = "#dddddd";
  c.lineWidth = 1;
  c.beginPath(); c.moveTo(PAD, y - 3); c.lineTo(W - PAD, y - 3); c.stroke();
  c.textAlign = "left";
  c.fillStyle = "#333";
  c.font = font("700", 11);
  c.fillText(t("Tipik", "Typical"), PAD, y + 12);
  season.forEach((s) => {
    const x = x0 + (s.month - 1) * (CW + G);
    if (s.median !== null) { c.fillStyle = color(s.median); c.fillRect(x, y, CW, 16); }
    if (s.critical) { c.strokeStyle = "#b3261e"; c.lineWidth = 2; c.strokeRect(x + 1, y + 1, CW - 2, 14); }
  });
  y += 30;
  let lx = PAD;
  const txt = (s: string) => { c.fillStyle = "#555"; c.textAlign = "left"; c.font = font("400", 11); c.fillText(s, lx, y + 11); lx += c.measureText(s).width + 12; };
  txt(t("az", "low"));
  RAMP.forEach((col) => { c.fillStyle = col; c.fillRect(lx, y, 14, 14); c.strokeStyle = "rgba(0,0,0,.15)"; c.lineWidth = 1; c.strokeRect(lx + 0.5, y + 0.5, 13, 13); lx += 16; });
  lx += 4;
  txt(t("çok (log ölçek)", "high (log scale)"));
  const sw = (fill: string, border: string, dash: boolean, mark: string, label: string) => {
    c.fillStyle = fill; c.fillRect(lx, y, 14, 14);
    c.strokeStyle = border; c.lineWidth = 2; c.setLineDash(dash ? [3, 2] : []); c.strokeRect(lx + 1, y + 1, 12, 12); c.setLineDash([]);
    if (mark) { c.fillStyle = "#fff"; c.font = font("700", 10); c.textAlign = "center"; c.fillText(mark, lx + 7, y + 11); }
    lx += 20;
    txt(label);
  };
  sw("#d94801", "#b3261e", false, "!", t("olağandışı yüksek", "unusually high"));
  sw("#d94801", "#b3261e", true, "?", t("muhtemelen olağandışı (%50–90)", "possibly unusual (50–90%)"));
  sw("#fdd0a2", "#1a4f9c", false, "", t("olağandışı düşük", "unusually low"));
  hatch(lx, y, 14, 14);
  lx += 20;
  txt(t("veri yetersiz", "insufficient data"));
  y += 24;
  lx = PAD;
  txt(t("★ kritik ay · ▸ VIIRS başlangıcı · ⌁ Aqua saat kayması", "★ critical month · ▸ VIIRS start · ⌁ Aqua time drift"));
  y += 26;
  c.fillStyle = "#888";
  c.font = font("400", 10.5);
  c.textAlign = "left";
  c.fillText(`FireA Harmonizer · NASA FIRMS (MODIS C6.1, VIIRS 375 m) · ${t("oluşturma tarihi", "generated")} ${new Date().toISOString().slice(0, 10)}`, PAD, y + 8);
  cv.toBlob((b) => { if (b) downloadBlob(`burning_calendar_${k}${metric === "frp" ? "_frp" : ""}_${r.bbox.join("_")}.png`, b); }, "image/png");
}
