#!/usr/bin/env python3
"""reddit_fetch.py — fetch top Reddit posts + LLM story selection (2nd channel).

Uses Reddit's public **RSS/Atom feeds** (https://www.reddit.com/r/<sub>/top/.rss) —
no API app, no client id/secret, no OAuth. RSS is not rate-blocked the way the
anonymous .json endpoint is. It gives the post title, author, permalink and body,
but NOT score / comment counts (those aren't in the feed), so score-based filtering
is skipped and the post card omits the vote/comment numbers when unknown.

Filters to usable text posts, skips already-used ones (content/stories/used.txt),
and lets a local LLM pick the most engaging, self-contained, SFW story for a short.

Usage:
    python pipeline/reddit_fetch.py --list   --config config.reddit.yaml
    python pipeline/reddit_fetch.py --select --config config.reddit.yaml
"""
from __future__ import annotations

import argparse
import html as _html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

from _common import get_path, load_config, resolve, setup_logging

log = setup_logging()

_ATOM = "{http://www.w3.org/2005/Atom}"


def _used_rows(config) -> list[tuple[str, str]]:
    """used.txt rows: 'id' or 'id\\ttitle' (title added since the dedupe upgrade)."""
    f = get_path(config, "stories") / "used.txt"
    rows = []
    if f.exists():
        for ln in f.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln:
                parts = ln.split("\t", 1)
                rows.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return rows


def _used_ids(config) -> set:
    return {i for i, _ in _used_rows(config)}


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (t or "").lower()).strip()


def _is_near_dup(title: str, used_titles: list[str], thresh: float = 0.82) -> bool:
    import difflib
    n = _norm_title(title)
    if not n:
        return False
    return any(difflib.SequenceMatcher(None, n, u).ratio() >= thresh for u in used_titles if u)


def mark_used(config, post_id: str, title: str = ""):
    d = get_path(config, "stories"); d.mkdir(parents=True, exist_ok=True)
    with open(d / "used.txt", "a", encoding="utf-8") as fh:
        fh.write(f"{post_id}\t{_norm_title(title)}\n" if title else f"{post_id}\n")


def _extract_selftext(content_html: str) -> str:
    """Turn a Reddit RSS entry's <content> HTML into plain narration text.

    Reddit wraps the body in <!-- SC_OFF --><div class="md">…</div><!-- SC_ON -->
    and appends a 'submitted by /u/… to r/…' footer with links; strip both."""
    s = content_html or ""
    for marker in ("<!-- SC_ON -->", "submitted by"):
        i = s.find(marker)
        if i != -1:
            s = s[:i]
            break
    s = re.sub(r"(?is)<!--.*?-->", " ", s)
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</p\s*>", "\n\n", s)
    s = re.sub(r"(?is)<.*?>", " ", s)      # drop remaining tags
    s = _html.unescape(s)                   # any leftover entities
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _parse_feed(xml_text: str, sub: str) -> list[dict]:
    posts = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        log.warning("RSS parse failed for r/%s: %s", sub, exc)
        return posts
    for e in root.findall(f"{_ATOM}entry"):
        raw_id = (e.findtext(f"{_ATOM}id") or "").strip()        # e.g. t3_abc123
        pid = raw_id.split("_", 1)[1] if "_" in raw_id else raw_id
        title = _html.unescape((e.findtext(f"{_ATOM}title") or "").strip())
        author = "user"
        a = e.find(f"{_ATOM}author/{_ATOM}name")
        if a is not None and a.text:
            author = a.text.strip().lstrip("/").removeprefix("u/") or "user"
        link_el = e.find(f"{_ATOM}link")
        permalink = link_el.get("href") if link_el is not None else ""
        # subreddit from <category term="..."> when present, else the requested sub
        cat = e.find(f"{_ATOM}category")
        subreddit = (cat.get("label") or cat.get("term")).lstrip("r/") if cat is not None and (cat.get("label") or cat.get("term")) else sub
        body = _extract_selftext(e.findtext(f"{_ATOM}content") or "")
        posts.append({"id": pid, "subreddit": subreddit or sub, "title": title,
                      "author": author, "selftext": body,
                      "score": None, "num_comments": None, "permalink": permalink})
    return posts


def _get_feed(url: str, ua: dict, retries: int = 5) -> str:
    """GET an RSS feed, backing off (with jitter) on Reddit's 429 rate-limiting."""
    import random
    delay = 3.0
    for attempt in range(retries):
        resp = requests.get(url, headers=ua, timeout=20)
        if resp.status_code == 429:
            wait = float(resp.headers.get("retry-after", delay)) + random.uniform(0, 2.5)
            log.info("rate-limited (429); waiting %.1fs then retrying...", wait)
            time.sleep(wait)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp.text
    resp.raise_for_status()
    return resp.text


def _cached_feed(config, sub: str, url: str, ua: dict) -> str:
    """Serve a subreddit feed from a short-lived on-disk cache to dodge 429s on re-runs."""
    ttl = float(config["reddit"].get("cache_minutes", 30)) * 60
    cdir = resolve(".state") / "rss_cache"
    cdir.mkdir(parents=True, exist_ok=True)
    cf = cdir / f"{sub}.xml"
    if ttl > 0 and cf.exists() and (time.time() - cf.stat().st_mtime) < ttl:
        log.info("using cached feed for r/%s (< %.0f min old)", sub, ttl / 60)
        return cf.read_text(encoding="utf-8")
    text = _get_feed(url, ua)
    try:
        cf.write_text(text, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return text


def fetch_candidates(config) -> list[dict]:
    r = config["reddit"]
    ua = {"User-Agent": r.get("user_agent", "AIPresenter/1.0 by u/yourname"),
          "Accept": "application/atom+xml, application/xml, text/xml"}
    pause = float(r.get("request_pause", 2.5))   # be polite between subreddits
    sort = r.get("sort", "top")                  # top | rising | hot | new
    used = _used_ids(config)
    used_titles = [t for _, t in _used_rows(config) if t]
    out, dropped_dup = [], 0
    for idx, sub in enumerate(r["subreddits"]):
        if idx > 0:
            time.sleep(pause)
        if sort == "rising":
            url = f"https://www.reddit.com/r/{sub}/rising/.rss?limit={r.get('limit',40)}"
        elif sort in ("hot", "new"):
            url = f"https://www.reddit.com/r/{sub}/{sort}/.rss?limit={r.get('limit',40)}"
        else:
            url = f"https://www.reddit.com/r/{sub}/top/.rss?t={r.get('time','week')}&limit={r.get('limit',40)}"
        try:
            children = _parse_feed(_cached_feed(config, sub, url, ua), sub)
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch failed for r/%s: %s", sub, exc)
            continue
        for d in children:
            body = d["selftext"]
            if (not body or d["id"] in used
                    or not (r.get("min_chars", 0) <= len(body) <= r.get("max_chars", 99999))):
                continue
            if _is_near_dup(d["title"], used_titles):    # skip re-telling the same story
                dropped_dup += 1
                continue
            out.append(d)
    log.info("fetched %d usable candidates from %d subreddits (RSS, sort=%s%s)",
             len(out), len(r["subreddits"]), sort,
             f", {dropped_dup} near-dup skipped" if dropped_dup else "")
    return out


_ID_PATTERNS = [
    re.compile(r"/comments/([a-z0-9]{4,10})", re.I),   # .../comments/abc123/title/
    re.compile(r"redd\.it/([a-z0-9]{4,10})", re.I),     # https://redd.it/abc123  (share link)
    re.compile(r"[?&]comment=|t3_([a-z0-9]{4,10})", re.I),
]


def _post_id_from_url(s: str) -> str | None:
    """Pull the base-36 post id out of any Reddit link form: full permalink, old./new.,
    redd.it share link, redditmedia embed link, or a bare id/t3_ fullname."""
    s = (s or "").strip()
    if not s:
        return None
    bare = s.split("_", 1)[1] if s.lower().startswith("t3_") else s
    if re.fullmatch(r"[a-z0-9]{4,10}", bare, re.I):      # already just an id
        return bare
    for pat in _ID_PATTERNS:
        m = pat.search(s)
        if m and m.lastindex:
            return m.group(1)
    return None


def fetch_full_story(config, url_or_id: str) -> dict | None:
    """Download the COMPLETE, untruncated story for one post via Reddit's public JSON
    endpoint (https://www.reddit.com/comments/<id>.json). RSS <content> can clip very
    long posts; this returns the whole selftext so narration is never shortened.

    Accepts a full permalink, a redd.it/redditmedia embed or share link, a t3_ fullname,
    or a bare post id. Returns a story record (same shape as fetch_candidates entries)."""
    pid = _post_id_from_url(url_or_id)
    if not pid:
        log.warning("could not find a Reddit post id in %r", url_or_id)
        return None
    r = config.get("reddit", {})
    ua = {"User-Agent": r.get("user_agent", "AIPresenter/1.0 by u/yourname"),
          "Accept": "application/json"}
    url = f"https://www.reddit.com/comments/{pid}.json?raw_json=1&limit=1"
    import random
    delay = 3.0
    for attempt in range(5):
        try:
            resp = requests.get(url, headers=ua, timeout=25)
        except Exception as exc:  # noqa: BLE001
            log.warning("request failed (%s) for %s", exc, url)
            return None
        if resp.status_code == 429:
            wait = float(resp.headers.get("retry-after", delay)) + random.uniform(0, 2.5)
            log.info("rate-limited (429); waiting %.1fs then retrying...", wait)
            time.sleep(wait); delay *= 2
            continue
        if resp.status_code in (403, 404):
            log.warning("Reddit returned %d for post %s (blocked/removed?).", resp.status_code, pid)
            return None
        resp.raise_for_status()
        break
    else:
        return None
    try:
        listing = resp.json()
        data = listing[0]["data"]["children"][0]["data"]
    except Exception as exc:  # noqa: BLE001
        log.warning("unexpected JSON shape for post %s: %s", pid, exc)
        return None
    body = _html.unescape(data.get("selftext") or "").strip()
    if not body:
        log.warning("post %s has no selftext (link/image/video post?).", pid)
        return None
    story = {
        "id": pid,
        "subreddit": data.get("subreddit") or r.get("subreddits", ["nosleep"])[0],
        "title": _html.unescape(data.get("title") or "").strip(),
        "author": data.get("author") or "user",
        "selftext": body,
        "score": data.get("score"),
        "num_comments": data.get("num_comments"),
        "permalink": "https://www.reddit.com" + (data.get("permalink") or ""),
    }
    log.info("downloaded full story r/%s: %s (%d words)",
             story["subreddit"], story["title"][:70], len(body.split()))
    return story


def select_story(config, candidates: list[dict]) -> dict | None:
    if not candidates:
        return None
    llm = config["llm"]
    shortlist = candidates[:20]
    listing = "\n".join(
        f"[{i}] r/{p['subreddit']}: {p['title']}  — {p['selftext'][:200]}"
        for i, p in enumerate(shortlist))
    system = ("You pick the single best Reddit story to narrate as a short video. Choose one that is "
              "engaging, self-contained, has a clear arc/twist, and is SFW (no graphic violence, sexual "
              "content, slurs, or personal identifying info). Return ONLY JSON: {\"pick\": <index>, "
              "\"reason\": \"...\"}.")
    try:
        resp = requests.post(f"{llm['ollama_host']}/api/chat", timeout=llm.get("request_timeout", 600),
                             json={"model": llm.get("seo_model", llm["fallback"]), "stream": False,
                                   "format": "json", "keep_alive": llm.get("keep_alive", 0),
                                   "messages": [{"role": "system", "content": system},
                                                {"role": "user", "content": listing}]})
        resp.raise_for_status()
        data = json.loads(re.search(r"\{.*\}", resp.json()["message"]["content"], re.DOTALL).group(0))
        pick = int(data.get("pick", 0))
        log.info("LLM picked [%d]: %s", pick, data.get("reason", ""))
        return shortlist[pick] if 0 <= pick < len(shortlist) else shortlist[0]
    except Exception as exc:  # noqa: BLE001
        log.warning("LLM selection failed (%s) — using top-of-feed.", exc)
        return shortlist[0]


def select_stories(config, candidates: list[dict], n: int = 1) -> list[dict]:
    """Pick up to n distinct stories from ONE candidate pool (no re-fetch per story).
    Asks the LLM for a ranked, varied set; falls back to feed order. Skips used ids."""
    if n <= 1:
        s = select_story(config, candidates)
        return [s] if s else []
    if not candidates:
        return []
    llm = config["llm"]
    shortlist = candidates[:30]
    listing = "\n".join(
        f"[{i}] r/{p['subreddit']}: {p['title']}  — {p['selftext'][:160]}"
        for i, p in enumerate(shortlist))
    system = (f"You are choosing the {n} best Reddit stories to narrate as SEPARATE short videos. Pick "
              f"engaging, self-contained, SFW stories with a clear arc or twist, and prefer VARIETY across "
              f"subreddits and themes. Return ONLY JSON: {{\"picks\": [<index>, ...]}} with {n} distinct "
              f"indices, best first.")
    try:
        resp = requests.post(f"{llm['ollama_host']}/api/chat", timeout=llm.get("request_timeout", 600),
                             json={"model": llm.get("seo_model", llm["fallback"]), "stream": False,
                                   "format": "json", "keep_alive": llm.get("keep_alive", 0),
                                   "messages": [{"role": "system", "content": system},
                                                {"role": "user", "content": listing}]})
        resp.raise_for_status()
        data = json.loads(re.search(r"\{.*\}", resp.json()["message"]["content"], re.DOTALL).group(0))
        idxs = [int(i) for i in (data.get("picks") or []) if isinstance(i, (int, float))]
    except Exception as exc:  # noqa: BLE001
        log.warning("multi-select failed (%s) — using feed order.", exc)
        idxs = []
    picked, seen = [], set()
    for i in idxs:
        if 0 <= i < len(shortlist) and i not in seen:
            picked.append(shortlist[i]); seen.add(i)
        if len(picked) >= n:
            break
    for j, p in enumerate(shortlist):        # pad if the LLM returned too few
        if len(picked) >= n:
            break
        if j not in seen:
            picked.append(p); seen.add(j)
    picked = picked[:n]
    # Enforce subreddit spread: if every pick is from ONE subreddit but the pool has
    # others, swap the last pick for the best candidate from a different subreddit.
    subs_all = {p["subreddit"].lower() for p in shortlist}
    if n >= 2 and len({p["subreddit"].lower() for p in picked}) == 1 and len(subs_all) > 1:
        have = picked[0]["subreddit"].lower()
        alt = next((p for p in shortlist if p["subreddit"].lower() != have
                    and p["id"] not in {x["id"] for x in picked}), None)
        if alt:
            picked[-1] = alt
            log.info("diversity: swapped in r/%s for subreddit spread", alt["subreddit"])
    log.info("selected %d story(ies) from %d candidates (subs: %s)",
             len(picked), len(candidates), ", ".join(sorted({p["subreddit"] for p in picked})))
    return picked[:n]


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch + select Reddit stories (RSS, no API keys).")
    ap.add_argument("--list", action="store_true", help="list candidate stories")
    ap.add_argument("--select", action="store_true", help="pick one and save its record")
    ap.add_argument("--url", help="download ONE full story from a Reddit link/embed link/id and save it")
    ap.add_argument("--id", help="optional id/filename to save under when using --url")
    ap.add_argument("--config", help="path to config (use config.reddit.yaml)")
    args = ap.parse_args()
    config = load_config(args.config)

    if args.url:                                     # download the complete story from a link
        story = fetch_full_story(config, args.url)
        if not story:
            sys.exit("[fatal] could not download a story from that link.")
        if args.id:
            story["id"] = args.id
        d = get_path(config, "stories"); d.mkdir(parents=True, exist_ok=True)
        out = d / f"{story['id']}.json"
        out.write_text(json.dumps(story, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("saved full story: %s", out)
        log.info("render it:")
        log.info("  9:16 shorts :  python pipeline/redditstory.py --id %s --config %s", story["id"], args.config or "config.reddit.yaml")
        log.info("  16:9 long   :  python pipeline/longform.py   --id %s --config %s", story["id"], args.config or "config.reddit.yaml")
        return

    cands = fetch_candidates(config)
    if args.list:
        for p in cands[:20]:
            log.info("[%s] r/%s  — %s", p["id"], p["subreddit"], p["title"][:80])
        return
    story = select_story(config, cands)
    if not story:
        sys.exit("[fatal] no story selected (no candidates).")
    d = get_path(config, "stories"); d.mkdir(parents=True, exist_ok=True)
    (d / f"{story['id']}.json").write_text(json.dumps(story, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("selected r/%s: %s", story["subreddit"], story["title"])
    log.info("saved: %s", d / f"{story['id']}.json")


if __name__ == "__main__":
    main()
