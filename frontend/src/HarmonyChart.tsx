import { useMemo, useRef, useState } from "react";
import type { VMDaily } from "./types";
import { COLORS, fmtDate, fmtInt, fmtNum } from "./util";
import { t } from "./i18n";

interface Props {
  daily: VMDaily[];
  selectedDay: string | null;
  onSelectDay: (d: string | null) => void;
}

export default function HarmonyChart({ daily, selectedDay, onSelectDay }: Props) {
  const [hover, setHover] = useState<number | null>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const W = 900, H = 200, L = 44, R = 12, T = 12, B = 26;
  const n = daily.length;
  const ymax = useMemo(() => {
    const m = Math.max(1, ...daily.map((d) => Math.max(d.aqua_observed, d.virtual_hi, d.virtual_mean)));
    const p = Math.pow(10, Math.floor(Math.log10(m)));
    return Math.ceil(m / p) * p;
  }, [daily]);
  if (!n) return null;
  const x = (i: number) => L + (n === 1 ? (W - L - R) / 2 : (i / (n - 1)) * (W - L - R));
  const y = (v: number) => T + (1 - v / ymax) * (H - T - B);
  const band =
    daily.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d.virtual_hi).toFixed(1)}`).join("") +
    daily.slice().reverse().map((d, k) => `L${x(n - 1 - k).toFixed(1)},${y(d.virtual_lo).toFixed(1)}`).join("") + "Z";
  const line = (key: "virtual_mean" | "aqua_observed") =>
    daily.map((d, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(d[key]).toFixed(1)}`).join("");
  const ticks = [0, ymax / 2, ymax];
  const xt = n <= 10 ? daily.map((_, i) => i) : [0, Math.floor(n / 4), Math.floor(n / 2), Math.floor((3 * n) / 4), n - 1];
  const sumA = daily.reduce((s, d) => s + d.aqua_observed, 0);
  const sumV = daily.reduce((s, d) => s + d.virtual_mean, 0);
  const sumVP = daily.reduce((s, d) => s + d.viirs_pixels, 0);

  const onMove = (e: React.MouseEvent) => {
    const r = svgRef.current!.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    const i = Math.round(((px - L) / (W - L - R)) * (n - 1));
    setHover(Math.max(0, Math.min(n - 1, i)));
  };
  const h = hover !== null ? daily[hover] : null;
  const sel = selectedDay ? daily.findIndex((d) => d.date === selectedDay) : -1;

  return (
    <div className="card harmony">
      <div className="card-head">
        <h3>{t("Harmonize günlük etkinlik", "Harmonized daily activity")}</h3>
        <span className="muted small">{t("Aqua-MODIS eşdeğeri tespit sayısı · UTC günleri", "Aqua-MODIS equivalent detection count · UTC days")}</span>
      </div>
      <div className="harmony-kpis">
        <div><span className="muted small">{t("VIIRS pikseli", "VIIRS pixels")}</span><b>{fmtInt(sumVP)}</b></div>
        <div><span className="muted small">{t("→ harmonize (FireA)", "→ harmonized (FireA)")}</span><b>≈ {fmtNum(sumV, 0)}</b></div>
        <div><span className="muted small">{t("Gerçek Aqua-MODIS", "Real Aqua-MODIS")}</span><b>{fmtInt(sumA)}</b></div>
      </div>
      <svg ref={svgRef} viewBox={`0 0 ${W} ${H}`} width="100%" className="harmony-svg" role="img"
        aria-label={t("Gerçek Aqua-MODIS ve FireA MODIS-eşdeğeri günlük tespit sayıları", "Real Aqua-MODIS and FireA MODIS-equivalent daily detection counts")}
        onMouseMove={onMove} onMouseLeave={() => setHover(null)}
        onClick={() => hover !== null && onSelectDay(daily[hover].date === selectedDay ? null : daily[hover].date)}>
        {ticks.map((v) => (
          <g key={v}>
            <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke="var(--grid)" />
            <text x={L - 6} y={y(v) + 3} textAnchor="end" className="axis">{fmtNum(v, 0)}</text>
          </g>
        ))}
        {xt.map((i) => (
          <text key={i} x={x(i)} y={H - 6} textAnchor={i === 0 ? "start" : i === n - 1 ? "end" : "middle"} className="axis">
            {fmtDate(daily[i].date)}
          </text>
        ))}
        {sel >= 0 && <line x1={x(sel)} x2={x(sel)} y1={T} y2={H - B} stroke="#111" strokeWidth={1.5} />}
        <path d={band} fill={COLORS.VIIRS} opacity={0.16} />
        <path d={line("virtual_mean")} fill="none" stroke={COLORS.VIIRS} strokeWidth={2} />
        <path d={line("aqua_observed")} fill="none" stroke={COLORS.MODIS} strokeWidth={2} />
        {n <= 120 && daily.map((d, i) => (
          <circle key={d.date} cx={x(i)} cy={y(d.aqua_observed)} r={2.6} fill={COLORS.MODIS} stroke="#fff" strokeWidth={1} />
        ))}
        {hover !== null && <line x1={x(hover)} x2={x(hover)} y1={T} y2={H - B} stroke="#9aa3b0" strokeDasharray="3 3" />}
      </svg>
      <div className="harmony-foot">
        <span className="legend-item"><span className="line-sw" style={{ background: COLORS.MODIS }} />{t("Gerçek Aqua-MODIS tespitleri", "Real Aqua-MODIS detections")}</span>
        <span className="legend-item"><span className="line-sw" style={{ background: COLORS.VIIRS }} />{t("Harmonize: VIIRS'ten MODIS-eşdeğeri (FireA)", "Harmonized: MODIS equivalent from VIIRS (FireA)")}</span>
        <span className="legend-item"><span className="band-sw" title={t("Yalnızca FireA'nın kendi belirsizliği. Örtüşme yıllarıyla kalibre edilmiş, daha gerçekçi aralık Tarihsel sekmesindedir.", "FireA's own uncertainty only. A more realistic interval, calibrated on the overlap years, is in the History tab.")} />{t("%80 model aralığı (yalnızca FireA belirsizliği)", "80% model interval (FireA uncertainty only)")}</span>
        <span className="muted small tip">
          {h ? t(`${fmtDate(h.date)} · VIIRS ${h.viirs_pixels} piksel → harmonize ≈ ${fmtNum(h.virtual_mean, 1)} (%80: ${h.virtual_lo}–${h.virtual_hi}) · gerçek Aqua ${h.aqua_observed}`,
            `${fmtDate(h.date)} · VIIRS ${h.viirs_pixels} pixels → harmonized ≈ ${fmtNum(h.virtual_mean, 1)} (80%: ${h.virtual_lo}–${h.virtual_hi}) · real Aqua ${h.aqua_observed}`)
            : t("Bir güne gelip ayrıntı görün; tıklayınca harita o güne süzülür.", "Hover over a day for details; click to filter the map to that day.")}
        </span>
      </div>
    </div>
  );
}
