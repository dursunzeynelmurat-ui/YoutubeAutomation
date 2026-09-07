#!/usr/bin/env python3
"""generate.py — Ollama -> finance script draft (Phase 1, content engine).

Reads a topic plus any real data files in content/data/, prompts a LOCAL Ollama
model, and writes a dated draft to content/scripts/ with a "not financial advice"
disclaimer prepended.

Safety (PIPELINE.md §8):
  * Feeds the model REAL DATA from content/data/ instead of relying on recall,
    and instructs it not to invent figures.
  * NEVER writes to content/approved/ — a human must review and move the draft
    there before it can be voiced. Nothing is auto-approved.
  * Every script carries the disclaimer.

VRAM (§2): the Ollama request sends keep_alive=0 so the model unloads right after
generation, freeing the GPU for the next stage.

Usage:
    python pipeline/generate.py --topic "Why index funds beat stock picking"
    python pipeline/generate.py --topic "Faiz kararı" --language tr
    python pipeline/generate.py --topic "..." --model aya-expanse:8b
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

import requests

from _common import get_path, load_config, resolve, rotate_pick, setup_logging

log = setup_logging()

DATA_EXTENSIONS = {".txt", ".md", ".csv", ".json"}


def slugify(text: str, max_len: int = 50) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    slug = re.sub(r"[\s_-]+", "-", slug)
    return slug[:max_len].strip("-") or "untitled"


def check_ollama(host: str) -> list[str]:
    """Return the list of installed model names, or exit if Ollama is unreachable."""
    try:
        resp = requests.get(f"{host}/api/tags", timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        sys.exit(
            f"[fatal] cannot reach Ollama at {host} ({exc}).\n"
            f"        Start it (the Ollama app / `ollama serve`) and retry."
        )
    return [m["name"] for m in resp.json().get("models", [])]


def pick_model(requested: str, fallback: str, installed: list[str]) -> str:
    """Prefer the requested model; fall back if it isn't installed."""
    def has(name: str) -> bool:
        # Ollama reports names like "qwen2.5:14b-instruct" or "...:latest".
        return name in installed or f"{name}:latest" in installed or any(
            m.split(":")[0] == name for m in installed
        )

    if has(requested):
        return requested
    if has(fallback):
        log.warning("model '%s' not installed — falling back to '%s'.", requested, fallback)
        log.warning("        Pull the primary with:  ollama pull %s", requested)
        return fallback
    sys.exit(
        f"[fatal] neither '{requested}' nor fallback '{fallback}' is installed.\n"
        f"        Installed: {', '.join(installed) or '(none)'}\n"
        f"        Pull one with:  ollama pull {requested}"
    )


def read_data_files(data_dir: Path) -> tuple[str, list[str]]:
    """Concatenate real-data files into a context block. Returns (block, filenames)."""
    if not data_dir.exists():
        return "", []
    files = sorted(p for p in data_dir.iterdir() if p.suffix.lower() in DATA_EXTENSIONS)
    chunks, names = [], []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            log.warning("could not read %s: %s", f.name, exc)
            continue
        if text:
            chunks.append(f"### SOURCE: {f.name}\n{text}")
            names.append(f.name)
    return "\n\n".join(chunks), names


def build_messages(topic: str, language: str, target_words: int,
                   data_block: str, data_names: list[str], fmt: str = "long",
                   brand: dict | None = None, hook: str = "") -> list[dict]:
    lang_name = {"en": "English", "tr": "Turkish"}.get(language, language)

    if data_block:
        data_section = (
            "You MUST base every figure, price, percentage, date, and factual claim "
            "ONLY on the REAL DATA below. Do not invent or recall numbers from memory. "
            "If a fact you want is not in the data, speak in general terms instead of "
            "stating a specific number. Refer to sources naturally when citing figures.\n\n"
            f"===== REAL DATA (from content/data/: {', '.join(data_names)}) =====\n"
            f"{data_block}\n"
            "===== END REAL DATA ====="
        )
    else:
        data_section = (
            "No real-data files were provided. Do NOT state specific prices, "
            "percentages, or dated figures — you have no verified source for them. "
            "Keep the script conceptual and educational, using only timeless, "
            "well-established principles."
        )

    if fmt == "short":
        brand = brand or {}
        persona = brand.get("persona", "a sharp, friendly money coach")
        tone = brand.get("tone", "energetic but credible")
        hook_line = f"- HOOK STYLE for this one: {hook}.\n" if hook else \
                    "- Open with a strong hook (bold claim or question).\n"
        system = (
            f"You are {persona}, writing a punchy short-form video script (YouTube Shorts / "
            f"TikTok) in {lang_name}, read over gameplay footage. Target ~{target_words} words "
            f"(~30-45s spoken).\n\n"
            f"Voice & tone: {tone}\n"
            f"Make it genuinely FUN to listen to — vivid, conversational, a touch of wit and "
            f"personality; short snappy sentences with rhythm. Never boring, never a lecture.\n\n"
            f"Rules:\n"
            f"{hook_line}"
            f"- One clear idea, building fast to a satisfying payoff. End with a punchy takeaway "
            f"that invites a comment or follow (don't say 'subscribe' robotically).\n"
            f"- Output ONLY the spoken words — no headings, hashtags, emojis, stage directions, "
            f"markdown, or labels.\n"
            f"- Accurate and genuinely useful; never give personalized buy/sell advice; no hype-only "
            f"filler or fake urgency.\n"
            f"- {data_section}"
        )
        user = f"Topic: {topic}\n\nWrite the short script now."
    elif fmt == "story":
        # Faithful narration ONLY. The LLM must not summarize, shorten, or alter the
        # story's content — it just converts the post into clean spoken-word text.
        # (Delivery drama comes from the TTS voice, not from rewording the content.)
        system = (
            f"You convert a Reddit post into clean spoken-word narration for a video, in {lang_name}. "
            f"You are a FAITHFUL NARRATOR, not an editor or summarizer.\n\n"
            f"ABSOLUTE RULES:\n"
            f"- Reproduce the ENTIRE post: every event, detail, name/initial, number, quote, and "
            f"edit/update, in the original order. Do NOT summarize, condense, shorten, skip, soften, "
            f"reorder, or add anything of your own.\n"
            f"- Keep the wording as close to the original as possible. You may ONLY: strip markdown/"
            f"formatting symbols, fix obvious typos, and expand text-speak so it reads aloud naturally "
            f"(e.g. 'AITA' -> 'Am I the asshole', 'WIBTA', 'OP', '28F' -> 'twenty-eight-year-old woman') "
            f"— without dropping any meaning.\n"
            f"- The narration must be essentially the SAME LENGTH as the original post. When unsure, "
            f"keep the original text.\n"
            f"- Output ONLY the spoken narration text — no headings, hashtags, emojis, 'Part 1' labels, "
            f"quotation marks around the whole thing, or any commentary about the task.\n"
        )
        user = (f"Reddit post to narrate faithfully — reproduce it in full, do NOT shorten or change "
                f"the content:\n{topic}\n\nWrite the full narration now.")
    else:
        system = (
            f"You are the script writer for a finance-education YouTube presenter. "
            f"Write a single spoken monologue in {lang_name} for one on-camera presenter "
            f"seated at a desk. Target about {target_words} words.\n\n"
            f"Rules:\n"
            f"- Output ONLY the spoken words — no headings, no stage directions, no "
            f"markdown, no bullet lists, no 'Presenter:' labels.\n"
            f"- Conversational, clear, engaging; short sentences that are easy to read aloud.\n"
            f"- Educational and balanced. Never give personalized investment advice or "
            f"tell viewers to buy or sell a specific asset.\n"
            f"- {data_section}"
        )
        user = f"Topic: {topic}\n\nWrite the full monologue now."
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def produce_script(config: dict, topic: str, fmt: str = "long", language: str | None = None,
                   model: str | None = None, data_dir: str | None = None):
    """Generate a script via Ollama and write a dated draft to content/scripts/.
    Returns (out_path, body). Reused by generate() and brainrot.py's one-shot mode."""
    llm = config["llm"]
    host = llm["ollama_host"]
    language = (language or config["content"]["language"]).lower()
    if language not in ("en", "tr"):
        sys.exit(f"[fatal] unsupported language '{language}' (use en or tr).")
    disclaimer = config["content"]["disclaimers"].get(language)
    if not disclaimer:
        sys.exit(f"[fatal] no disclaimer configured for language '{language}'.")

    installed = check_ollama(host)
    model = model or pick_model(llm["primary"], llm["fallback"], installed)
    ddir = resolve(data_dir) if data_dir else get_path(config, "data")
    data_block, data_names = read_data_files(ddir)
    if data_names:
        log.info("using %d real-data file(s): %s", len(data_names), ", ".join(data_names))
    elif fmt != "short":
        log.warning("no data files in %s — script will stay conceptual (no specific figures).", ddir)

    if fmt == "short":
        target = 80
    elif fmt == "story":
        target = config["content"].get("story_words", 320)
    else:
        target = config["content"]["target_words"]
    brand = config.get("brand", {})
    hook = ""
    if fmt in ("short", "story"):
        hooks = brand.get("hook_formats") or []
        hook = rotate_pick("hook_format", list(hooks)) if hooks else ""
        if hook:
            log.info("hook style: %s", hook)
    messages = build_messages(topic, language, target, data_block, data_names,
                              fmt=fmt, brand=brand, hook=hook)
    log.info("generating with '%s' (lang=%s, %s)…", model, language, fmt)
    # Faithful story narration must not be cut off: use a low temp (no creative drift),
    # a large context (fit the whole post + full narration), and an uncapped output.
    if fmt == "story":
        options = {"temperature": llm.get("story_temperature", 0.3),
                   "num_ctx": llm.get("story_num_ctx", 12288),
                   "num_predict": llm.get("story_num_predict", -1)}
    else:
        options = {"temperature": llm.get("temperature", 0.7),
                   "num_ctx": llm.get("num_ctx", 8192)}
    payload = {"model": model, "messages": messages, "stream": False,
               "keep_alive": llm.get("keep_alive", 0), "options": options}
    try:
        resp = requests.post(f"{host}/api/chat", json=payload, timeout=llm.get("request_timeout", 600))
        resp.raise_for_status()
    except requests.RequestException as exc:
        sys.exit(f"[fatal] Ollama generation failed: {exc}")
    body = resp.json().get("message", {}).get("content", "").strip()
    if not body:
        sys.exit("[fatal] model returned empty output.")

    document = (
        f"{disclaimer}\n\n---\n"
        f"<!-- topic: {topic} | model: {model} | language: {language} | format: {fmt} | "
        f"generated: {dt.datetime.now().isoformat(timespec='seconds')} | "
        f"data: {', '.join(data_names) or 'none'} -->\n\n{body}\n"
    )
    scripts_dir = get_path(config, "scripts")
    scripts_dir.mkdir(parents=True, exist_ok=True)
    date = dt.date.today().isoformat()
    out_path = scripts_dir / f"{date}_{slugify(topic)}.md"
    n = 2
    while out_path.exists():
        out_path = scripts_dir / f"{date}_{slugify(topic)}-{n}.md"
        n += 1
    out_path.write_text(document, encoding="utf-8")
    log.info("draft written: %s (~%d words)", out_path, len(body.split()))
    return out_path, body


def produce_metadata(config: dict, text: str, topic: str = "", language: str | None = None) -> dict:
    """Generate SEO title/description/hashtags/tags for a video from its script/transcript.
    Uses a fast local model; always appends the channel disclaimer to the description (§8).
    Returns {title, description, tags, hashtags}. Never raises — falls back on any error."""
    import json as _json
    import re as _re
    llm = config["llm"]
    host = llm["ollama_host"]
    language = (language or config["content"]["language"]).lower()
    disclaimer = config["content"]["disclaimers"].get(language, "")

    profile = config.get("content", {}).get("seo_profile", "finance")
    default_tags = {"story": ["#shorts", "#reddit", "#redditstories", "#storytime"]}.get(
        profile, ["#shorts", "#finance", "#money"])
    default_title = "Reddit Story" if profile == "story" else "Finance Short"

    def _fallback():
        t = (topic or text[:60]).strip()[:90] or default_title
        desc = (text.strip().split(". ")[0][:180] + ".") if text.strip() else t
        return {"title": t, "description": (desc + "\n\n" + disclaimer).strip(),
                "tags": [], "hashtags": default_tags}

    try:
        installed = check_ollama(host)
        model = pick_model(llm.get("seo_model", llm["fallback"]), llm["fallback"], installed)
        lang_name = {"en": "English", "tr": "Turkish"}.get(language, language)
        if profile == "story":
            system = (
                f"You are a YouTube Shorts SEO expert for a Reddit-storytelling channel. In {lang_name}, "
                f"return ONLY a JSON object with keys title, description, hashtags, tags.\n"
                f"- title: <=90 chars, a strong curiosity hook that teases the drama or dilemma WITHOUT "
                f"spoiling the ending; natural, not spammy.\n"
                f"- description: 2-3 natural sentences that set up the story and invite viewers to give "
                f"their verdict in the comments.\n"
                f"- hashtags: array of 5-8 short strings like #reddit #redditstories #aita #storytime #shorts.\n"
                f"- tags: array of 10-15 short keyword phrases (reddit stories, aita, story time, ...).\n"
                f"Output JSON only — no markdown, no commentary."
            )
        else:
            system = (
                f"You are a YouTube Shorts SEO expert for a finance channel. In {lang_name}, "
                f"return ONLY a JSON object with keys title, description, hashtags, tags.\n"
                f"- title: <=90 chars, a strong curiosity hook containing the main keyword; no false claims.\n"
                f"- description: 2-3 natural, keyword-rich sentences.\n"
                f"- hashtags: array of 5-8 short strings (no spaces).\n"
                f"- tags: array of 10-15 short keyword phrases.\n"
                f"Output JSON only — no markdown, no commentary."
            )
        payload = {"model": model, "stream": False, "keep_alive": llm.get("keep_alive", 0),
                   "format": "json",
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": f"Topic: {topic}\n\nScript:\n{text}"}],
                   "options": {"temperature": 0.6}}
        resp = requests.post(f"{host}/api/chat", json=payload, timeout=llm.get("request_timeout", 600))
        resp.raise_for_status()
        raw = resp.json().get("message", {}).get("content", "")
        m = _re.search(r"\{.*\}", raw, _re.DOTALL)
        data = _json.loads(m.group(0)) if m else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("SEO metadata generation failed (%s) — using fallback.", exc)
        return _fallback()

    title = (data.get("title") or topic or default_title).strip()[:100]
    desc = (data.get("description") or "").strip()
    hashtags = ["#" + str(h).lstrip("#").replace(" ", "") for h in (data.get("hashtags") or [])]
    if not hashtags:
        hashtags = default_tags
    tags = [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()]
    brand = config.get("brand", {})
    cta = brand.get("cta", "").strip()
    affiliate = brand.get("affiliate_block", "").strip()
    parts = [desc, cta, " ".join(hashtags), affiliate, disclaimer]
    description = "\n\n".join(p for p in parts if p).strip()
    return {"title": title, "description": description, "tags": tags, "hashtags": hashtags}


def produce_teaser(config: dict, title: str, body: str, language: str = "en") -> str:
    """One short spoken teaser line for the very start of a story short — framing only.
    Must NOT reveal the ending or add story facts. Falls back to a generic line. Never raises."""
    import random as _random
    llm = config["llm"]; host = llm["ollama_host"]
    brand = config.get("brand", {})
    fallbacks = brand.get("teasers") or [
        "You are not going to believe how this one ends.",
        "This story had the whole comment section arguing.",
        "Wait until you hear what happens next.",
    ]
    try:
        model = pick_model(llm.get("seo_model", llm["fallback"]), llm["fallback"], check_ollama(host))
        lang_name = {"en": "English", "tr": "Turkish"}.get(language, language)
        system = (f"Write ONE short spoken teaser line (max 14 words) in {lang_name} to hook a viewer at "
                  f"the start of a Reddit story video. Build curiosity about the drama. Do NOT reveal or "
                  f"spoil the outcome and do NOT invent facts. Output ONLY the sentence, no quotes, no label.")
        payload = {"model": model, "stream": False, "keep_alive": llm.get("keep_alive", 0),
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": f"Title: {title}\n\nStory: {body[:800]}"}],
                   "options": {"temperature": 0.8}}
        resp = requests.post(f"{host}/api/chat", json=payload, timeout=llm.get("request_timeout", 600))
        resp.raise_for_status()
        line = resp.json().get("message", {}).get("content", "").strip().splitlines()[0].strip()
        line = re.sub(r'^["\']|["\']$', "", line).strip()
        if 3 <= len(line.split()) <= 20:
            return line
    except Exception as exc:  # noqa: BLE001
        log.warning("teaser generation failed (%s) — using fallback.", exc)
    return _random.choice(fallbacks)


def moderate_story(config: dict, text: str, language: str = "en") -> dict:
    """Lightweight pre-upload safety check. Returns {"ok": bool, "reasons": [...]}.
    Never raises — defaults to ok on any error (uploads are private + human-reviewed anyway)."""
    import json as _json
    import re as _re
    llm = config["llm"]; host = llm["ollama_host"]
    try:
        model = pick_model(llm.get("seo_model", llm["fallback"]), llm["fallback"], check_ollama(host))
        system = ("You are a content-safety checker for a storytelling channel. Given a narration, decide "
                  "if it is safe to publish (no graphic sexual content, no explicit self-harm/suicide "
                  "methods, no gore, no slurs/hate speech, no doxxing or private personal identifying "
                  "info of real people). Mild profanity and everyday conflict are fine. "
                  'Return ONLY JSON: {"ok": true or false, "reasons": ["..."]}.')
        payload = {"model": model, "stream": False, "format": "json", "keep_alive": llm.get("keep_alive", 0),
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": text[:6000]}],
                   "options": {"temperature": 0.0}}
        resp = requests.post(f"{host}/api/chat", json=payload, timeout=llm.get("request_timeout", 600))
        resp.raise_for_status()
        m = _re.search(r"\{.*\}", resp.json()["message"]["content"], _re.DOTALL)
        data = _json.loads(m.group(0)) if m else {}
        return {"ok": bool(data.get("ok", True)), "reasons": list(data.get("reasons") or [])}
    except Exception as exc:  # noqa: BLE001
        log.warning("moderation check failed (%s) — defaulting to ok.", exc)
        return {"ok": True, "reasons": [f"check-skipped: {exc}"]}


def generate(config: dict, args: argparse.Namespace) -> None:
    fmt = getattr(args, "format", "long") or "long"
    out_path, body = produce_script(config, args.topic, fmt=fmt, language=args.language,
                                    model=args.model, data_dir=args.data_dir)
    approved_dir = get_path(config, "approved")
    log.info("")
    log.info("HUMAN REVIEW REQUIRED (nothing is auto-approved).")
    log.info("Review the draft, then approve it by copying it into content/approved/:")
    log.info('    copy "%s" "%s"', out_path, approved_dir)
    log.info("Then voice it:  python pipeline/speak.py --input %s", out_path.name)


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a finance script draft via local Ollama.")
    ap.add_argument("--topic", required=True, help="topic / prompt for the script")
    ap.add_argument("--language", help="override content.language (en|tr)")
    ap.add_argument("--model", help="override the Ollama model (e.g. aya-expanse:8b)")
    ap.add_argument("--data-dir", help="override content/data path")
    ap.add_argument("--format", choices=["long", "short"], default="long",
                    help="long monologue (default) or short-form hook script for shorts")
    ap.add_argument("--config", help="path to config.yaml")
    args = ap.parse_args()
    generate(load_config(args.config), args)


if __name__ == "__main__":
    main()
