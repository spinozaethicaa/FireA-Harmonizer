import { useEffect, useMemo, useState } from "react";
import { api, errInfo } from "./api";
import type { CrossLink, EventFull, EventLite, Obs, ObsFull, Summary, VirtualModis, VMMetrics } from "./types";
import { COLORS, fmtDate, fmtInt, fmtLocal, fmtNum, fmtUtc } from "./util";
import { t } from "./i18n";

interface Props {
  summary: Summary;
  events: EventLite[];
  dayFilter: string | null;
  selectedEventId: string | null;
  selectedObsId: string | null;
  onSelectEvent: (id: string | null) => void;
  onSelectObs: (id: string | null) => void;
  onEventMembers: (members: Obs[] | null) => void;
}

export default function DetailPanel(p: Props) {
  if (p.selectedObsId) return <ObsDetail {...p} id={p.selectedObsId} />;
  if (p.selectedEventId) return <EventDetail {...p} id={p.selectedEventId} />;
  return <Overview {...p} />;
}

function Overview({ summary, events, dayFilter, onSelectEvent }: Props) {
  type Sort = "size" | "time" | "recent" | "frp";
  const [sort, setSort] = useState<Sort>("size");
  const list = useMemo(() => {
    const cmp: Record<Sort, (a: EventLite, b: EventLite) => number> = {
      size: (a, b) => b.n_obs - a.n_obs,
      time: (a, b) => a.first_obs_utc.localeCompare(b.first_obs_utc),
      recent: (a, b) => b.last_obs_utc.localeCompare(a.last_obs_utc),
      frp: (a, b) => (b.max_single_frp_mw ?? -1) - (a.max_single_frp_mw ?? -1),
    };
    return [...events].sort(cmp[sort]);
  }, [events, sort]);
  const nowMs = Date.now();
  const ageH = (e: EventLite) => (nowMs - Date.parse(e.last_obs_utc)) / 3.6e6;
  const st = summary.stats;
  const q = summary.quality_summary;
  return (
    <div className="panel">
      <div className="stats-grid">
        <Stat label={t("Aday olay", "Candidate events")} value={st.n_events} />
        <Stat label={t("Gözlem (kullanılan)", "Observations (used)")} value={st.n_observations_used} sub={`${fmtInt(st.n_observations_total)} ${t("toplam", "total")}`} />
        <Stat label="MODIS" value={q.MODIS?.kept ?? 0} color={COLORS.MODIS} sub={q.MODIS?.excluded ? `${q.MODIS.excluded} ${t("filtrelendi", "filtered")}` : undefined} />
        <Stat label="VIIRS" value={q.VIIRS?.kept ?? 0} color={COLORS.VIIRS} sub={q.VIIRS?.excluded ? `${q.VIIRS.excluded} ${t("filtrelendi", "filtered")}` : undefined} />
      </div>
      <p className="hint">
        {t("Aday olay, konum ve zaman yakınlığına göre ilişkilendirilmiş gözlem kümesidir. Nokta sayısı yangın sayısı değildir.",
          "A candidate event is a group of observations linked by proximity in space and time. Point count is not fire count.")}
      </p>
      <div className="list-head">
        <h3>{t("Olaylar", "Events")} {dayFilter && <span className="muted">· {fmtDate(dayFilter)}</span>} <span className="muted">({list.length})</span></h3>
        <select value={sort} onChange={(e) => setSort(e.target.value as Sort)}>
          <option value="size">{t("En büyük", "Largest")}</option>
          <option value="recent">{t("En yeni tespit", "Most recent detection")}</option>
          <option value="frp">{t("En yüksek FRP", "Highest FRP")}</option>
          <option value="time">{t("Kronolojik", "Chronological")}</option>
        </select>
      </div>
      {list.length === 0 && <div className="empty">{t("Bu seçimde aday olay yok.", "No candidate events in this selection.")}</div>}
      <ul className="event-list">
        {list.slice(0, 200).map((e) => (
          <li key={e.event_id}>
            <button onClick={() => onSelectEvent(e.event_id)}>
              <div className="ev-top">
                <span className="mono">{e.event_id}</span>
                {ageH(e) <= 12 && <span className="badge-new" title={t("Son 12 saatte tespit", "Detected in the last 12 hours")}>{t("YENİ", "NEW")}</span>}
                <span className="ev-bar">
                  <span style={{ flex: e.n_modis, background: COLORS.MODIS }} />
                  <span style={{ flex: e.n_viirs, background: COLORS.VIIRS }} />
                </span>
              </div>
              <div className="ev-sub">
                {fmtDate(e.first_obs_utc.slice(0, 10))}
                {e.n_days > 1 && ` → ${fmtDate(e.last_obs_utc.slice(0, 10))}`} · {e.n_modis} M · {e.n_viirs} V
                {e.n_cross_links > 0 && " · ⇄"}
                {e.max_single_frp_mw !== null && sort === "frp" && ` · ${fmtNum(e.max_single_frp_mw, 0)} MW`}
                {ageH(e) <= 72 && <span className="recent"> · {t(`son tespit ${Math.max(0, Math.round(ageH(e)))} sa önce`, `last detection ${Math.max(0, Math.round(ageH(e)))} h ago`)}</span>}
                {e.n_flags > 0 && <span className="warn-txt"> · ⚠</span>}
              </div>
            </button>
          </li>
        ))}
      </ul>
      {list.length > 200 && <p className="hint">{t("İlk 200 olay gösteriliyor; tamamı “Dışa aktar” ile indirilebilir.", "Showing the first 200 events; download all of them with “Export”.")}</p>}
    </div>
  );
}

function Stat({ label, value, sub, color }: { label: string; value: number; sub?: string; color?: string }) {
  return (
    <div className="stat">
      <div className="stat-label">{color && <span className="dot" style={{ background: color }} />}{label}</div>
      <div className="stat-value">{fmtInt(value)}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}

function EventDetail({ summary, id, onSelectEvent, onSelectObs, onEventMembers }: Props & { id: string }) {
  const [data, setData] = useState<{ event: EventFull; members: Obs[] } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tab, setTab] = useState<"summary" | "obs">("summary");
  useEffect(() => {
    let alive = true;
    setData(null);
    setErr(null);
    api.event(summary.analysis_key, id)
      .then((d) => { if (alive) { setData(d); onEventMembers(d.members); } })
      .catch((e) => alive && setErr(errInfo(e).message));
    return () => { alive = false; onEventMembers(null); };
  }, [id, summary.analysis_key]);

  if (err) return <div className="panel"><BackBtn onClick={() => onSelectEvent(null)} /><div className="alert error">{err}</div></div>;
  if (!data) return <div className="panel"><BackBtn onClick={() => onSelectEvent(null)} /><div className="empty">{t("Yükleniyor…", "Loading…")}</div></div>;
  const ev = data.event;
  return (
    <div className="panel">
      <BackBtn onClick={() => onSelectEvent(null)} />
      <h2 className="mono ev-title">{ev.event_id}</h2>
      <div className="stats-grid three">
        <Stat label="MODIS" value={ev.n_modis} color={COLORS.MODIS} />
        <Stat label="VIIRS" value={ev.n_viirs} color={COLORS.VIIRS} />
        <Stat label={t("Gün", "Days")} value={ev.n_days} />
      </div>
      <div className="tabs">
        <button className={tab === "summary" ? "on" : ""} onClick={() => setTab("summary")}>{t("Özet", "Summary")}</button>
        <button className={tab === "obs" ? "on" : ""} onClick={() => setTab("obs")}>{t("Gözlemler", "Observations")} ({data.members.length})</button>
      </div>
      {tab === "summary" ? (
        <>
          <dl className="kv">
            <dt>{t("İlk gözlem", "First observation")}</dt><dd>{fmtUtc(ev.first_obs_utc)} <span className="muted">{fmtLocal(ev.first_obs_utc)}</span></dd>
            <dt>{t("Son gözlem", "Last observation")}</dt><dd>{fmtUtc(ev.last_obs_utc)} <span className="muted">{fmtLocal(ev.last_obs_utc)}</span></dd>
            <dt>{t("Uydular", "Satellites")}</dt><dd>{ev.satellites.join(", ")}</dd>
            <dt>{t("Ürünler", "Products")}</dt><dd>{ev.sources.join(", ")}</dd>
            <dt>{t("Karşı sensör eşi", "Cross-sensor match")}</dt><dd>{t(`${ev.n_cross_matched_obs} var · ${ev.n_unmatched_obs} yok`, `${ev.n_cross_matched_obs} matched · ${ev.n_unmatched_obs} unmatched`)}</dd>
            <dt>{t("Yayılım", "Extent")}</dt><dd>{fmtNum(ev.extent_km, 2)} km</dd>
            <dt>{t("En yüksek FRP", "Highest FRP")}</dt><dd>{ev.max_single_frp_mw === null ? "—" : `${fmtNum(ev.max_single_frp_mw)} MW`}</dd>
          </dl>
          <p className="hint">{t("İlk/son gözlem gerçek başlangıç/sönme zamanı değildir; kutu yanmış alan değildir.", "First/last observation is not the real ignition/extinction time; the box is not a burned area.")}</p>
          <div className="explain">{ev.explanation}</div>
          {ev.flags.length > 0 && <ul className="flags">{ev.flags.map((f) => <li key={f}>⚠ {f}</li>)}</ul>}

          <h4>{t("FRP gözlemleri", "FRP observations")}</h4>
          <FrpChart members={data.members} />

          <h4>{t("Geçişler", "Overpasses")}</h4>
          <div className="table-wrap">
            <table className="tbl">
              <thead><tr><th>UTC</th><th>{t("Uydu", "Satellite")}</th><th>n</th><th>{t("Maks FRP", "Max FRP")}</th><th>{t("Geçiş Σ", "Pass Σ")}</th></tr></thead>
              <tbody>
                {ev.passes.map((ps) => (
                  <tr key={ps.pass_id}>
                    <td>{ps.time_utc.slice(0, 16).replace("T", " ")}</td>
                    <td><span className="dot" style={{ background: COLORS[ps.instrument as "MODIS" | "VIIRS"] }} />{ps.satellite}</td>
                    <td>{ps.n_obs}</td>
                    <td>{fmtNum(ps.frp_max_mw)}</td>
                    <td>{fmtNum(ps.frp_pass_sum_mw)}{ps.n_frp_missing ? "*" : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="hint">{t("FRP MW cinsindendir. Σ yalnızca aynı geçişteki piksellerin toplamıdır; farklı saatler toplanmaz. VIIRS alt piksel FRP'leri paylaştırılmış olabilir.", "FRP is in MW. Σ is only the sum of pixels in the same overpass; different times are never summed. VIIRS sub-pixel FRP may be apportioned.")}</p>

          {ev.ambiguous_links.length > 0 && (
            <>
              <h4>{t("Belirsiz ilişkiler", "Ambiguous links")} ({ev.ambiguous_links.length})</h4>
              <ul className="amb">
                {ev.ambiguous_links.slice(0, 15).map((a, i) => {
                  const other = a.event_id === ev.event_id ? a.to_event_id : a.event_id;
                  return (
                    <li key={i}>
                      {other !== ev.event_id ? <button className="link mono" onClick={() => onSelectEvent(other)}>{other}</button> : "—"}{" "}
                      <span className="muted">{fmtNum(a.distance_km, 2)} km · {fmtNum(a.gap_h, 1)} {t("sa", "h")} — {a.reason}</span>
                    </li>
                  );
                })}
              </ul>
            </>
          )}
        </>
      ) : (
        <div className="table-wrap tall">
          <table className="tbl clickable">
            <thead><tr><th>UTC</th><th>{t("Uydu", "Satellite")}</th><th>FRP</th><th>Conf</th><th>{t("Eş", "Match")}</th></tr></thead>
            <tbody>
              {data.members.map((o) => (
                <tr key={o.observation_id} onClick={() => onSelectObs(o.observation_id)}>
                  <td>{o.observed_at_utc.slice(0, 16).replace("T", " ")}</td>
                  <td><span className="dot" style={{ background: COLORS[o.instrument] }} />{o.satellite}</td>
                  <td>{fmtNum(o.frp_mw)}</td>
                  <td>{o.confidence_raw}</td>
                  <td>{o.cross_matched ? `⇄${o.n_cross_links}` : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function BackBtn({ onClick, label }: { onClick: () => void; label?: string }) {
  return <button className="back" onClick={onClick}>← {label ?? t("Olay listesi", "Event list")}</button>;
}

function FrpChart({ members }: { members: Obs[] }) {
  const [hover, setHover] = useState<Obs | null>(null);
  const pts = members.filter((o) => o.frp_mw !== null);
  const missing = members.length - pts.length;
  if (!pts.length) return <div className="empty">{t("FRP değeri olan gözlem yok.", "No observations with an FRP value.")}</div>;
  const W = 320, H = 140, L = 34, R = 8, T = 10, B = 22;
  const ts = pts.map((o) => Date.parse(o.observed_at_utc));
  const t0 = Math.min(...ts), t1 = Math.max(...ts);
  const fmax = Math.max(...pts.map((o) => o.frp_mw!)) * 1.1 || 1;
  const x = (t: number) => L + (t1 === t0 ? (W - L - R) / 2 : ((t - t0) / (t1 - t0)) * (W - L - R));
  const y = (f: number) => T + (1 - f / fmax) * (H - T - B);
  const ticks = [0, Math.round(fmax / 2), Math.round(fmax)];
  return (
    <div className="frp">
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label={t("Zaman damgalı FRP gözlemleri", "Time-stamped FRP observations")}>
        {ticks.map((v) => (
          <g key={v}>
            <line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke="var(--grid)" />
            <text x={L - 5} y={y(v) + 3} textAnchor="end" className="axis">{v}</text>
          </g>
        ))}
        <text x={L} y={H - 5} className="axis">{new Date(t0).toISOString().slice(0, 10)}</text>
        {t1 !== t0 && <text x={W - R} y={H - 5} textAnchor="end" className="axis">{new Date(t1).toISOString().slice(0, 10)}</text>}
        {pts.map((o, i) => (
          <circle key={o.observation_id} cx={x(ts[i])} cy={y(o.frp_mw!)} r={hover === o ? 5.5 : 3.8}
            fill={COLORS[o.instrument]} stroke="#fff" strokeWidth={1.2}
            onMouseEnter={() => setHover(o)} onMouseLeave={() => setHover(null)} />
        ))}
      </svg>
      <div className="frp-foot">
        <span><span className="dot" style={{ background: COLORS.MODIS }} />MODIS</span>
        <span><span className="dot" style={{ background: COLORS.VIIRS }} />VIIRS</span>
        <span className="muted">
          {hover ? `${hover.satellite} · ${fmtUtc(hover.observed_at_utc)} · ${fmtNum(hover.frp_mw, 2)} MW`
            : missing ? t(`${missing} gözlemde FRP eksik (0 sayılmadı)`, `FRP missing for ${missing} observation(s) (not counted as 0)`) : t("MW · her nokta bir piksel", "MW · each point is one pixel")}
        </span>
      </div>
    </div>
  );
}

function ObsDetail({ summary, id, onSelectObs, onSelectEvent, selectedEventId }: Props & { id: string }) {
  const [data, setData] = useState<{ observation: ObsFull; links: CrossLink[] } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setData(null);
    api.observation(summary.analysis_key, id).then((d) => alive && setData(d)).catch((e) => alive && setErr(errInfo(e).message));
    return () => { alive = false; };
  }, [id, summary.analysis_key]);
  const back = <BackBtn onClick={() => onSelectObs(null)} label={selectedEventId ? t("Olaya dön", "Back to event") : t("Geri", "Back")} />;
  if (err) return <div className="panel">{back}<div className="alert error">{err}</div></div>;
  if (!data) return <div className="panel">{back}<div className="empty">{t("Yükleniyor…", "Loading…")}</div></div>;
  const o = data.observation;
  return (
    <div className="panel">
      {back}
      <h2 className="ev-title"><span className="dot big" style={{ background: COLORS[o.instrument] }} />{o.satellite} · {o.instrument}</h2>
      <dl className="kv">
        <dt>{t("Zaman", "Time")}</dt><dd>{fmtUtc(o.observed_at_utc)} <span className="muted">{fmtLocal(o.observed_at_utc)}</span></dd>
        <dt>{t("Kaynak", "Source")}</dt><dd>{o.source} · {t("sürüm", "version")} {o.version}</dd>
        <dt>{t("Konum", "Location")}</dt><dd>{o.latitude.toFixed(4)}, {o.longitude.toFixed(4)}</dd>
        <dt>FRP</dt><dd>{o.frp_mw === null ? t("eksik", "missing") : `${fmtNum(o.frp_mw, 2)} MW`}</dd>
        <dt>Confidence</dt><dd>{o.confidence_raw} <span className="muted">{o.instrument === "VIIRS" ? "(l/n/h)" : "(0–100)"}</span></dd>
        <dt>{t("Piksel", "Pixel")}</dt><dd>{fmtNum(o.scan_km, 2)} × {fmtNum(o.track_km, 2)} km</dd>
        <dt>{t("Kalite", "Quality")}</dt><dd>{o.quality_pass ? t("geçti", "passed") : `${t("dışlandı", "excluded")} — ${o.quality_excluded_reason}`}</dd>
        <dt>{t("Olay", "Event")}</dt>
        <dd>{o.event_id ? <button className="link mono" onClick={() => { onSelectObs(null); onSelectEvent(o.event_id); }}>{o.event_id}</button> : "—"}</dd>
        {o.match_flags && <><dt>{t("Uyarı", "Warning")}</dt><dd className="warn-txt">⚠ {o.match_flags}</dd></>}
      </dl>
      <h4>{t("Karşı sensör bağlantıları", "Cross-sensor links")} ({data.links.length})</h4>
      {data.links.length === 0 && <p className="hint">{t("Eşik içinde karşı sensör gözlemi yok. Gözlem eşleşmeyen olarak korunur.", "No cross-sensor observation within the threshold. The observation is kept as unmatched.")}</p>}
      <ul className="amb">
        {data.links.map((l) => {
          const other = o.instrument === "MODIS" ? l.viirs_observation_id : l.modis_observation_id;
          return (
            <li key={l.link_id}>
              <button className="link mono" onClick={() => onSelectObs(other)}>{o.instrument === "MODIS" ? l.viirs_satellite : l.modis_satellite}</button>{" "}
              <span className="muted">{fmtNum(l.distance_km, 2)} km ({t("eşik", "threshold")} {fmtNum(l.threshold_km, 2)}, {l.threshold_basis}) · Δt {fmtNum(l.dt_min, 0)} {t("dk", "min")}</span>
            </li>
          );
        })}
      </ul>
      <details className="raw">
        <summary>{t("Ham FIRMS kaydı", "Raw FIRMS record")}</summary>
        <table className="tbl">
          <tbody>{Object.entries(o.raw).map(([k, v]) => <tr key={k}><th>{k}</th><td>{String(v)}</td></tr>)}</tbody>
        </table>
        <div className="mono muted small">{o.observation_id}</div>
      </details>
    </div>
  );
}

const REGION_EN: Record<string, string> = {
  "Doğu Akdeniz (Türkiye, Yunanistan)": "Eastern Mediterranean (Türkiye, Greece)",
  "Sibirya": "Siberia",
  "Kuzey Avustralya": "Northern Australia",
};
const regionName = (r: string) => t(r, REGION_EN[r] ?? r);

export function ModelCard({ vm, dayFilter }: { vm: VirtualModis; dayFilter: string | null }) {
  if (!vm.available || !vm.model) {
    return (
      <div className="panel">
        <h3>Harmonize</h3>
        <div className="alert warn">{vm.reason ?? t("FireA kullanılamıyor.", "FireA is unavailable.")}</div>
      </div>
    );
  }
  const m = vm.model;
  const day = dayFilter ? vm.daily?.find((d) => d.date === dayFilter) : null;
  const tot = day
    ? { viirs: day.viirs_pixels, virt: day.virtual_mean, lo: day.virtual_lo, hi: day.virtual_hi, aqua: day.aqua_observed }
    : vm.totals
      ? { viirs: vm.totals.viirs_pixels, virt: vm.totals.virtual_mean, lo: null, hi: null, aqua: vm.totals.aqua_observed }
      : null;
  const rows: [string, string, VMMetrics | null][] = [
    ["FireA", m.name, m.test_metrics.FireA],
    ["GBM", t("gradyan artırma", "gradient boosting"), m.test_metrics.gbm],
    [t("Güç yasası", "Power law"), "μ = a·n^b", m.test_metrics.power_law],
  ];
  const f = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? "—" : v.toFixed(d));
  return (
    <div className="panel">
      <h3>Harmonize <span className="muted">· {m.name}</span></h3>
      <p className="hint">
        {t("Soru: “VIIRS'in gördüğü bu yangına aynı anda Aqua-MODIS baksaydı kaç tespit görürdü?” FireA, dünyanın farklı bölgelerindeki yakın zamanlı (≤60 dk) VIIRS–Aqua geçiş çiftlerinden öğrendi.",
          "Question: “If Aqua-MODIS had looked at this VIIRS fire at the same time, how many detections would it have reported?” FireA learned from near-simultaneous (≤60 min) VIIRS–Aqua overpass pairs in fire regions around the world.")}
      </p>
      {tot && (
        <div className="vm-hero">
          <div><span>{t("VIIRS pikseli", "VIIRS pixels")}</span><b>{fmtInt(tot.viirs)}</b></div>
          <div className="arrow">→</div>
          <div><span>{t("Harmonize", "Harmonized")}</span><b>≈ {fmtNum(tot.virt, 0)}</b>{tot.lo !== null && <small>{t("%80", "80%")}: {tot.lo}–{tot.hi}</small>}</div>
          <div><span>{t("Gerçek Aqua", "Real Aqua")}</span><b>{fmtInt(tot.aqua)}</b></div>
        </div>
      )}
      <p className="hint">{dayFilter ? fmtDate(dayFilter) : t("Tüm seçili dönem", "Whole selected period")} · {t("Haritadaki kesikli halkalar VIIRS kümeleridir; boyutları MODIS-eşdeğeri sayıyı, dolgu yoğunluğu MODIS'in algılama olasılığını gösterir.", "Dashed rings on the map are VIIRS clusters; their size shows the MODIS-equivalent count and fill strength the probability that MODIS detects them.")}</p>

      <h4>{t("Test başarımı (eğitimde görülmemiş bölgeler)", "Test performance (regions unseen in training)")}</h4>
      <div className="table-wrap">
        <table className="tbl">
          <thead><tr><th>Model</th><th title={t("Ortalama Poisson sapması (düşük iyi)", "Mean Poisson deviance (lower is better)")}>{t("Sapma", "Dev.")}</th><th title={t("Ortalama mutlak hata (küme başına tespit)", "Mean absolute error (detections per cluster)")}>MAE</th><th title={t("Algılama AUC (yüksek iyi)", "Detection AUC (higher is better)")}>AUC</th><th title={t("%80 aralığın gerçek değeri kapsama oranı", "Share of true values inside the 80% interval")}>{t("%80", "80%")}</th><th title={t("Bölge-gün toplamında medyan % hata", "Median % error of region-day totals")}>{t("Top.%", "Tot.%")}</th></tr></thead>
          <tbody>
            {rows.map(([k, sub, r]) => (
              <tr key={k}>
                <th>{k}<div className="muted small">{sub}</div></th>
                <td>{f(r?.poisson_deviance)}</td><td>{f(r?.mae)}</td><td>{f(r?.auc_detection)}</td>
                <td>{r?.interval80_coverage != null ? `${(r.interval80_coverage * 100).toFixed(0)}%` : "—"}</td>
                <td>{f(r?.region_day_total_abs_pct_err_median, 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="hint">{t("Test bölgeleri", "Test regions")}: {m.test_regions.map(regionName).join(", ")}. {t("Eğitim", "Training")}: {m.train_regions.length} {t("bölge", "regions")}, {m.split_sizes ? fmtInt(m.split_sizes.train) : "—"} {t("küme", "clusters")}. {fmtInt(m.n_params)} {t("parametre", "parameters")}.</p>

      <h4>{t("Sınırlar", "Limitations")}</h4>
      <ul className="flags plain">
        <li>{t("Sonuç bir tahmindir, gözlem değildir; aralıkla birlikte okunmalıdır.", "The result is an estimate, not an observation; read it together with the interval.")}</li>
        <li>{t("VIIRS'in kaçırdığı (yalnızca MODIS'in gördüğü) yangınlar tahmin edilemez.", "Fires missed by VIIRS (seen only by MODIS) cannot be estimated.")}</li>
        <li>{t("Bulut ve tam gözlem fırsatı bilinmiyor; MODIS'in bakıp bakmadığı yakın Aqua tespitlerinden çıkarıldı.", "Clouds and exact observation opportunity are unknown; whether MODIS looked was inferred from nearby Aqua detections.")}</li>
        <li>{t("Girdi tüm VIIRS güven sınıflarıdır; arayüzdeki kalite filtresinden bağımsızdır.", "Input is all VIIRS confidence classes, independent of the quality filter in the UI.")}</li>
      </ul>
    </div>
  );
}
