from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

from reportlab.graphics.shapes import Drawing, Line, PolyLine, Polygon, Rect, String
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .config import L

_FONTS = None


def _fonts():
    global _FONTS
    if _FONTS:
        return _FONTS
    cands = [(Path(r"C:\Windows\Fonts\arial.ttf"), Path(r"C:\Windows\Fonts\arialbd.ttf")),
             (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))]
    for reg, bold in cands:
        if reg.exists() and bold.exists():
            pdfmetrics.registerFont(TTFont("RPT", str(reg)))
            pdfmetrics.registerFont(TTFont("RPT-B", str(bold)))
            _FONTS = ("RPT", "RPT-B")
            return _FONTS
    _FONTS = ("Helvetica", "Helvetica-Bold")
    return _FONTS


C_MODIS, C_VIIRS, C_GRID, C_INK = colors.HexColor("#eb6834"), colors.HexColor("#2a78d6"), colors.HexColor("#dddddd"), colors.HexColor("#222222")
MONTHS = ["Oca", "Şub", "Mar", "Nis", "May", "Haz", "Tem", "Ağu", "Eyl", "Eki", "Kas", "Ara"]
MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _d(s: str) -> str:
    return f"{int(s[8:10])} {L(MONTHS[int(s[5:7]) - 1], MONTHS_EN[int(s[5:7]) - 1])} {s[:4]}"


def _n(v, d=0) -> str:
    if v is None:
        return "—"
    s = f"{v:,.{d}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _bar_chart(days, w, h, font):
    dr = Drawing(w, h)
    L, B, T = 30, 16, 8
    pw, ph = w - L - 6, h - B - T
    vmax = max([1] + [max(d["sensors"]["MODIS"]["n_detections"], d["sensors"]["VIIRS"]["n_detections"]) for d in days])
    n = len(days)
    for v in (0, vmax / 2, vmax):
        y = B + v / vmax * ph
        dr.add(Line(L, y, L + pw, y, strokeColor=C_GRID, strokeWidth=0.4))
        dr.add(String(L - 3, y - 2.5, _n(v), fontName=font, fontSize=6.5, textAnchor="end"))
    bw = pw / max(n, 1)
    for i, d in enumerate(days):
        for k, (key, col) in enumerate((("MODIS", C_MODIS), ("VIIRS", C_VIIRS))):
            v = d["sensors"][key]["n_detections"]
            if v:
                x = L + i * bw + (bw * 0.1) + k * bw * 0.4
                dr.add(Rect(x, B, max(bw * 0.38, 0.6), v / vmax * ph, fillColor=col, strokeColor=None))
    step = max(1, n // 6)
    for i in range(0, n, step):
        dr.add(String(L + i * bw + bw / 2, 4, _d(days[i]["date"])[:6], fontName=font, fontSize=6.5, textAnchor="middle"))
    return dr


def _harmony_chart(daily, w, h, font):
    dr = Drawing(w, h)
    L, B, T = 30, 16, 8
    pw, ph = w - L - 22, h - B - T
    n = len(daily)
    vmax = max([1] + [max(d["aqua_observed"], d["virtual_hi"]) for d in daily])
    x = lambda i: L + (pw / 2 if n == 1 else i / (n - 1) * pw)
    y = lambda v: B + v / vmax * ph
    for v in (0, vmax / 2, vmax):
        dr.add(Line(L, y(v), L + pw, y(v), strokeColor=C_GRID, strokeWidth=0.4))
        dr.add(String(L - 3, y(v) - 2.5, _n(v), fontName=font, fontSize=6.5, textAnchor="end"))
    band = [c for i, d in enumerate(daily) for c in (x(i), y(d["virtual_hi"]))] + \
           [c for i in range(n - 1, -1, -1) for c in (x(i), y(daily[i]["virtual_lo"]))]
    if n > 1:
        dr.add(Polygon(band, fillColor=colors.Color(0.16, 0.47, 0.84, alpha=0.18), strokeColor=None))
        dr.add(PolyLine([c for i, d in enumerate(daily) for c in (x(i), y(d["virtual_mean"]))], strokeColor=C_VIIRS, strokeWidth=1.4))
        dr.add(PolyLine([c for i, d in enumerate(daily) for c in (x(i), y(d["aqua_observed"]))], strokeColor=C_MODIS, strokeWidth=1.4))
    step = max(1, n // 6)
    for i in range(0, n, step):
        dr.add(String(x(i), 4, _d(daily[i]["date"])[:6], fontName=font, fontSize=6.5, textAnchor="middle"))
    return dr


def build_report(r: dict) -> bytes:
    F, FB = _fonts()
    s = r["summary"]
    ds, st, cal = s["dataset"], s["stats"], s["calendar"]["days"]
    vm = r.get("vm") or {}
    events = sorted(r["events_list"], key=lambda e: -e["n_obs"])[:10]
    body = ParagraphStyle("b", fontName=F, fontSize=8.5, leading=11)
    small = ParagraphStyle("s", fontName=F, fontSize=7.2, leading=9, textColor=colors.HexColor("#555555"))
    h1 = ParagraphStyle("h1", fontName=FB, fontSize=15, leading=18, textColor=colors.HexColor("#1c3557"))
    h2 = ParagraphStyle("h2", fontName=FB, fontSize=10, leading=13, spaceBefore=6, spaceAfter=2, textColor=colors.HexColor("#1c3557"))

    days_with = sum(1 for d in cal if d["class"] in ("both", "modis_only", "viirs_only"))
    max_frp = max([e["max_single_frp_mw"] or 0 for e in r["events_list"]] or [0])
    kpis = [(L("Tespit (filtre sonrası)", "Detections (after filter)"), _n(st["n_observations_used"])), (L("Aday olay", "Candidate events"), _n(st["n_events"])),
            (L("Tespitli gün", "Days with detections"), f"{days_with} / {len(cal)}"), (L("En yüksek tekil FRP", "Highest single FRP"), f"{_n(max_frp, 1)} MW")]
    if vm.get("available"):
        t = vm["totals"]
        kpis.append((L("Harmonize (MODIS-eşd.)", "Harmonized (MODIS-eq.)"), f"≈ {_n(t['virtual_mean'])} ({_n(t.get('virtual_lo'))}–{_n(t.get('virtual_hi'))})"))
        kpis.append((L("Gerçek Aqua-MODIS", "Real Aqua-MODIS"), _n(t["aqua_observed"])))

    story = [
        Paragraph(L("Aktif Yangın Gözlem Özeti", "Active Fire Observation Summary"), h1),
        Paragraph(f"{L('ÖRNEK VERİ (sentetik) · ', 'SAMPLE DATA (synthetic) · ') if ds['is_sample'] else ''}{_d(ds['start'])} – {_d(ds['end'])} (UTC) · "
                  f"{L('bölge', 'area')} {', '.join(str(x) for x in ds['bbox']) if ds['bbox'] else L('yüklenen dosya', 'uploaded file')} {L('(B, G, D, K)', '(W, S, E, N)')} · "
                  f"{L('ürünler', 'products')}: {', '.join(sorted(ds['products_versions'])) or '—'}", small),
        Spacer(1, 6),
    ]
    kt = Table([[Paragraph(f"<font size=7 color='#666666'>{k}</font><br/><font name='{FB}' size=11>{v}</font>", body)
                 for k, v in kpis[i:i + 3]] for i in range(0, len(kpis), 3)], colWidths=[60 * mm] * 3)
    kt.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
                            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f6f6f3"))]))
    story += [kt, Paragraph(L("Günlük tespitler (filtre sonrası; turuncu MODIS, mavi VIIRS)", "Daily detections (after filter; orange MODIS, blue VIIRS)"), h2), _bar_chart(cal, 180 * mm, 42 * mm, F)]
    if vm.get("available") and vm.get("daily"):
        story += [Paragraph(L(f"Harmonize günlük etkinlik — {vm['model']['name']} (mavi: VIIRS'ten MODIS-eşdeğeri, %80 bant; turuncu: gerçek Aqua-MODIS)",
                              f"Harmonized daily activity — {vm['model']['name']} (blue: MODIS equivalent from VIIRS, 80% band; orange: real Aqua-MODIS)"), h2), _harmony_chart(vm["daily"], 180 * mm, 42 * mm, F)]
    rows = [[L("Olay", "Event"), L("İlk gözlem (UTC)", "First obs. (UTC)"), L("Son gözlem (UTC)", "Last obs. (UTC)"), L("Gün", "Days"), "MODIS", "VIIRS", L("Maks FRP (MW)", "Max FRP (MW)"), L("Konum", "Location")]]
    for e in events:
        rows.append([e["event_id"], e["first_obs_utc"][:16].replace("T", " "), e["last_obs_utc"][:16].replace("T", " "),
                     e["n_days"], e["n_modis"], e["n_viirs"], _n(e["max_single_frp_mw"], 1),
                     f"{e['centroid_lat']:.3f}, {e['centroid_lon']:.3f}"])
    et = Table(rows, repeatRows=1, colWidths=[22 * mm, 27 * mm, 27 * mm, 10 * mm, 13 * mm, 13 * mm, 22 * mm, 30 * mm])
    et.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), F, 7), ("FONT", (0, 0), (-1, 0), FB, 7),
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eceae3")),
                            ("LINEBELOW", (0, 0), (-1, -1), 0.3, colors.HexColor("#dddddd")),
                            ("TOPPADDING", (0, 0), (-1, -1), 1.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5)]))
    story += [Paragraph(L(f"En büyük {len(events)} aday olay (gözlem sayısına göre)", f"Largest {len(events)} candidate events (by observation count)"), h2), et, Spacer(1, 6),
              Paragraph(L("<b>Okurken dikkat:</b> Nokta sayısı yangın sayısı değildir. Aday olay, konum/zaman yakınlığına göre "
                          "ilişkilendirilmiş tespit kümesidir; ilk/son gözlem gerçek başlangıç/sönme zamanı değildir. Tespit olmayan "
                          "gün yangın yokluğunu doğrulamaz (bulut/gözlem fırsatı bilinmiyor). Harmonize değer bir model tahminidir. "
                          "Eşleştirme eşikleri deneyseldir.",
                          "<b>Read with care:</b> Point count is not fire count. A candidate event is a group of detections linked "
                          "by proximity in space and time; first/last observation is not the real ignition/extinction time. A day "
                          "without detections does not verify the absence of fire (clouds/observation opportunity unknown). The "
                          "harmonized value is a model estimate. Matching thresholds are experimental."), small),
              Paragraph(L("Kaynak: NASA FIRMS. Üretildi: ", "Source: NASA FIRMS. Generated: ") + f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · "
                        f"{L('veri seti', 'dataset')} {ds['dataset_id']}.", small)]
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=13 * mm, bottomMargin=12 * mm,
                      title=L("Aktif Yangın Gözlem Özeti", "Active Fire Observation Summary")).build(story)
    return buf.getvalue()
