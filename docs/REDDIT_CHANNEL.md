# Second channel — Reddit Stories

Narrated Reddit stories (AITA, TIFU, nosleep, …) over gameplay, with a Reddit
post-card intro and karaoke captions. Runs from the same repo as the finance
channel via a separate profile: **`--config config.reddit.yaml`**.

Pipeline: fetch top posts (RSS) → LLM picks N SFW stories (one fetch) → **faithful**
narration script (never shortened/altered) → safety check → split into ~90 s parts →
Kokoro voice, one locked per story, slow + paced with inter-sentence pauses → per-word
captions → gameplay (varied per part) + Reddit card (title read, every part) + spoken
**teaser hook** (Part 1) + **Subscribe/🔔 CTA card** near the end → background music
(ducked under the voice) + whoosh/ding SFX + **−14 LUFS loudness** → SEO sidecar →
optional private upload.

## Reused code
`reddit_fetch.py` (+ `reddit_card.py`, `redditstory.py`) reuse `brainrot.py`
(captions/gameplay/upload), `generate.py` (`produce_script(fmt="story")`,
`produce_metadata`), `speak.py` (Kokoro), and `_common.py` (voice rotation).

---

## Setup: none (public RSS)
The fetcher reads Reddit's **public RSS/Atom feeds**
(`https://www.reddit.com/r/<sub>/top/.rss`) — **no API app, no client id/secret, no
OAuth.** RSS isn't rate-blocked the way the anonymous `.json` endpoint is.

Just set a descriptive contact in `config.reddit.yaml → reddit.user_agent`
(e.g. `windows:AIPresenter.RedditStories:v1.0 (by /u/yourname)`).

**Trade-offs vs. the API:** RSS gives title, author, permalink and body, but **not**
score / comment counts — so `min_score` is ignored (the `/top` feed is already popular)
and the post card omits the vote/comment numbers. Reddit rate-limits rapid bursts
(HTTP 429); `reddit.request_pause` spaces out the per-subreddit requests and the
fetcher retries with backoff. One good subreddit is enough to run.

> Want the richer API path later (scores, NSFW flags, fetch-by-id)? It needs a
> **script app** at https://www.reddit.com/prefs/apps and now Reddit's manual API
> approval. That's deferred; RSS is the default.

## Usage
```bat
:: verify fetching works
python pipeline\reddit_fetch.py --list --config config.reddit.yaml

:: one story end to end (no upload)
python pipeline\redditstory.py --auto --config config.reddit.yaml

:: a specific post id, or a batch of N stories, with private upload
python pipeline\redditstory.py --id <postid> --config config.reddit.yaml
python pipeline\redditstory.py --auto --count 3 --upload --config config.reddit.yaml
```
Outputs: `output/shorts/<slug>[_pN].mp4` + `.json`/`.txt` SEO sidecars. Used post
ids are logged to `content/stories/used.txt` (dedupe). Test without creds by dropping
a `content/stories/<id>.json` record and running `--id <id>`.

## 2nd YouTube channel
`config.reddit.yaml → youtube.token_file = token_reddit.json` and
`client_secret = client_secret_reddit.json`. Create that channel's own Desktop OAuth
client (see `docs/PHASE4_UPLOAD.md`), authorize it once, then `--upload` posts there
(private by default). Uploads never go public automatically.

## Audio production (music + SFX + loudness)
- **Background music:** drop royalty-free loops in `content/music/` (any `.mp3`/`.wav`).
  One is picked at random per part and **ducked** under the voice (sidechain compressor).
  Missing = silently skipped. Level/duck in `config.reddit.yaml → audio` (`music_volume`,
  `duck_threshold`, `duck_ratio`). *(git-ignored — supply your own licensed tracks.)*
- **SFX:** `content/sfx/whoosh.*` plays on the title card, `content/sfx/ding.*` on the CTA
  card. Default `whoosh.wav`/`ding.wav` ship with the repo — replace them to taste.
- **Loudness:** every video is normalized to **−14 LUFS** (`audio.loudness_i`), YouTube's
  target, so volume is consistent across uploads.

## Tuning
- **Pace:** `tts.kokoro.speed` (0.92) + `tts.kokoro.segment_gap_ms` (180 ms pause between
  sentences). Slower/again clearer = lower `speed` / higher gap.
- **Part length:** `reddit.max_seconds_per_part` (90) + `reddit.wpm` (~175, matches 0.92).
  If parts run long, lower `wpm`; the splitter balances parts evenly at sentence boundaries.
- **Teaser hook:** `reddit.teaser` (spoken curiosity line before the title on Part 1; the
  LLM writes it, `brand.teasers` are fallbacks). **CTA card:** `reddit.cta_seconds` (how
  long the Subscribe/🔔 card shows at the end; art in `reddit_card.render_cta_card`).
- **Card:** timed automatically to the spoken title (whisper word timings);
  `reddit.card_intro_seconds` is the fallback. **Voices:** `tts.kokoro.voices` (locked per
  story). **Captions:** `brainrot` block. **Subreddits/filters:** `reddit` block.
- **Safety:** `reddit.moderate` runs an LLM check on each story; results are saved in the
  sidecar `moderation` field and a flagged story is **skipped on `--upload`** (kept locally).
- **Faithful narration:** stories are never summarized/shortened — see the `fmt="story"`
  prompt in `generate.py`. Don't reintroduce a word target.
- **Batching:** `--count N` fetches ONCE and the LLM picks N varied stories (fewer 429s).

## Mood profiles (per subreddit)
`config.reddit.yaml → moods` maps each subreddit to a **mood** (horror/drama/comedy) that
sets the **voice + pace**, and picks **gameplay + music** from a mood subfolder when present:
`content/gameplay/<mood>/` and `content/music/<mood>/` are used first, else the flat folder.
E.g. nosleep → horror (am_michael, 0.88); AITA → drama; tifu → comedy. Edit `moods.profiles`
to taste.

## On-screen extras
- **On-screen hook** (`reddit.onscreen_hook`): the spoken teaser is also burned big on-screen
  for the first `hook_seconds`, then the **animated Reddit card** slides in during the title read.
- **Progress pill** (`reddit.progress_pill`): a "PART i/N" badge (multi-part only).
- **Caption emphasis** (`brainrot.emphasis`): dramatic words (from `emphasis_words`, ALL-CAPS,
  or ending "!") are enlarged + recolored.
- **SFX**: `content/sfx/intro.wav` (start), `ding.wav` (CTA), `outro.wav` (end), `whoosh.wav`
  (start fallback). Defaults ship in the repo; replace to taste.

## Auto-playlists + series linking (on upload)
With `youtube.make_playlists: true`, a multi-part story is grouped into one **playlist** and
each part's description is **cross-linked** to the next part as they upload. Needs the broader
`youtube` OAuth scope (the reddit token requests it automatically; finance stays upload-only).
Upload state is tracked in `output/shorts/.uploads.json`.

## Daily autopilot + weekly compilation
- **Scheduler:** `run_daily.bat [count]` produces N stories (logs to `logs/daily.log`). Register
  it with Task Scheduler, e.g. daily at 09:00:
  ```bat
  schtasks /Create /TN "RedditShorts" /TR "C:\Users\dursu\AIPresenter\automation\run_daily.bat 3" /SC DAILY /ST 09:00
  ```
  Add `--upload` inside the .bat once the channel's OAuth is wired (uploads stay private).
- **Weekly compilation:** `python pipeline/compile_weekly.py --days 7 --config config.reddit.yaml`
  stitches the week's reddit shorts (multi-part stories kept in order) into one long-form video
  in `output/compilations/` — extra watch time on top of the Shorts.

## Content safety
`over_18`/stickied filtered; the selection LLM avoids graphic/hateful/PII content.
Uploads are **private by default** — review in YouTube Studio before publishing.

---

## FUTURE — Turkish version (3rd pipeline, not built)
Plan: `config.reddit.tr.yaml` + an LLM **translation** step (English story → Turkish)
→ **Chatterbox multilingual** TTS (Kokoro has no Turkish) → a **multilingual whisper**
model for Turkish caption timing. Slower than the English path; separate channel/token.
