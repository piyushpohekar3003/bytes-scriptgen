"""
Bytes ScriptGen — Python Backend
Flask server that serves the React frontend + API endpoints.
Now supports the A1 Bytes framework (5 categories) + HeyGen video pipeline.
"""
import os
import io
import json
import re
import time
import uuid
import threading
from html import unescape
from urllib.parse import quote, urlparse
from dataclasses import dataclass, field, asdict
from typing import Optional, Any

from flask import Flask, request, jsonify, send_from_directory, send_file
from flask_cors import CORS
import anthropic
import requests

# ── Local modules ──
from bytes_patterns import CATEGORY_PATTERNS, CATEGORY_LABELS, CATEGORY_ICONS
import storyboard as sb
import heygen as hg

# ── Config ──
DEFAULT_API_KEY = os.environ.get("ANTHROPIC_DEFAULT_KEY", "")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", DEFAULT_API_KEY)
MODEL = "claude-sonnet-4-20250514"

# Persistent storage paths — only for short-lived storyboard PDFs.
# Videos are NOT stored — we keep only the HeyGen URL and stream on download.
STORAGE_ROOT = os.environ.get("STORAGE_ROOT", "/tmp/bytes-scriptgen")
STORYBOARDS_DIR = os.path.join(STORAGE_ROOT, "storyboards")
os.makedirs(STORYBOARDS_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

client = anthropic.Anthropic(api_key=API_KEY)


# ══════════════════════════════════════
# Helpers
# ══════════════════════════════════════

def call_claude(prompt: str, max_tokens: int = 1024) -> str:
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text if resp.content else ""


def fetch_url(url: str, timeout: int = 8) -> str:
    try:
        r = requests.get(url, timeout=timeout, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        return r.text
    except Exception:
        return ""


def clean_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r'<a\s[^>]*>(.*?)</a>', r'\1', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<font\s[^>]*>(.*?)</font>', r'\1', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]*>', ' ', text)
    text = re.sub(r'\ba\s+target="[^"]*"', '', text)
    text = re.sub(r'\bfont\s+color="[^"]*"', '', text)
    text = re.sub(r'\btarget="[^"]*"', '', text)
    text = re.sub(r'\bhref="[^"]*"', '', text)
    text = text.replace('&nbsp;', ' ').replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = re.sub(r'&[a-zA-Z]+;', ' ', text)
    text = re.sub(r'&#\d+;', ' ', text)
    text = unescape(text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'[<>]', '', text)
    text = re.sub(r'/font\b', '', text)
    text = re.sub(r'/a\b', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def extract_cdata(xml_str: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\s*<!\[CDATA\[(.*?)\]\]>\s*</{tag}>", xml_str, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", xml_str, re.DOTALL)
    return m.group(1).strip() if m else ""


def resolve_article_url(title: str, hostname: str) -> str:
    clean_title = title.split(" - ")[0].split(" | ")[0].strip()[:60]
    if hostname:
        return f'https://www.google.com/search?q={quote(f"{clean_title} site:{hostname}")}'
    return f'https://www.google.com/search?q={quote(clean_title)}'


def extract_article_text(url: str) -> str:
    """Scrape readable text from an article URL. Best-effort."""
    html = fetch_url(url, timeout=10)
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()
        # Prefer <article> or main <p> paragraphs
        article = soup.find("article")
        if article:
            paragraphs = article.find_all("p")
        else:
            paragraphs = soup.find_all("p")
        text = "\n\n".join(p.get_text(" ", strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 40)
        if len(text) < 200:
            # Fallback: get all visible text
            text = soup.get_text(" ", strip=True)
        return clean_html(text)[:8000]
    except Exception as e:
        print(f"[extract_article_text] {e}")
        return clean_html(html)[:8000]


# ══════════════════════════════════════
# Search Queries + Scoring
# ══════════════════════════════════════

QUERIES = [
    # Equity
    {"query": "India stock market today", "category": "equity", "label": "Stock Market"},
    {"query": "Sensex Nifty today India", "category": "equity", "label": "Sensex / Nifty"},
    {"query": "Indian rupee dollar RBI", "category": "equity", "label": "Indian Rupee"},
    {"query": "FII DII flows India stocks", "category": "equity", "label": "FII / DII Flows"},
    {"query": "RBI monetary policy rate", "category": "equity", "label": "RBI Policy"},
    {"query": "SEBI regulation India", "category": "equity", "label": "SEBI Regulation"},
    {"query": "India banking sector stocks", "category": "equity", "label": "Banking Sector"},
    {"query": "mutual fund India SIP inflow", "category": "equity", "label": "Mutual Funds"},
    {"query": "India midcap smallcap stocks", "category": "equity", "label": "Midcap / Smallcap"},
    {"query": "India IT sector stock TCS", "category": "equity", "label": "IT Sector"},
    # IPO
    {"query": "India IPO launch 2026", "category": "ipo", "label": "New IPO"},
    {"query": "IPO GMP grey market premium India", "category": "ipo", "label": "IPO GMP"},
    {"query": "IPO listing performance India", "category": "ipo", "label": "IPO Listing"},
    {"query": "SME IPO India subscription", "category": "ipo", "label": "SME IPO"},
    {"query": "upcoming IPO India mainboard", "category": "ipo", "label": "Upcoming IPO"},
    {"query": "IPO subscription status India", "category": "ipo", "label": "IPO Subscription"},
    {"query": "IPO allotment date India", "category": "ipo", "label": "IPO Allotment"},
    # Commodities
    {"query": "crude oil price India impact", "category": "commodities", "label": "Crude Oil"},
    {"query": "gold price India today", "category": "commodities", "label": "Gold"},
    {"query": "silver price India market", "category": "commodities", "label": "Silver"},
    {"query": "copper price India commodity", "category": "commodities", "label": "Copper"},
    {"query": "natural gas price India", "category": "commodities", "label": "Natural Gas"},
]

SCORING_WEIGHTS = {
    "googleTrends": {"label": "Google Trends", "weight": 5, "color": "#34A853"},
    "twitterTier1": {"label": "Curated Twitter", "weight": 5, "color": "#1DA1F2"},
    "twitterTier2": {"label": "Twitter 50k+", "weight": 3, "color": "#71C9F8"},
    "reddit": {"label": "Reddit", "weight": 3, "color": "#FF4500"},
    "publications": {"label": "Publications", "weight": 1, "color": "#666"},
}


# ══════════════════════════════════════
# Job Pipeline (in-memory)
# ══════════════════════════════════════

@dataclass
class PipelineStep:
    name: str
    label: str
    status: str = "pending"  # pending | running | done | failed
    progress: int = 0  # 0-100
    eta_seconds: int = 0
    message: str = ""
    started_at: Optional[float] = None
    ended_at: Optional[float] = None


@dataclass
class PipelineJob:
    id: str
    created_at: float
    category: str
    script: str
    avatar_id: str
    voice_id: str
    article_title: str = ""
    status: str = "running"  # running | done | failed
    error: Optional[str] = None
    heygen_video_id: Optional[str] = None
    # artifacts: {storyboard_pdf: local path, video_url: HeyGen CDN URL, thumbnail_url, duration}
    artifacts: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)


jobs: dict[str, PipelineJob] = {}
jobs_lock = threading.Lock()


def update_step(job: PipelineJob, step_name: str, **updates):
    """Thread-safe step update."""
    with jobs_lock:
        for step in job.steps:
            if step.name == step_name:
                for k, v in updates.items():
                    setattr(step, k, v)
                return


def run_visuals_and_pdf(job: PipelineJob):
    """Thread 1: generate visual JSON → render PDF."""
    try:
        # Step: visuals
        update_step(job, "visuals", status="running", started_at=time.time(), progress=10, message="Generating shot breakdown...")
        visuals = sb.generate_visuals(job.script, job.category)
        if not visuals:
            raise RuntimeError("Visual generator returned empty JSON")
        update_step(job, "visuals", status="done", progress=100, ended_at=time.time(),
                    message=f"Generated {len(visuals)} shots")

        # Step: storyboard_pdf
        update_step(job, "storyboard_pdf", status="running", started_at=time.time(), progress=20, message="Rendering PDF...")
        title = f"A1 Bytes — {CATEGORY_LABELS.get(job.category, job.category.title())}"
        if job.article_title:
            title = f"A1 Bytes — {job.article_title[:60]}"
        pdf_buffer = sb.render_storyboard_pdf(visuals, job.script, title)

        pdf_path = os.path.join(STORYBOARDS_DIR, f"{job.id}.pdf")
        with open(pdf_path, "wb") as f:
            f.write(pdf_buffer.getvalue())

        with jobs_lock:
            job.artifacts["storyboard_pdf"] = pdf_path
        update_step(job, "storyboard_pdf", status="done", progress=100, ended_at=time.time(),
                    message="Storyboard PDF ready")
    except Exception as e:
        print(f"[Pipeline {job.id}] visuals/pdf error: {e}")
        update_step(job, "visuals", status="failed", message=str(e)[:120])
        update_step(job, "storyboard_pdf", status="failed", message="Skipped due to earlier error")


def run_heygen(job: PipelineJob):
    """Thread 2: submit to HeyGen → poll → download."""
    try:
        # Step: heygen_submit
        update_step(job, "heygen_submit", status="running", started_at=time.time(), progress=20,
                    message="Submitting script to HeyGen...")
        video_id = hg.submit_video(job.script, job.avatar_id, job.voice_id)
        job.heygen_video_id = video_id
        update_step(job, "heygen_submit", status="done", progress=100, ended_at=time.time(),
                    message=f"Submitted (video_id: {video_id[:10]}...)")

        # Step: heygen_poll — poll every 15s, updating progress
        update_step(job, "heygen_poll", status="running", started_at=time.time(), progress=0,
                    message="Waiting for HeyGen to render video...", eta_seconds=600)

        start = time.time()
        max_wait = 20 * 60  # 20 minutes
        while True:
            elapsed = time.time() - start
            if elapsed > max_wait:
                raise TimeoutError("HeyGen rendering timed out after 20 minutes")

            info = hg.poll_video(video_id)
            status = info.get("status", "unknown")
            video_url = info.get("video_url")

            # Estimate progress: HeyGen typically takes 5-10 min, cap at 90% until complete
            progress = min(90, int((elapsed / 600) * 90))
            eta = max(60, int(600 - elapsed))
            update_step(job, "heygen_poll", progress=progress, eta_seconds=eta,
                        message=f"Status: {status} ({int(elapsed)}s elapsed)")

            if status == "completed" and video_url:
                # Don't download — store HeyGen's CDN URL only.
                # User downloads directly from HeyGen via redirect.
                with jobs_lock:
                    job.artifacts["video_url"] = video_url
                    if info.get("thumbnail_url"):
                        job.artifacts["thumbnail_url"] = info.get("thumbnail_url")
                    if info.get("duration"):
                        job.artifacts["duration"] = info.get("duration")
                update_step(job, "heygen_poll", status="done", progress=100, ended_at=time.time(),
                            message="Video ready on HeyGen")
                return

            if status == "failed":
                raise RuntimeError(f"HeyGen failed: {info.get('error', 'unknown error')}")

            time.sleep(15)
    except Exception as e:
        print(f"[Pipeline {job.id}] heygen error: {e}")
        update_step(job, "heygen_submit", status="failed" if job.heygen_video_id is None else "done",
                    message=str(e)[:120])
        update_step(job, "heygen_poll", status="failed", message=str(e)[:120])


def run_pipeline(job: PipelineJob):
    """Run the two parallel threads and mark job complete."""
    t1 = threading.Thread(target=run_visuals_and_pdf, args=(job,), daemon=True)
    t2 = threading.Thread(target=run_heygen, args=(job,), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Finalize
    any_failed = any(s.status == "failed" for s in job.steps)
    with jobs_lock:
        job.status = "failed" if any_failed else "done"


def serialize_job(job: PipelineJob) -> dict:
    with jobs_lock:
        return {
            "id": job.id,
            "created_at": job.created_at,
            "category": job.category,
            "article_title": job.article_title,
            "status": job.status,
            "error": job.error,
            "heygen_video_id": job.heygen_video_id,
            "artifacts": {
                "storyboard_pdf": bool(job.artifacts.get("storyboard_pdf")),
                "video_url": job.artifacts.get("video_url"),  # HeyGen CDN URL (or null)
                "thumbnail_url": job.artifacts.get("thumbnail_url"),
                "duration": job.artifacts.get("duration"),
            },
            "steps": [asdict(s) for s in job.steps],
        }


# ══════════════════════════════════════
# ROUTES — static SPA
# ══════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


# ══════════════════════════════════════
# ROUTES — Topics (Quick Scan)
# ══════════════════════════════════════

@app.route("/api/topics")
def get_topics():
    mode = request.args.get("mode", "quick")
    limit = int(request.args.get("limit", 20))

    topics = []
    seen = set()

    for i, q in enumerate(QUERIES):
        rss_url = f"https://news.google.com/rss/search?q={quote(q['query'] + ' India finance when:1d')}&hl=en-IN&gl=IN&ceid=IN:en"
        xml = fetch_url(rss_url)
        if not xml:
            continue

        m = re.search(r"<item>(.*?)</item>", xml, re.DOTALL)
        if not m:
            continue
        item_xml = m.group(1)

        title = extract_cdata(item_xml, "title")
        if not title:
            continue

        title_key = " ".join(title.lower().split()[:4])
        if title_key in seen:
            continue
        seen.add(title_key)

        pub_date = extract_cdata(item_xml, "pubDate")
        if pub_date:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(pub_date)
                age = time.time() - dt.timestamp()
                if age > 48 * 3600:
                    continue
            except Exception:
                pass

        source_name = extract_cdata(item_xml, "source")
        src_url_m = re.search(r'<source\s+url="([^"]+)"', item_xml)
        hostname = ""
        if src_url_m:
            try:
                hostname = urlparse(src_url_m.group(1)).hostname or ""
            except Exception:
                pass

        description = extract_cdata(item_xml, "description")
        summary = clean_html(description)[:250] or f"Latest news on {q['label']}"
        article_url = resolve_article_url(title, hostname)

        topics.append({
            "id": f"q-{i}",
            "keyword": q["label"],
            "headline": clean_html(title),
            "articleUrl": article_url,
            "articleTitle": clean_html(title),
            "articleSummary": summary,
            "category": q["category"],
            "score": 2,
            "articles": [{"url": article_url, "sourceName": source_name or "Google News", "title": clean_html(title)}],
            "tweets": [],
            "sources": {
                "googleTrends": False,
                "twitter": {"tier1Count": 0, "tier1Handles": [], "tier2Count": 0, "tier2Handles": []},
                "reddit": {"count": 0, "score": 0, "subreddits": []},
                "publications": {"count": 1, "names": [source_name or "Google News"]},
            },
        })

        if len(topics) >= limit:
            break

    # LLM headline enrichment
    if topics and API_KEY:
        try:
            topic_lines = "\n".join(
                f'{i}. keyword="{t["keyword"]}" title="{t["articleTitle"]}" summary="{t["articleSummary"]}"'
                for i, t in enumerate(topics)
            )
            prompt = f"""You are a finance news editor. For each topic below, write an insightful headline.

GOOD: "Sensex Rallies 969 Points to Close Above 23,200" (specific number + level)
BAD: "Stock Market Today" (just restating the topic)

RULES:
- Extract the REAL financial insight: what happened? what number? what changed?
- Typically 6-12 words, no hard limit. Use ₹ for Indian rupees.
- NEVER include "today", specific dates, or "analysis"

Return ONLY a JSON array of strings, one per topic. No explanation.

Topics:
{topic_lines}"""
            headlines_raw = call_claude(prompt, 800)
            json_match = re.search(r"\[[\s\S]*\]", headlines_raw)
            if json_match:
                headlines = json.loads(json_match.group(0))
                for i, h in enumerate(headlines):
                    if i < len(topics) and h:
                        topics[i]["headline"] = h
        except Exception as e:
            print(f"[Topics] LLM headline error: {e}")

    return jsonify({
        "topics": topics,
        "meta": {"mode": mode, "totalDiscovered": len(topics), "weights": SCORING_WEIGHTS},
    })


# ══════════════════════════════════════
# ROUTES — Manual Input
# ══════════════════════════════════════

@app.route("/api/manual-input", methods=["POST"])
def manual_input():
    """
    Accept manual article input: URL + optional fallback text.
    Returns a normalized topic object ready for script generation.
    """
    data = request.json or {}
    article_url = (data.get("articleUrl") or "").strip()
    article_title = (data.get("articleTitle") or "").strip()
    fallback_text = (data.get("fallbackText") or "").strip()

    article_text = ""
    resolved_title = article_title

    # Try URL extraction first
    if article_url:
        extracted = extract_article_text(article_url)
        if extracted and len(extracted) > 200:
            article_text = extracted
            if not resolved_title:
                # Try to grab title from page <title>
                html = fetch_url(article_url, timeout=8)
                tm = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
                if tm:
                    resolved_title = clean_html(tm.group(1))[:200]

    # Fall back to user-provided text
    if len(article_text) < 200 and fallback_text:
        article_text = fallback_text

    if not article_text:
        return jsonify({"error": "Could not extract article content. Provide fallback text."}), 400

    if not resolved_title:
        resolved_title = article_text[:80] + "..."

    summary = (article_text[:240] + "...") if len(article_text) > 240 else article_text

    # Auto-detect category so the UI can pre-select it
    category_key = "stock"
    try:
        category_key = sb.detect_category(resolved_title, article_text)
    except Exception as e:
        print(f"[manual-input] category detection failed: {e}")

    topic = {
        "id": f"manual-{uuid.uuid4().hex[:8]}",
        "keyword": CATEGORY_LABELS.get(category_key, "Custom"),
        "headline": resolved_title,
        "articleUrl": article_url,
        "articleTitle": resolved_title,
        "articleSummary": summary,
        "articleText": article_text,  # full text for script generation
        "category": category_key,
        "score": 0,
        "articles": [{"url": article_url, "sourceName": "Manual Input", "title": resolved_title}] if article_url else [],
        "tweets": [],
        "sources": {
            "googleTrends": False,
            "twitter": {"tier1Count": 0, "tier1Handles": [], "tier2Count": 0, "tier2Handles": []},
            "reddit": {"count": 0, "score": 0, "subreddits": []},
            "publications": {"count": 0, "names": []},
        },
    }
    return jsonify({"topic": topic, "detectedCategory": category_key})


# ══════════════════════════════════════
# ROUTES — Scripts (A1 Bytes framework)
# ══════════════════════════════════════

@app.route("/api/scripts/generate", methods=["POST"])
def generate_scripts():
    """
    Generate structured A1 Bytes scripts for 1-5 topics.
    Request: {topics: [...], category?: string}  (category=null → auto-detect per topic)
    """
    data = request.json or {}
    topics_list = data.get("topics", [])
    forced_category = data.get("category")  # null/undefined = auto-detect

    if not topics_list:
        return jsonify({"error": "topics are required"}), 400
    if len(topics_list) > 5:
        return jsonify({"error": "Maximum 5 topics at a time"}), 400

    scripts = []
    for topic in topics_list:
        article_text = topic.get("articleText") or topic.get("articleSummary", "")
        article_title = topic.get("articleTitle", topic.get("keyword", ""))

        # Resolve category
        category = forced_category or topic.get("category")
        if not category or category not in CATEGORY_PATTERNS:
            try:
                category = sb.detect_category(article_title, article_text)
            except Exception as e:
                print(f"[scripts] detect_category failed: {e}")
                category = "stock"

        # Compose the "raw script" the structurer wants: title + article text
        raw_input = f"Headline: {article_title}\n\n{article_text}"

        try:
            structured = sb.structure_script(raw_input, category)
        except Exception as e:
            print(f"[scripts] structure_script error: {e}")
            structured = f"[Avatar] Script generation failed: {e}"

        wc = len(structured.split())
        secs = round(wc / 2.5)
        script_obj = {
            "id": str(uuid.uuid4()),
            "topicId": topic.get("id", ""),
            "keyword": topic.get("keyword", ""),
            "articleTitle": article_title,
            "category": category,
            "categoryLabel": CATEGORY_LABELS.get(category, category.title()),
            "categoryIcon": CATEGORY_ICONS.get(category, "📰"),
            "script": structured,
            "wordCount": wc,
            "estimatedDuration": f"{secs // 60}:{secs % 60:02d}",
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "revisions": [],
        }
        scripts.append(script_obj)

    return jsonify({"scripts": scripts})


@app.route("/api/scripts/revise", methods=["POST"])
def revise_script_route():
    """Revise a structured script based on user feedback, keeping category pattern."""
    data = request.json or {}
    feedback = data.get("feedback", "")
    current_script = data.get("currentScript", "")
    category = data.get("category", "stock")

    if not feedback or not current_script:
        return jsonify({"error": "feedback and currentScript are required"}), 400

    if category not in CATEGORY_PATTERNS:
        category = "stock"

    try:
        revised = sb.revise_structured_script(current_script, feedback, category)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    wc = len(revised.split())
    return jsonify({
        "revision": {
            "feedback": feedback,
            "script": revised,
            "wordCount": wc,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    })


# ══════════════════════════════════════
# ROUTES — HeyGen metadata
# ══════════════════════════════════════

@app.route("/api/heygen/avatars")
def get_heygen_avatars():
    try:
        avatars = hg.list_avatars()
        # Return trimmed fields (custom avatars only)
        simplified = [
            {
                "avatar_id": a.get("avatar_id"),
                "avatar_name": a.get("avatar_name"),
                "group_name": a.get("group_name"),
                "gender": a.get("gender"),
                "preview_image_url": a.get("preview_image_url"),
                "preview_video_url": a.get("preview_video_url"),
                "default_voice_id": a.get("default_voice_id"),
            }
            for a in avatars
            if a.get("avatar_id")
        ]
        return jsonify({"avatars": simplified})
    except requests.HTTPError as e:
        return jsonify({"error": f"HeyGen API error: {e.response.status_code}", "avatars": []}), 502
    except Exception as e:
        return jsonify({"error": str(e), "avatars": []}), 500


@app.route("/api/heygen/voices")
def get_heygen_voices():
    try:
        voices = hg.list_voices()
        simplified = [
            {
                "voice_id": v.get("voice_id"),
                "name": v.get("name"),
                "language": v.get("language"),
                "gender": v.get("gender"),
                "preview_audio": v.get("preview_audio"),
            }
            for v in voices
            if v.get("voice_id")
        ]
        return jsonify({"voices": simplified})
    except Exception as e:
        return jsonify({"error": str(e), "voices": []}), 500


# ══════════════════════════════════════
# ROUTES — Pipeline
# ══════════════════════════════════════

@app.route("/api/pipeline/start", methods=["POST"])
def start_pipeline():
    """
    Start the full Storyboard + HeyGen pipeline.
    Request: {script, category, avatar_id, voice_id, article_title?}
    """
    data = request.json or {}
    script = data.get("script", "").strip()
    category = data.get("category", "stock")
    avatar_id = data.get("avatar_id", "").strip()
    voice_id = data.get("voice_id", "").strip()
    article_title = data.get("articleTitle", "").strip()

    if not script:
        return jsonify({"error": "script is required"}), 400
    if not avatar_id:
        return jsonify({"error": "avatar_id is required"}), 400
    if not voice_id:
        return jsonify({"error": "voice_id is required"}), 400
    if not os.environ.get("HEYGEN_API_KEY"):
        return jsonify({"error": "HEYGEN_API_KEY not configured on server"}), 500

    job_id = f"job-{uuid.uuid4().hex[:10]}"
    steps = [
        PipelineStep(name="visuals", label="Generate shot breakdown"),
        PipelineStep(name="storyboard_pdf", label="Render storyboard PDF"),
        PipelineStep(name="heygen_submit", label="Submit to HeyGen"),
        PipelineStep(name="heygen_poll", label="Wait for video render"),
    ]
    job = PipelineJob(
        id=job_id,
        created_at=time.time(),
        category=category,
        script=script,
        avatar_id=avatar_id,
        voice_id=voice_id,
        article_title=article_title,
        steps=steps,
    )
    with jobs_lock:
        jobs[job_id] = job

    # Fire-and-forget pipeline thread
    threading.Thread(target=run_pipeline, args=(job,), daemon=True).start()

    return jsonify({"job_id": job_id, "job": serialize_job(job)})


@app.route("/api/pipeline/<job_id>/status")
def pipeline_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    return jsonify({"job": serialize_job(job)})


@app.route("/api/pipeline/<job_id>/download/<artifact>")
def pipeline_download(job_id: str, artifact: str):
    """
    Storyboard PDF — served from local /tmp (small file, generated server-side).
    Video MP4 — redirected to HeyGen's CDN URL (we don't store videos).
    """
    from flask import redirect, Response, stream_with_context
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404

    if artifact == "storyboard":
        path = job.artifacts.get("storyboard_pdf")
        if not path or not os.path.exists(path):
            return jsonify({"error": "storyboard not ready"}), 404
        return send_file(path, as_attachment=True, download_name=f"bytes-storyboard-{job_id}.pdf")

    if artifact == "video":
        video_url = job.artifacts.get("video_url")
        if not video_url:
            return jsonify({"error": "video not ready"}), 404
        # Stream HeyGen's video through our server with a friendly filename
        # (so downloads have a proper name and CORS-safe).
        try:
            r = requests.get(video_url, stream=True, timeout=30)
            r.raise_for_status()
            filename = f"bytes-video-{job_id}.mp4"
            return Response(
                stream_with_context(r.iter_content(chunk_size=8192)),
                mimetype="video/mp4",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Length": r.headers.get("Content-Length", ""),
                },
            )
        except Exception as e:
            # Fallback: just redirect to HeyGen URL if streaming fails
            return redirect(video_url)

    return jsonify({"error": "unknown artifact"}), 400


@app.route("/api/pipeline/list")
def pipeline_list():
    """List all jobs (sorted newest first)."""
    with jobs_lock:
        all_jobs = sorted(jobs.values(), key=lambda j: j.created_at, reverse=True)
    return jsonify({"jobs": [serialize_job(j) for j in all_jobs[:50]]})


# ══════════════════════════════════════
# ROUTES — Settings
# ══════════════════════════════════════

@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    """Allow users to set their own API key."""
    global client, API_KEY
    if request.method == "POST":
        data = request.json or {}
        new_key = data.get("apiKey", "")
        if new_key:
            API_KEY = new_key
            client = anthropic.Anthropic(api_key=API_KEY)
            return jsonify({"status": "ok", "message": "API key updated"})
        return jsonify({"error": "apiKey is required"}), 400
    return jsonify({
        "hasKey": bool(API_KEY),
        "isDefault": API_KEY == DEFAULT_API_KEY,
        "hasHeyGenKey": bool(os.environ.get("HEYGEN_API_KEY")),
    })


# Serve React SPA for all non-API routes
@app.route("/<path:path>")
def serve_spa(path):
    if os.path.exists(os.path.join("static", path)):
        return send_from_directory("static", path)
    return send_from_directory("static", "index.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"\n  Bytes ScriptGen running at http://localhost:{port}")
    print(f"  Open this URL in your browser\n")
    try:
        import webbrowser
        webbrowser.open(f"http://localhost:{port}")
    except Exception:
        pass
    app.run(host="0.0.0.0", port=port, debug=False)
