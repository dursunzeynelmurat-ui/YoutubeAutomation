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

from _common import get_path, load_config, setup_logging

log = setup_logging()

_ATOM = "{http://www.w3.org/2005/Atom}"


def _used_ids(config) -> set:
    f = get_path(config, "stories") / "used.txt"
    if f.exists():
        return {ln.strip() for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()}
    return set()


def mark_used(config, post_id: str):
    d = get_path(config, "stories"); d.mkdir(parents=True, exist_ok=True)
    with open(d / "used.txt", "a", encoding="utf-8") as fh:
        fh.write(post_id + "\n")


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


def _get_feed(url: str, ua: dict, retries: int = 4) -> str:
    """GET an RSS feed, backing off on Reddit's 429 rate-limiting."""
    delay = 3.0
    for attempt in range(retries):
        resp = requests.get(url, headers=ua, timeout=20)
        if resp.status_code == 429:
            wait = float(resp.headers.get("retry-after", delay))
            log.info("rate-limited (429); waiting %.0fs then retrying...", wait)
            time.sleep(wait)
            delay *= 2
            continue
        resp.raise_for_status()
        return resp.text
    resp.raise_for_status()
    return resp.text


def fetch_candidates(config) -> list[dict]:
    r = config["reddit"]
    ua = {"User-Agent": r.get("user_agent", "AIPresenter/1.0 by u/yourname"),
          "Accept": "application/atom+xml, application/xml, text/xml"}
    pause = float(r.get("request_pause", 2.5))   # be polite between subreddits
    used = _used_ids(config)
    out = []
    for idx, sub in enumerate(r["subreddits"]):
        if idx > 0:
            time.sleep(pause)
        url = (f"https://www.reddit.com/r/{sub}/top/.rss"
               f"?t={r.get('time','week')}&limit={r.get('limit',40)}")
        try:
            children = _parse_feed(_get_feed(url, ua), sub)
        except Exception as exc:  # noqa: BLE001
            log.warning("fetch failed for r/%s: %s", sub, exc)
            continue
        for d in children:
            body = d["selftext"]
            # RSS gives no score/over_18/stickied flags; rely on the /top feed being
            # already popular + the LLM SFW selection. Only length + dedupe filter here.
            if (not body or d["id"] in used
                    or not (r.get("min_chars", 0) <= len(body) <= r.get("max_chars", 99999))):
                continue
            out.append(d)
    log.info("fetched %d usable candidates from %d subreddits (RSS)", len(out), len(r["subreddits"]))
    return out


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
    ap.add_argument("--config", help="path to config (use config.reddit.yaml)")
    args = ap.parse_args()
    config = load_config(args.config)

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
