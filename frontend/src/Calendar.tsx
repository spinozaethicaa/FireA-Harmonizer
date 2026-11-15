import { useMemo } from "react";
import type { CalendarDay, DayClass, VMDaily } from "./types";
import { COLORS, COVERAGE_LABEL, DAY_CLASS_LABEL, DISPLAY, fmtDate, monthLabel } from "./util";
import { t } from "./i18n";

interface Props {
  days: CalendarDay[];
  selectedDay: string | null;
  highlightDays: Set<string>;
  onSelectDay: (d: string | null) => void;
  view?: "real" | "modis";
  vmDaily?: VMDaily[];
}

const CODE: Record<DayClass, string> = { both: "M+V", modis_only: "M", viirs_only: "V", none: "", unavailable: "×" };

const PAT_MODIS = "repeating-linear-gradient(0deg, rgba(0,0,0,.30) 0 1.5px, transparent 1.5px 4px)";
const PAT_VIIRS = "radial-gradient(rgba(255,255,255,.75) 1.1px, transparent 1.3px) 0 0 / 4px 4px";

function bg(c: DayClass): React.CSSProperties {
  switch (c) {
    case "both": return { background: COLORS.both, color: COLORS.onBoth };
    case "modis_only": return { background: DISPLAY.patterns ? `${PAT_MODIS}, ${COLORS.MODIS}` : COLORS.MODIS, color: COLORS.onMODIS };
    case "viirs_only": return { background: DISPLAY.patterns ? `${PAT_VIIRS}, ${COLORS.VIIRS}` : COLORS.VIIRS, color: COLORS.onVIIRS };
    case "none": return { background: COLORS.none, color: "#6b6a66" };
    case "unavailable": return { background: "repeating-linear-gradient(45deg,#cfccc4 0 3px,#f3f2ee 3px 6px)", color: "#4a4945" };
  }
}

function tip(d: CalendarDay): string {
  const s = (k: "MODIS" | "VIIRS") => {
    const x = d.sensors[k];
    return `${k}: ${x.n_detections} ${t("tespit", "detections")}${x.n_excluded_quality ? ` (+${x.n_excluded_quality} ${t("filtrelendi", "filtered")})` : ""} · ${COVERAGE_LABEL[x.coverage] ?? x.coverage}${x.product ? ` · ${x.product}` : ""}`;
  };
  return [
    `${fmtDate(d.date)} (UTC)`,
    DAY_CLASS_LABEL[d.class] + (d.partial_coverage ? t(" — bir sensörün verisi yok", " — one sensor has no data") : ""),
    s("MODIS"),
    s("VIIRS"),
    t(`Aday olay: ${d.n_events} · algoritma eşleşmesi olan olay: ${d.n_events_with_cross_match}`, `Candidate events: ${d.n_events} · events with an algorithm match: ${d.n_events_with_cross_match}`),
  ].join("\n");
}

const RAMP = ["#fde7dc", "#f8b998", "#f08a5b", "#d9622e", "#a8441a"];

function vmBg(v: number, max: number, covered: boolean): React.CSSProperties {
  if (!covered) return bg("unavailable");
  if (v < 0.5) return { background: COLORS.none, color: "#6b6a66" };
  const k = Math.min(RAMP.length - 1, Math.floor((Math.log1p(v) / Math.log1p(max)) * RAMP.length));
  return { background: RAMP[k], color: k >= 3 ? "#fff" : "#3d2a20" };
}

export default function Calendar({ days, selectedDay, highlightDays, onSelectDay, view = "real", vmDaily }: Props) {
  const vmMap = useMemo(() => new Map((vmDaily ?? []).map((d) => [d.date, d])), [vmDaily]);
  const vmMax = useMemo(() => Math.max(1, ...(vmDaily ?? []).map((d) => d.virtual_mean)), [vmDaily]);
  const modisView = view === "modis" && vmMap.size > 0;
  const months = useMemo(() => {
    const m = new Map<string, (CalendarDay | null)[]>();
    for (const d of days) {
      const ym = d.date.slice(0, 7);
      if (!m.has(ym)) m.set(ym, Array(31).fill(null));
      m.get(ym)![+d.date.slice(8, 10) - 1] = d;
    }
    return [...m.entries()];
  }, [days]);
  const big = days.length <= 62;
  const totals = useMemo(() => {
    const tot: Record<DayClass, number> = { both: 0, modis_only: 0, viirs_only: 0, none: 0, unavailable: 0 };
    days.forEach((d) => tot[d.class]++);
    return tot;
  }, [days]);

  if (!days.length) return null;
  return (
    <div className="card calendar">
      <div className="card-head">
        <h3>{t("Etkinlik takvimi", "Activity calendar")}</h3>
        <span className="muted small">{t("UTC günleri · bir güne tıklayıp haritayı filtreleyin", "UTC days · click a day to filter the map")}</span>
        {selectedDay && <button className="chip-btn" onClick={() => onSelectDay(null)}>{fmtDate(selectedDay)} ✕</button>}
      </div>
      <div className={"cal" + (big ? " big" : "")}>
        <div className="cal-row head">
          <span className="cal-label" />
          {Array.from({ length: 31 }, (_, i) => (
            <span key={i} className="cal-num">{big || (i + 1) % 5 === 0 || i === 0 ? i + 1 : ""}</span>
          ))}
        </div>
        {months.map(([ym, cells]) => (
          <div className="cal-row" key={ym}>
            <span className="cal-label">{monthLabel(ym)}</span>
            {cells.map((d, i) =>
              d ? (
                <button
                  key={i}
                  className={"cal-cell" + (selectedDay === d.date ? " sel" : "") + (highlightDays.has(d.date) ? " ev" : "")}
                  style={modisView ? vmBg(vmMap.get(d.date)?.virtual_mean ?? 0, vmMax, d.sensors.VIIRS.coverage !== "error" && d.sensors.VIIRS.coverage !== "out_of_range" && d.sensors.VIIRS.coverage !== "not_queried") : bg(d.class)}
                  title={modisView && vmMap.get(d.date) ? `${fmtDate(d.date)} (UTC)
${t("FireA: VIIRS", "FireA: VIIRS")} ${vmMap.get(d.date)!.viirs_pixels} ${t("piksel → harmonize", "pixels → harmonized")} ≈ ${vmMap.get(d.date)!.virtual_mean.toFixed(1)} (${t("%80", "80%")}: ${vmMap.get(d.date)!.virtual_lo}–${vmMap.get(d.date)!.virtual_hi})
${t("Gerçek Aqua-MODIS", "Real Aqua-MODIS")}: ${vmMap.get(d.date)!.aqua_observed}` : tip(d)}
                  onClick={() => onSelectDay(selectedDay === d.date ? null : d.date)}
                >
                  {big && <span className="code">{modisView ? (vmMap.get(d.date)?.virtual_mean ?? 0) >= 0.5 ? Math.round(vmMap.get(d.date)!.virtual_mean) : "" : CODE[d.class]}</span>}
                  {d.partial_coverage && <span className="partial" />}
                  {d.n_events_with_cross_match > 0 && <span className="match" />}
                </button>
              ) : (
                <span key={i} className="cal-cell blank" />
              ),
            )}
          </div>
        ))}
      </div>
      {modisView ? (
        <div className="legend">
          <span className="legend-item">{t("Harmonize günlük tespit (MODIS-eşdeğeri, FireA):", "Harmonized daily detections (MODIS equivalent, FireA):")}</span>
          {RAMP.map((c, i) => <span key={c} className="swatch" style={{ background: c }} title={`${t("kademe", "step")} ${i + 1}`} />)}
          <span className="legend-item muted">{t("az → çok (log ölçek, en yüksek ≈ ", "low → high (log scale, max ≈ ")}{Math.round(vmMax)})</span>
          <span className="legend-item"><span className="swatch" style={bg("unavailable")} />{t("VIIRS verisi yok", "no VIIRS data")}</span>
        </div>
      ) : (
      <div className="legend">
        {(Object.keys(DAY_CLASS_LABEL) as DayClass[]).map((c) => (
          <span key={c} className="legend-item">
            <span className="swatch" style={bg(c)} />
            {DAY_CLASS_LABEL[c]} <b>{totals[c]}</b>
          </span>
        ))}
        <span className="legend-item"><span className="swatch white"><span className="match" /></span>{t("Algoritma o gün MODIS–VIIRS eşleşmesi buldu", "The algorithm found a MODIS–VIIRS match that day")}</span>
        <span className="legend-item"><span className="swatch white"><span className="partial" /></span>{t("Bir sensörün verisi yok", "One sensor has no data")}</span>
      </div>
      )}
    </div>
  );
}
