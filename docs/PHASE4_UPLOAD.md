# Phase 4 — YouTube upload

`upload.py` uploads a finished video via the YouTube Data API v3, using the SEO
sidecar (`output/shorts/<name>.json`) for title/description/tags.

**Safety:** uploads are **private by default** (§8). Public requires an explicit
`--privacy public`. No secrets live in code — OAuth uses `client_secret.json` +
`token.json`, both git-ignored.

---

## One-time setup (you do this — it needs your Google account)

I can't create Google credentials or click the consent screen for you. Steps:

1. Go to **https://console.cloud.google.com/** and create (or pick) a project.
2. **APIs & Services → Library →** search **"YouTube Data API v3" → Enable.**
3. **APIs & Services → OAuth consent screen:**
   - User type: **External**; fill app name + your email; **Save**.
   - **Audience → Test users → Add** your own Google (channel) email. (While the
     app is in "Testing", only test users can authorize — that's fine for you.)
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID:**
   - Application type: **Desktop app** → Create → **Download JSON**.
5. Save that file as **`automation/client_secret.json`** (exact name, or set
   `youtube.client_secret` in config.yaml).

That's it — you never paste secrets to me; the file stays local + git-ignored.

---

## First run (browser consent, once)

```bat
python pipeline/upload.py --input <clip>
```
The first time, it opens your browser → sign in → "Allow". It caches
`token.json` so future uploads are silent. (If Google warns the app is
unverified, that's expected for your own Testing app — proceed as the test user.)

---

## Usage

```bat
python pipeline/upload.py --input 2026-09-06_the-debt-trap-that-keeps-you-broke
:: -> uploads output/shorts/<name>.mp4 as PRIVATE, using the .json sidecar's
::    title / description / tags. Prints the video URL.

python pipeline/upload.py --input <clip> --privacy unlisted   # link-only
python pipeline/upload.py --input <clip> --privacy public     # explicit, deliberate
python pipeline/upload.py --file path/to/video.mp4 --title "..." --description "..."
```

Recommended flow: upload **private**, review in **YouTube Studio**, then flip to
public there when you're happy. Titles get ` #Shorts` appended
(`youtube.title_suffix`) so vertical <60 s videos are classified as Shorts.

## Quota note
The API has a daily quota; a video upload costs ~1600 units of the default
10,000/day (~6 uploads/day) unless you request more. Fine for 2 longs + 7
shorts a week.
