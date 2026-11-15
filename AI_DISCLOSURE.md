# AI Disclosure

**English** · [Türkçe](#türkçe)

Following the NASA Space Apps Challenge rules on Super Intelligence (SI) tools, this file explains where and how AI tools were used in this project.

## Tools used

| Tool | Provider | How we used it |
|---|---|---|
| Claude (via Claude Code) | Anthropic | Writing and changing code, running tests, writing documentation, producing the demo videos |
| ChatGPT | OpenAI | Brainstorming, debugging and refining project documentation |

## Where AI was used

| Part of the project | Role of AI |
|---|---|
| **Source code** (backend in `backend/app`, frontend in `frontend/src`) | Written jointly by our team and Claude. Team members wrote code themselves, and Claude generated code in line with the requirements and feedback we provided. |
| **FireA model code** (`backend/firea`: data download, dataset building, features, model, training) | Written jointly by our team and Claude. The training runs were done on our own computer. |
| **Tests** (`backend/tests`) and browser checks | Written and run by Claude. |
| **Documentation** (`README.md`, `README.tr.md`, model cards, this file) | Written by Claude and edited at our request. ChatGPT was also used to refine some of the wording. |
| **Demo videos** | Recorded and edited by Claude with an automated browser script: scene order, on-screen captions and title cards. They show real screenshots of the running app. |
| **Screenshots** in `docs/screenshots` | Captured automatically from the running app by a test script written by Claude. They are real screenshots, not generated images. |

## What the team did

- Chose the challenge and the goal of the project.
- Developed code directly, and tested and revised it in collaboration with the AI tools.
- Set the requirements and made the decisions, for example which features to build, how the results are presented, the model name, and keeping the project anonymous.
- Reviewed the results and asked for changes.
- Prepared and sent the submission.

## Data

- **No data in this project is AI-generated.** All fire data comes from NASA FIRMS (MODIS and VIIRS).
- The FireA-v1 model weights were trained with the code described above on real NASA FIRMS data.
- All numbers in the app, the README and the model card are calculated by the code from this data.

## Images, video and audio

- No AI image generation was used. There are no AI-generated pictures in the project.
- The demo videos were made with AI assistance, as described above.
- No AI-generated audio is included.

## NASA branding

The project does not use, contain or modify any NASA logos, flags, mission identifiers or other NASA design elements. "NASA FIRMS" is mentioned only as the source of the data.

---

## Türkçe

NASA Space Apps Challenge'ın yapay zekâ (SI) araçlarıyla ilgili kuralına uygun olarak, bu dosya projede yapay zekâ araçlarının nerede ve nasıl kullanıldığını açıklar.

**Kullanılan araçlar:** Claude (Anthropic, Claude Code üzerinden) kod yazma ve değiştirme, testleri çalıştırma, dokümantasyon yazma ve demo videolarını hazırlama için; ChatGPT (OpenAI) fikir geliştirme, hata ayıklama ve dokümantasyonu düzenleme için kullanıldı.

**Yapay zekânın kullanıldığı yerler:**
- Kaynak kod (backend, frontend) ekibimiz ve Claude tarafından birlikte yazıldı. Kodun geliştirilmesinde ekip üyelerimiz doğrudan görev aldı; Claude ise belirlediğimiz gereksinimler ve geri bildirimler doğrultusunda kod üretti.
- FireA modelinin kodu (veri indirme, veri kümesi, öznitelikler, model, eğitim) ekibimiz ve Claude tarafından birlikte yazıldı. Eğitimler kendi bilgisayarımızda yapıldı.
- Testler ve tarayıcı kontrolleri Claude tarafından yazıldı ve çalıştırıldı.
- README'ler, model kartları ve bu dosya Claude tarafından yazıldı ve isteğimize göre düzenlendi. Bazı ifadelerin düzenlenmesinde ChatGPT de kullanıldı.
- Demo videoları Claude tarafından otomatik bir tarayıcı betiğiyle kaydedildi ve kurgulandı (sahne sırası, ekran yazıları, başlık kartları). Videolarda çalışan uygulamanın gerçek görüntüleri yer alıyor.
- `docs/screenshots` içindeki ekran görüntüleri, Claude'un yazdığı bir test betiğiyle çalışan uygulamadan otomatik olarak alındı. Gerçek ekran görüntüleridir, üretilmiş resim değildir.

**Ekibin yaptıkları:** görevi ve projenin amacını seçtik; kodun geliştirilmesinde doğrudan görev aldık ve kodu yapay zekâ araçlarıyla birlikte test edip geliştirdik; gereksinimleri belirledik ve kararları verdik (hangi özelliklerin yapılacağı, sonuçların nasıl sunulacağı, modelin adı, projenin anonim tutulması gibi); sonuçları inceleyip değişiklik istedik; başvuruyu hazırlayıp gönderdik.

**Veri:** Projedeki hiçbir veri yapay zekâ ile üretilmedi. Bütün yangın verisi NASA FIRMS'ten (MODIS ve VIIRS) geliyor. FireA-v1 modeli yukarıdaki kodla gerçek NASA FIRMS verisi üzerinde eğitildi. Uygulamadaki, README'deki ve model kartındaki bütün sayılar bu veriden kodla hesaplandı.

**Görsel, video ve ses:** Yapay zekâ ile resim üretilmedi. Demo videoları yukarıda anlatıldığı gibi yapay zekâ yardımıyla hazırlandı. Yapay zekâ ile üretilmiş ses yok.

**NASA markası:** Projede NASA logosu, bayrağı, görev amblemi ya da başka bir NASA tasarım öğesi kullanılmadı ve değiştirilmedi. "NASA FIRMS" yalnızca veri kaynağı olarak anılıyor.
