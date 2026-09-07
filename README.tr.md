# YouTube Otomasyon — Kurulum Kılavuzu (Türkçe)

Tamamen **yerel** çalışan bir finans içerik hattı. İki ürün var:

- **Finans Brainrot Shorts (hızlı):** konu → senaryo (yerel LLM) → seslendirme
  (TTS) → kelime kelime altyazı + oyun görüntüsü arka plan → 1080×1920 dikey video →
  SEO başlık/açıklama → (isteğe bağlı) YouTube'a **gizli** yükleme.
- **Reddit Hikâye Shorts:** AITA/nosleep/TIFU hikâyeleri; Reddit kartı, ruh hâli (mood),
  çok parçalı bölme, Abone/🔔 kartı. Ayrıntılar: `docs/REDDIT_CHANNEL.md`
  (`--config config.reddit.yaml`).

> Her şey yerelde çalışır. İnternet yalnızca ilk kurulumdaki model indirmeleri ve
> YouTube yüklemesi için gerekir.

---

## 1. Gereksinimler

- **Windows**, NVIDIA GPU (bu proje **RTX 5060 / 8 GB**, Blackwell için ayarlandı)
- **Python 3.11**
- **Ollama** (yerel LLM) — https://ollama.com
- **ffmpeg** (PATH'te olmalı) — https://ffmpeg.org

Kontrol:
```bat
python --version
ollama --version
ffmpeg -version
```

---

## 2. Kurulum (sırayı bozmayın — Blackwell/torch önemli)

`automation/` klasöründe:

```bat
:: 1) Sanal ortam
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip

:: 2) PyTorch'u ÖNCE cu128 (CUDA 12.8) deposundan kurun (RTX 50xx / Blackwell şart)
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128

:: 3) Kalan bağımlılıklar
pip install -r requirements.txt

:: 4) Doğrulama — True yazmalı
python -c "import torch; print(torch.cuda.is_available())"
```
> Adım 4 `False` derse: bir TTS paketi torch'u düşürmüştür. Şununla düzeltin:
> `pip install --force-reinstall torch torchaudio --index-url https://download.pytorch.org/whl/cu128`

### LLM modelleri (Ollama)
```bat
ollama pull qwen2.5:14b-instruct   :: birincil
ollama pull llama3.1:8b            :: yedek + SEO (hızlı)
```

### Oyun görüntüleri (Shorts arka planı)
`content/gameplay/` klasörüne **telifsiz / kullanım hakkına sahip olduğunuz** oyun
videoları koyun (`.mp4/.mov/.mkv/.webm`). ⚠️ Telifli oyun görüntüsü (Subway Surfers,
GTA vb.) YouTube'da gelir kaybı/Content ID riski taşır. (Bu klasör de `.gitignore`'da.)

---

## 3. Kullanım — Brainrot Shorts

Tek komut (senaryo + ses + altyazı + video + SEO):
```bat
python pipeline\brainrot.py --topic "Bileşik faiz nasıl çalışır"
```
Toplu üretim (haftalık) — her satır bir konu:
```bat
python pipeline\brainrot.py --topics content\batch1.txt
```
Üretip **YouTube'a gizli** olarak yükle:
```bat
python pipeline\brainrot.py --topics content\batch1.txt --upload
```
Çıktılar: `output/shorts/<isim>.mp4` + yanında SEO için `.json` / `.txt`.

---

## 4. YouTube kurulumu (yükleme için, bir kez)

Ayrıntılı: `docs/PHASE4_UPLOAD.md`. Özet:
1. https://console.cloud.google.com → proje oluştur.
2. **YouTube Data API v3**'ü etkinleştir.
3. **OAuth consent screen** → External → kendi e-postanı **test user** olarak ekle.
4. **Credentials → OAuth client ID → Desktop app** → JSON'u indir.
5. Dosyayı `automation/client_secret.json` olarak kaydet.

İlk yüklemede tarayıcı açılır, izin verirsin; `token.json` önbelleğe alınır.
Yüklemeler **varsayılan olarak gizli** (public asla otomatik değil).
> `client_secret.json` ve `token.json` gizlidir ve `.gitignore` ile korunur — repoya girmez.

---

## 5. Ayarlar (`config.yaml`)

Her şey buradan yönetilir:
- `brand` — kanal adı, persona, ton, CTA, **affiliate linkleri**, kanca (hook) çeşitleri
- `tts.kokoro.voices` — sırayla dönen sesler (`am_adam`, `am_liam`), `speed`
- `brainrot` — altyazı fontu/boyutu/konumu, kelime sayısı
- `youtube` — gizlilik, kategori, etiketler

---

## 6. Güvenlik / yasal

- Senaryolar otomatik yayınlanmaz; her videoya "yatırım tavsiyesi değildir" ibaresi eklenir.
- Yüklemeler varsayılan **gizli**; herkese açık yapmak **elle** bir adımdır (`--privacy public`).
- Gizli anahtarlar koda gömülmez; yerel dosyalarda ve `.gitignore`'da tutulur.
- Yalnızca kullanım hakkına sahip olduğunuz oyun görüntülerini kullanın.

Ayrıntılı İngilizce dokümanlar: `README.md` ve `docs/`.
