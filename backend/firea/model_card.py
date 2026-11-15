from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.firea_infer import _find_model_dir
from firea.features import EVID_MAX_KM

REGION_EN = {
    "east_med": ("Eastern Mediterranean (Türkiye, Greece)", "Mediterranean shrubland/forest"),
    "iberia": ("Iberian Peninsula", "Mediterranean shrubland/forest"),
    "california": ("California", "Mediterranean shrubland/forest"),
    "canada": ("Canadian boreal", "Boreal forest"),
    "siberia": ("Siberia", "Boreal forest"),
    "amazon": ("Amazon arc", "Tropical forest / deforestation"),
    "cerrado": ("Cerrado", "Tropical savanna"),
    "central_africa": ("Central Africa (Angola, Zambia, DRC)", "Savanna"),
    "sahel": ("West Africa / Sahel", "Savanna"),
    "se_asia": ("Southeast Asia (Myanmar, Thailand, Laos)", "Tropical dry forest / agriculture"),
    "north_australia": ("Northern Australia", "Savanna"),
    "punjab": ("India / Punjab", "Crop residue burning"),
    "east_europe": ("Eastern European steppe", "Agriculture / steppe"),
    "mexico": ("Mexico / Central America", "Tropical dry forest"),
    "indonesia": ("Indonesia (Sumatra, Kalimantan)", "Peatland / tropical forest"),
}


def num(v, d, tr):
    s = f"{v:,.{d}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".") if tr else s


def pct(v, d, tr):
    return f"%{num(v, d, tr)}" if tr else f"{num(v, d, tr)}%"


def build(cfg, met, tr):
    t, s = met["test"], met["split_sizes"]
    n = lambda v, d=0: num(v, d, tr)
    p = lambda v, d=1: pct(v, d, tr)
    labels = ((("FireA", cfg["name"]), ("gbm", "Gradyan artırma (Poisson)"), ("power_law", "Güç yasası (μ = a·n^b)")) if tr
              else (("FireA", cfg["name"]), ("gbm", "Gradient boosting (Poisson)"), ("power_law", "Power law (μ = a·n^b)")))
    rows = []
    for k, label in labels:
        m = t[k]
        name = f"**{label}**" if k == "FireA" else label
        rows.append(f"| {name} | {n(m['poisson_deviance'], 3)} | {n(m['mae'], 3)} | {n(m['auc_detection'], 3)} | "
                    f"{n(m['brier_detection'], 3)} | {p(m['interval80_coverage'] * 100)} | {p(m['region_day_total_abs_pct_err_median'])} |")
    split_name = {"train": "eğitim", "test": "test"} if tr else {"train": "train", "test": "test"}
    regions = "\n".join(
        f"| {r['name'] if tr else REGION_EN.get(r['key'], (r['name'],))[0]} | "
        f"{r['biome'] if tr else REGION_EN.get(r['key'], (0, r['biome']))[1]} | {split_name.get(r['split'], r['split'])} |"
        for r in cfg["regions"])
    f = t["FireA"]
    a = cfg["arch"]
    final_n = s.get("final")
    names = {r["key"]: (r["name"] if tr else REGION_EN.get(r["key"], (r["name"],))[0]) for r in cfg["regions"]}
    excl = ", ".join(names.get(k, k) for k in met.get("final_excludes", [])) or "-"
    final_tr = f"; toplam {n(final_n)} küme" if final_n else ""
    final_en = f"; {n(final_n)} clusters in total" if final_n else ""
    if tr:
        return f"""# {cfg['name']}: model kartı

[English](MODEL_CARD.md) · **Türkçe**

## Ne yapıyor?

FireA şu soruyu cevaplamak için eğitildi: VIIRS'in gördüğü bir yangına aynı anda Aqua-MODIS baksaydı kaç tespit görürdü?

Girdi, tek bir VIIRS geçişinde birbirine yakın piksellerden oluşan bir kümedir. Çıktı tek bir sayı değil, bir olasılık dağılımıdır (negatif binom). Bu dağılımdan beklenen MODIS tespit sayısını, %80 aralığı ve MODIS'in kümeyi en az bir tespitle görme olasılığını hesaplıyoruz.

## Model

- {n(cfg['n_params'])} parametreli bir set transformer: {a['layers']} katman, d={a['d']}, {a['heads']} başlık.
- Her küme en fazla 64 piksel olarak verilir (piksel başına 12 öznitelik). Yanında kümenin geneline ait {a['n_ctx']} bağlam özniteliği vardır.
- Kayıp fonksiyonu negatif binom log-olabilirliğidir. AdamW ile {cfg['best_epoch']} epoch eğitildi, yalnızca CPU kullanıldı.
- Eğitim tarihi: {cfg['trained_at_utc']}.

## Veri

- NASA FIRMS arşiv ürünleri: MODIS C6.1, VIIRS S-NPP ve VIIRS NOAA-20 (375 m), 2019–2024.
- {len(cfg['regions'])} bölge. Her bölgeden 50 gün aldık: 26'sı yangın mevsiminden, 24'ü yılın 12 ayına yayılmış. Böylece model kışı ve sakin dönemleri de görüyor.
- Volkanlar, statik ısı kaynakları ve deniz üstü tespitler (`type` 1–3) çıkarıldı.
- Etiket: kümeye en yakın Aqua geçişinde (en fazla 60 dk fark) kümeye düşen MODIS tespiti sayısı.
- MODIS'in o bölgeye gerçekten baktığından emin olmak için, aynı geçişte kümeden 10–{EVID_MAX_KM:,.0f} km uzakta başka Aqua tespitleri olmasını şart koştuk. Kümenin kendi üzerindeki tespitleri bu kanıta katmadık; aksi halde etiket, kümenin veriye girip girmemesini etkilerdi. Kanıtı olmayan kümeler kullanılmadı.
- Bağlam öznitelikleri arasında küme boyutu, FRP, parlaklık, güven sınıfı, bakış geometrisi, yerel saat, mevsim (yarıküreye göre), mutlak enlem ve aynı geçişte 25 km içindeki VIIRS piksel sayısı var.
- Bölge başına en fazla {n(met.get('region_cap', 0))} küme. Eğitim {n(s['train'])}, doğrulama {n(s['val'])}, test {n(s['test'])} küme.

| Bölge | Biyom | Kullanım |
|---|---|---|
{regions}

## Test sonuçları

Test bölgeleri değerlendirme modelinin eğitiminde hiç kullanılmadı. Sonuçlar {n(f['n_clusters'])} küme üzerinden:

| Model | Poisson sapması ↓ | MAE ↓ | Algılama AUC ↑ | Brier ↓ | %80 aralık kapsaması | Bölge-gün toplam hatası (medyan) ↓ |
|---|---|---|---|---|---|---|
{chr(10).join(rows)}

- FireA, güç yasasından açıkça daha iyi. Gradyan artırmadan da her ölçütte biraz daha iyi, ancak aradaki fark küçük.
- %80 aralığın kapsama oranı {p(f['interval80_coverage'] * 100)}; yani aralıklar gerektiğinden biraz geniş.
- Bütün test kümelerinde FireA {n(f['total_predicted'])} MODIS tespiti öngördü, gerçekte {n(f['total_observed'])} tespit vardı.

Uygulamada kullanılan model aynı ayarlarla yeniden eğitildi; bu kez {excl} dışındaki bütün bölgeler kullanıldı{final_tr}. Doğu Akdeniz'i dışarıda bırakmamızın nedeni, Antalya–Muğla dahil bu bölgedeki kontrollerin bağımsız kalması.

## Uygulamada nasıl kullanılıyor?

- Tahmin, iki uydunun aynı anda baktığı varsayımıyla yapılır (Δt = 0).
- MODIS'in o yangını hangi açıdan göreceğini bilmediğimiz için 16 farklı piksel boyutu için tahmin yapıp ortalamasını alıyoruz.
- Günlük ve aylık toplamların aralıkları Monte Carlo ile hesaplanır.
- Model bütün VIIRS güven sınıflarını kullanır; arayüzdeki kalite filtresi modeli etkilemez.

## Sınırlar

- Çıktı bir tahmindir, gözlem değildir. VIIRS'in görmediği ama MODIS'in gördüğü yangınlar bu yolla bulunamaz.
- Bulut bilgisi ve tam yörünge geometrisi kullanılmadı. MODIS'in bölgeye bakıp bakmadığını yakındaki tespitlerden çıkardık; bu yüzden veri, yangının yoğun olduğu yerlere doğru biraz yanlı olabilir.
- Etiketler yalnızca Aqua'dan geliyor, çünkü Terra VIIRS ile aynı saatlerde geçmiyor. Bu yüzden çıktı "Aqua-MODIS eşdeğeri"dir.
- İki geçiş arasında en fazla 60 dakika olabilir ve yangın bu sürede değişebilir. Bu farkı modele girdi olarak verdik.
- Küme tanımı ve eşleştirme mesafesi bizim seçtiğimiz deneysel değerlerdir.
"""
    return f"""# {cfg['name']}: model card

**English** · [Türkçe](MODEL_CARD.tr.md)

## What it does

FireA was trained to answer one question: if Aqua-MODIS had looked at a fire seen by VIIRS at the same moment, how many detections would it have reported?

The input is a cluster of nearby pixels from one VIIRS overpass. The output is not a single number but a probability distribution (negative binomial). From it we compute the expected number of MODIS detections, an 80% interval and the probability that MODIS detects the cluster at all.

## Model

- A set transformer with {n(cfg['n_params'])} parameters: {a['layers']} layers, d={a['d']}, {a['heads']} heads.
- Each cluster is given as up to 64 pixels (12 features per pixel), plus {a['n_ctx']} context features describing the whole cluster.
- The loss is the negative binomial log-likelihood. It was trained with AdamW for {cfg['best_epoch']} epochs on a CPU.
- Trained on: {cfg['trained_at_utc']}.

## Data

- NASA FIRMS archive products: MODIS C6.1, VIIRS S-NPP and VIIRS NOAA-20 (375 m), 2019–2024.
- {len(cfg['regions'])} regions. We took 50 days from each region: 26 from the fire season and 24 spread over all 12 months, so the model also sees winter and quiet periods.
- Volcanoes, static heat sources and offshore detections (`type` 1–3) were removed.
- Label: the number of MODIS detections that fall on the cluster in the Aqua overpass closest in time (at most 60 min apart).
- To make sure MODIS actually looked at the area, we required other Aqua detections 10–{EVID_MAX_KM:,.0f} km away in the same overpass. Detections on the cluster itself were not counted as evidence; otherwise the label would decide whether a cluster enters the data. Clusters without evidence were not used.
- Context features include cluster size, FRP, brightness, confidence class, viewing geometry, local time, season (hemisphere-aware), absolute latitude and the number of VIIRS pixels within 25 km in the same overpass.
- At most {n(met.get('region_cap', 0))} clusters per region. Train {n(s['train'])}, validation {n(s['val'])}, test {n(s['test'])} clusters.

| Region | Biome | Used for |
|---|---|---|
{regions}

## Test results

The test regions were never used to train the evaluation model. Results on {n(f['n_clusters'])} clusters:

| Model | Poisson deviance ↓ | MAE ↓ | Detection AUC ↑ | Brier ↓ | 80% interval coverage | Region-day total error (median) ↓ |
|---|---|---|---|---|---|---|
{chr(10).join(rows)}

- FireA is clearly better than the power law. It is also slightly better than gradient boosting on every metric, but the difference is small.
- The 80% intervals contain {p(f['interval80_coverage'] * 100)} of the real values, so they are a little wider than they need to be.
- Over all test clusters FireA predicted {n(f['total_predicted'])} MODIS detections; the real number was {n(f['total_observed'])}.

The model used in the app was retrained with the same settings on all regions except {excl}{final_en}. We left the Eastern Mediterranean out so that checks in that region, including the Antalya–Muğla box, stay independent.

## How the app uses it

- Predictions assume both satellites look at the same moment (Δt = 0).
- We do not know from which angle MODIS would see the fire, so we predict for 16 different pixel sizes and average them.
- Intervals for daily and monthly totals are computed with Monte Carlo sampling.
- The model uses all VIIRS confidence classes; the quality filter in the interface does not affect it.

## Limitations

- The output is an estimate, not an observation. Fires that MODIS sees but VIIRS misses cannot be recovered this way.
- Cloud information and exact orbit geometry were not used. We inferred whether MODIS looked at an area from nearby detections, so the data may be slightly biased towards areas with many fires.
- Labels come only from Aqua, because Terra does not pass at the same time as VIIRS. The output is therefore an "Aqua-MODIS equivalent".
- The two overpasses can be up to 60 minutes apart and the fire may change in that time. We give this time difference to the model as an input.
- The cluster definition and matching distance are experimental choices we made.
"""


def main():
    d = _find_model_dir()
    if d is None:
        print("Model bulunamadı.")
        return
    cfg = json.loads((d / "config.json").read_text(encoding="utf-8"))
    met = json.loads((d / "metrics.json").read_text(encoding="utf-8"))
    (d / "MODEL_CARD.md").write_text(build(cfg, met, False), encoding="utf-8")
    (d / "MODEL_CARD.tr.md").write_text(build(cfg, met, True), encoding="utf-8")
    print(f"Yazıldı: {d / 'MODEL_CARD.md'}, {d / 'MODEL_CARD.tr.md'}")


if __name__ == "__main__":
    main()
