import { useEffect, useRef } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import type { BBox, EventLite, Obs, VMCluster } from "./types";
import { escapeHtml, fmtLocal, fmtNum, fmtUtc, MAP_COLORS } from "./util";
import { t } from "./i18n";

export { MAP_COLORS };

// Leaflet can run a queued canvas redraw after the map has been removed.
const canvasProto = L.Canvas.prototype as unknown as { _redraw: () => void; _ctx?: unknown; _redrawRequest: number | null };
const origRedraw = canvasProto._redraw;
canvasProto._redraw = function (this: typeof canvasProto) {
  if (!this._ctx) {
    this._redrawRequest = null;
    return;
  }
  origRedraw.call(this);
};

function eventColorDark(id: string | null): string {
  if (!id) return MAP_COLORS.excluded;
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360}, 70%, 62%)`;
}

interface Props {
  observations: Obs[];
  events: EventLite[];
  mode: "raw" | "events";
  view: "real" | "modis";
  paletteKey?: string;
  vmClusters: VMCluster[];
  showModis: boolean;
  showViirs: boolean;
  showExcluded: boolean;
  selectedEventId: string | null;
  selectedObsId: string | null;
  bbox: BBox;
  dataBbox?: BBox | null;
  drawMode: boolean;
  fitKey: string;
  onSelectObs: (id: string) => void;
  onSelectEvent: (id: string) => void;
  onBboxDrawn: (b: BBox) => void;
}

function popupHtml(o: Obs): string {
  const rows: [string, string][] = [
    [t("Kaynak", "Source"), o.source],
    [t("Uydu", "Satellite"), `${o.satellite} · ${o.instrument}`],
    [t("Zaman", "Time"), `${fmtUtc(o.observed_at_utc)}<br><span class="muted">${fmtLocal(o.observed_at_utc)}</span>`],
    ["FRP", o.frp_mw === null ? t("eksik", "missing") : `${fmtNum(o.frp_mw, 2)} MW`],
    [t("Confidence (ham)", "Confidence (raw)"), escapeHtml(o.confidence_raw || "—") + (o.instrument === "VIIRS" ? " · l/n/h" : " · 0–100")],
    [t("Olay", "Event"), o.event_id ?? (o.quality_pass ? "—" : t("filtreyle dışlandı", "excluded by filter"))],
    [t("Karşı sensör eşi", "Cross-sensor match"), o.cross_matched ? t(`${o.n_cross_links} bağlantı`, `${o.n_cross_links} link(s)`) : t("yok", "none")],
  ];
  return `<table class="popup">${rows.map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join("")}</table>`;
}

export default function MapView(p: Props) {
  const divRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<L.Map | null>(null);
  const dataLayer = useRef<L.LayerGroup | null>(null);
  const boxLayer = useRef<L.LayerGroup | null>(null);
  const renderer = useRef<L.Canvas | null>(null);
  const cb = useRef(p);
  cb.current = p;

  useEffect(() => {
    if (!divRef.current || mapRef.current) return;
    const map = L.map(divRef.current, { preferCanvas: true, zoomControl: false }).setView([36.9, 29.8], 7);
    L.control.zoom({ position: "bottomright" }).addTo(map);
    const attr = "Esri, HERE, Garmin, © OpenStreetMap contributors";
    L.tileLayer("https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 16, attribution: attr }).addTo(map);
    map.createPane("labels");
    map.getPane("labels")!.style.zIndex = "450";
    map.getPane("labels")!.style.pointerEvents = "none";
    L.tileLayer("https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Reference/MapServer/tile/{z}/{y}/{x}",
      { maxZoom: 16, pane: "labels" }).addTo(map);
    L.control.scale({ metric: true, imperial: false, position: "bottomleft" }).addTo(map);
    renderer.current = L.canvas({ padding: 0.3, tolerance: 5 });
    dataLayer.current = L.layerGroup().addTo(map);
    boxLayer.current = L.layerGroup().addTo(map);
    mapRef.current = map;
    return () => {
      dataLayer.current?.remove();
      boxLayer.current?.remove();
      map.remove();
      mapRef.current = null;
    };
  }, []);

  useEffect(() => {
    const g = boxLayer.current;
    if (!g) return;
    g.clearLayers();
    const [w, s, e, n] = p.bbox;
    L.rectangle([[s, w], [n, e]], { color: "#9aa4ae", weight: 1, dashArray: "5 5", fill: false, interactive: false }).addTo(g);
    if (p.dataBbox) {
      const [dw, ds, de, dn] = p.dataBbox;
      L.rectangle([[ds, dw], [dn, de]], { color: "#ffc94a", weight: 1, dashArray: "2 4", fill: false })
        .bindTooltip(t("Gösterilen verilerin bölgesi", "Area of the data shown"), { sticky: true }).addTo(g);
    }
    const ev = p.events.find((x) => x.event_id === p.selectedEventId);
    if (ev) {
      const [bw, bs, be, bn] = ev.bbox;
      const pad = 0.004;
      L.rectangle([[bs - pad, bw - pad], [bn + pad, be + pad]], { color: "#ffffff", weight: 1.3, dashArray: "3 3", fill: false })
        .bindTooltip(t("Gözlem sınır kutusu — yanmış alan değildir", "Observation bounding box — not a burned area"), { sticky: true })
        .addTo(g);
    }
  }, [p.bbox, p.dataBbox, p.selectedEventId, p.events, p.paletteKey]);

  useEffect(() => {
    const g = dataLayer.current;
    const r = renderer.current;
    if (!g || !r) return;
    g.clearLayers();
    const sel = p.selectedEventId;
    for (const o of p.observations) {
      if (p.view === "modis" && (o.instrument !== "MODIS" || o.satellite !== "Aqua")) continue;
      if (o.instrument === "MODIS" && !p.showModis) continue;
      if (o.instrument === "VIIRS" && !p.showViirs) continue;
      if (!o.quality_pass && !p.showExcluded) continue;
      const isSel = o.observation_id === p.selectedObsId;
      const dim = sel !== null && o.event_id !== sel;
      const color = !o.quality_pass ? MAP_COLORS.excluded : p.mode === "raw" ? MAP_COLORS[o.instrument] : eventColorDark(o.event_id);
      const m = L.circleMarker([o.latitude, o.longitude], {
        renderer: r,
        radius: isSel ? 8 : o.instrument === "MODIS" ? 5 : 3.2,
        color: isSel ? "#ffffff" : "rgba(255,255,255,0.55)",
        weight: isSel ? 2.2 : 0.6,
        fillColor: color,
        fillOpacity: !o.quality_pass ? 0.3 : dim ? 0.15 : 0.92,
        opacity: dim ? 0.2 : 1,
      });
      m.bindPopup(() => popupHtml(o), { maxWidth: 300 });
      m.on("popupopen", () => { if (cb.current.drawMode) m.closePopup(); });
      m.on("click", () => { if (!cb.current.drawMode) cb.current.onSelectObs(o.observation_id); });
      m.addTo(g);
    }
    if (p.view === "modis") {
      for (const c of p.vmClusters) {
        const m = L.circleMarker([c.lat, c.lon], {
          renderer: r,
          radius: Math.min(18, 3 + 3 * Math.sqrt(c.modis_like_mean)),
          color: "#8ec2ff",
          weight: 1.2,
          dashArray: "3 2",
          fillColor: MAP_COLORS.harmon,
          fillOpacity: 0.1 + 0.55 * c.p_detect,
        });
        m.bindTooltip(
          t(`<b>VIIRS ${c.n_viirs} piksel → harmonize ≈ ${fmtNum(c.modis_like_mean, 1)} MODIS-eşdeğeri</b><br>%80 aralık: ${c.modis_like_lo}–${c.modis_like_hi} · P(MODIS algılar) %${fmtNum(c.p_detect * 100, 0)}`,
            `<b>VIIRS ${c.n_viirs} pixels → harmonized ≈ ${fmtNum(c.modis_like_mean, 1)} MODIS equivalent</b><br>80% interval: ${c.modis_like_lo}–${c.modis_like_hi} · P(MODIS detects) ${fmtNum(c.p_detect * 100, 0)}%`) +
            `<br>${c.satellite} · ${fmtUtc(c.time_utc)} · ΣFRP ${fmtNum(c.frp_sum_mw, 0)} MW`,
        );
        m.addTo(g);
      }
    }
    if (p.mode === "events" && p.view === "real") {
      for (const ev of p.events) {
        const isSel = ev.event_id === sel;
        const m = L.circleMarker([ev.centroid_lat, ev.centroid_lon], {
          renderer: r,
          radius: Math.min(16, 4 + Math.sqrt(ev.n_obs)),
          color: isSel ? "#ffffff" : eventColorDark(ev.event_id),
          weight: isSel ? 2.5 : 1.6,
          fill: p.observations.length === 0,
          fillOpacity: 0.25,
          dashArray: ev.n_flags ? "4 3" : undefined,
        });
        m.bindTooltip(`${ev.event_id} · ${ev.n_modis} MODIS / ${ev.n_viirs} VIIRS · ${ev.n_days} ${t("gün", "day(s)")}`);
        m.on("click", () => { if (!cb.current.drawMode) cb.current.onSelectEvent(ev.event_id); });
        m.addTo(g);
      }
    }
  }, [p.observations, p.events, p.mode, p.view, p.vmClusters, p.showModis, p.showViirs, p.showExcluded, p.selectedEventId, p.selectedObsId, p.paletteKey]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const [w, s, e, n] = p.bbox;
    map.fitBounds([[s, w], [n, e]], { padding: [20, 20] });
  }, [p.fitKey]);

  useEffect(() => {
    const map = mapRef.current;
    const ev = p.events.find((x) => x.event_id === p.selectedEventId);
    if (map && ev) {
      const [w, s, e, n] = ev.bbox;
      map.fitBounds([[s - 0.02, w - 0.02], [n + 0.02, e + 0.02]], { maxZoom: 12 });
    }
  }, [p.selectedEventId]);

  useEffect(() => {
    const t = setTimeout(() => mapRef.current?.invalidateSize(), 50);
    return () => clearTimeout(t);
  });

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !p.drawMode) return;
    map.closePopup();
    map.dragging.disable();
    map.doubleClickZoom.disable();
    map.boxZoom.disable();
    const box = map.getContainer();
    box.style.cursor = "crosshair";
    let start: L.LatLng | null = null;
    let rect: L.Rectangle | null = null;
    const clamp = (ll: L.LatLng) => L.latLng(Math.max(-90, Math.min(90, ll.lat)), Math.max(-180, Math.min(180, ll.lng)));
    const clear = () => {
      rect?.remove();
      rect = null;
      start = null;
    };
    const down = (e: L.LeafletMouseEvent) => {
      if (e.originalEvent.button !== 0) return;
      clear();
      start = clamp(e.latlng);
      rect = L.rectangle(L.latLngBounds(start, start), { color: "#ff5a5a", weight: 2, fillOpacity: 0.06, interactive: false }).addTo(map);
    };
    const move = (e: L.LeafletMouseEvent) => {
      if (start && rect) rect.setBounds(L.latLngBounds(start, clamp(e.latlng)));
    };
    const finish = (end: L.LatLng | null) => {
      if (!start) return;
      const b = end ? L.latLngBounds(start, clamp(end)) : rect?.getBounds() ?? null;
      clear();
      setTimeout(() => map.closePopup(), 0);
      if (!b) return;
      const r = (v: number) => Math.round(v * 1000) / 1000;
      if (b.getEast() - b.getWest() > 0.01 && b.getNorth() - b.getSouth() > 0.01)
        cb.current.onBboxDrawn([r(b.getWest()), r(b.getSouth()), r(b.getEast()), r(b.getNorth())]);
    };
    const up = (e: L.LeafletMouseEvent) => finish(e.latlng);
    const docUp = () => finish(null);
    map.on("mousedown", down);
    map.on("mousemove", move);
    map.on("mouseup", up);
    document.addEventListener("mouseup", docUp);
    return () => {
      map.off("mousedown", down);
      map.off("mousemove", move);
      map.off("mouseup", up);
      document.removeEventListener("mouseup", docUp);
      clear();
      map.dragging.enable();
      map.doubleClickZoom.enable();
      map.boxZoom.enable();
      box.style.cursor = "";
    };
  }, [p.drawMode]);

  return <div ref={divRef} className="map" />;
}
