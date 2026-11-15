# FireA-v1: model kartı

[English](MODEL_CARD.md) · **Türkçe**

## Ne yapıyor?

FireA şu soruyu cevaplamak için eğitildi: VIIRS'in gördüğü bir yangına aynı anda Aqua-MODIS baksaydı kaç tespit görürdü?

Girdi, tek bir VIIRS geçişinde birbirine yakın piksellerden oluşan bir kümedir. Çıktı tek bir sayı değil, bir olasılık dağılımıdır (negatif binom). Bu dağılımdan beklenen MODIS tespit sayısını, %80 aralığı ve MODIS'in kümeyi en az bir tespitle görme olasılığını hesaplıyoruz.

## Model

- 2.122.178 parametreli bir set transformer: 4 katman, d=192, 6 başlık.
- Her küme en fazla 64 piksel olarak verilir (piksel başına 12 öznitelik). Yanında kümenin geneline ait 25 bağlam özniteliği vardır.
- Kayıp fonksiyonu negatif binom log-olabilirliğidir. AdamW ile 5 epoch eğitildi, yalnızca CPU kullanıldı.
- Eğitim tarihi: 2026-11-14T21:44:30Z.

## Veri

- NASA FIRMS arşiv ürünleri: MODIS C6.1, VIIRS S-NPP ve VIIRS NOAA-20 (375 m), 2019–2024.
- 15 bölge. Her bölgeden 50 gün aldık: 26'sı yangın mevsiminden, 24'ü yılın 12 ayına yayılmış. Böylece model kışı ve sakin dönemleri de görüyor.
- Volkanlar, statik ısı kaynakları ve deniz üstü tespitler (`type` 1–3) çıkarıldı.
- Etiket: kümeye en yakın Aqua geçişinde (en fazla 60 dk fark) kümeye düşen MODIS tespiti sayısı.
- MODIS'in o bölgeye gerçekten baktığından emin olmak için, aynı geçişte kümeden 10–1,000 km uzakta başka Aqua tespitleri olmasını şart koştuk. Kümenin kendi üzerindeki tespitleri bu kanıta katmadık; aksi halde etiket, kümenin veriye girip girmemesini etkilerdi. Kanıtı olmayan kümeler kullanılmadı.
- Bağlam öznitelikleri arasında küme boyutu, FRP, parlaklık, güven sınıfı, bakış geometrisi, yerel saat, mevsim (yarıküreye göre), mutlak enlem ve aynı geçişte 25 km içindeki VIIRS piksel sayısı var.
- Bölge başına en fazla 50.000 küme. Eğitim 283.341, doğrulama 55.423, test 61.064 küme.

| Bölge | Biyom | Kullanım |
|---|---|---|
| Doğu Akdeniz (Türkiye, Yunanistan) | Akdeniz çalılık/orman | test |
| İber Yarımadası | Akdeniz çalılık/orman | eğitim |
| Kaliforniya | Akdeniz çalılık/orman | eğitim |
| Kanada boreal | Boreal orman | eğitim |
| Sibirya | Boreal orman | test |
| Amazon yay | Tropikal orman / ormansızlaşma | eğitim |
| Cerrado | Tropikal savan | eğitim |
| Orta Afrika (Angola, Zambiya, KDC) | Savan | eğitim |
| Batı Afrika / Sahel | Savan | eğitim |
| Güneydoğu Asya (Myanmar, Tayland, Laos) | Tropikal kuru orman / tarım | eğitim |
| Kuzey Avustralya | Savan | test |
| Hindistan / Pencap | Tarım yakmaları | eğitim |
| Doğu Avrupa bozkırı | Tarım / bozkır | eğitim |
| Meksika / Orta Amerika | Tropikal kuru orman | eğitim |
| Endonezya (Sumatra, Kalimantan) | Turbalık / tropikal orman | eğitim |

## Test sonuçları

Test bölgeleri değerlendirme modelinin eğitiminde hiç kullanılmadı. Sonuçlar 61.064 küme üzerinden:

| Model | Poisson sapması ↓ | MAE ↓ | Algılama AUC ↑ | Brier ↓ | %80 aralık kapsaması | Bölge-gün toplam hatası (medyan) ↓ |
|---|---|---|---|---|---|---|
| **FireA-v1** | 0,764 | 0,468 | 0,891 | 0,111 | %94,9 | %20,9 |
| Gradyan artırma (Poisson) | 0,788 | 0,478 | 0,889 | 0,113 | %93,8 | %22,5 |
| Güç yasası (μ = a·n^b) | 1,243 | 0,663 | 0,754 | 0,165 | %90,8 | %35,4 |

- FireA, güç yasasından açıkça daha iyi. Gradyan artırmadan da her ölçütte biraz daha iyi, ancak aradaki fark küçük.
- %80 aralığın kapsama oranı %94,9; yani aralıklar gerektiğinden biraz geniş.
- Bütün test kümelerinde FireA 35.426 MODIS tespiti öngördü, gerçekte 34.634 tespit vardı.

Uygulamada kullanılan model aynı ayarlarla yeniden eğitildi; bu kez Doğu Akdeniz (Türkiye, Yunanistan) dışındaki bütün bölgeler kullanıldı; toplam 389.974 küme. Doğu Akdeniz'i dışarıda bırakmamızın nedeni, Antalya–Muğla dahil bu bölgedeki kontrollerin bağımsız kalması.

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
