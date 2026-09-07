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

İki platform desteklenir:
- **Windows** + NVIDIA GPU (bu proje **RTX 5060 / 8 GB**, Blackwell için ayarlandı) — kurulum: Bölüm 2.
- **macOS** (Apple Silicon önerilir) — kurulum: **Bölüm 2-B**.

Her iki platformda ortak:
- **Python 3.11**
- **Ollama** (yerel LLM) — https://ollama.com
- **ffmpeg** (PATH'te olmalı) — https://ffmpeg.org

Kontrol:
```bash
python --version
ollama --version
ffmpeg -version
```

---

## 2. Kurulum — Windows (NVIDIA / Blackwell; sırayı bozmayın — torch önemli)

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

---

## 2-B. Kurulum — macOS (Apple Silicon / Intel)

macOS'ta **CUDA yoktur**. Shorts + Reddit hikâye hattı (Ollama + Kokoro TTS + faster-whisper
+ ffmpeg) Mac'te sorunsuz çalışır; uzun-form 16:9 görsel üretimiyle ilgili uyarılar aşağıda.

**1) Homebrew ile araçlar:**
```bash
brew install python@3.11 ffmpeg espeak-ng
brew install ollama            # ya da https://ollama.com uygulaması
brew services start ollama     # Ollama'yı arka planda başlat
```

**2) Sanal ortam + bağımlılıklar** (`automation/` klasöründe):
```bash
# Sanal ortam (Windows'tan farkı: source ... activate)
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip

# PyTorch — Mac'te cu128 KULLANMAYIN. Standart derleme Apple Silicon'da Metal (MPS) kullanır.
pip install torch torchaudio

# Kalan bağımlılıklar
pip install -r requirements.txt

# Doğrulama (Apple Silicon'da True olmalı; Intel Mac'te False + CPU kullanılır)
python -c "import torch; print('MPS:', torch.backends.mps.is_available())"
```

**3) Cihaz ayarları — `config.yaml` ve `config.reddit.yaml` içinde CUDA→CPU/MPS:**
- `tts.kokoro.device: "cpu"`  (Apple Silicon'da `"mps"` denenebilir; sorun olursa `"cpu"`)
- `tts.chatterbox.device: "cpu"`
- `brainrot.whisper_device: "cpu"`  (faster-whisper Mac'te CPU'da çalışır — CUDA yok)

**4) LLM:** Ollama Apple Silicon'da Metal ile hızlıdır. `qwen2.5:14b` ~16 GB+ RAM ister;
16 GB'tan az Mac'lerde birincil olarak `llama3.1:8b` kullanın (`config → llm.primary`).

**⚠️ Uzun-form 16:9 korku hattı (SDXL/SVD) hakkında:**
- Görsel üretimi (`pipeline/imagegen.py`) NVIDIA/CUDA için yazıldı — `enable_model_cpu_offload()`
  kullanır. Mac'te çalıştırmak için bu satırı `pipe.to("mps")` ile değiştirmek gerekir; Apple
  Silicon'da **yavaş ve bellek yoğundur**, SVD "hero-shot" hareketleri pratik olmayabilir.
  Intel Mac'lerde önerilmez. **Shorts + Reddit hattı Mac'te tam çalışır** — uzun-form için
  güçlü bir NVIDIA GPU (veya bulut) tavsiye edilir.
- Yazı tipleri: kart/başlık fontları artık çok platformlu bulunuyor (macOS'ta
  `/System/Library/Fonts/Supplemental/Arial*.ttf`). Ekstra ayar gerekmez.

**5) Komutlar:** Windows `\` yerine Mac'te `/` kullanın, örn:
`python pipeline/brainrot.py --topic "..."`  ·  `python pipeline/redditstory.py --auto --config config.reddit.yaml`

---

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
