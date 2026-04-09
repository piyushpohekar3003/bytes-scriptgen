"""
Bytes ScriptGen — Python Backend
Flask server that serves the React frontend + API endpoints.
"""
import os
import json
import re
import time
import uuid
import xml.etree.ElementTree as ET
from html import unescape
from urllib.parse import urlencode, quote, urlparse

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import anthropic
import requests

# ── Config ──
DEFAULT_API_KEY = os.environ.get("ANTHROPIC_DEFAULT_KEY", "")
API_KEY = os.environ.get("ANTHROPIC_API_KEY", DEFAULT_API_KEY)
MODEL = "claude-sonnet-4-20250514"

app = Flask(__name__, static_folder="static", static_url_path="")
CORS(app)

client = anthropic.Anthropic(api_key=API_KEY)

# ── Helpers ──

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
    # Remove URLs (raw or in href attributes)
    text = re.sub(r'href="[^"]*"', '', html)
    text = re.sub(r'https?://\S+', '', text)
    # Remove anchor tags but keep inner text
    text = re.sub(r"<a[^>]*>(.*?)</a>", r"\1", text, flags=re.DOTALL)
    # Remove all remaining HTML tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Remove HTML entities
    text = unescape(text)
    # Remove any leftover tag fragments
    text = re.sub(r'[<>]', '', text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text[:250]


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


# ── Search Queries ──

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

# ── Platform Prompts ──

PREAMBLE = 'You are a professional finance content writer for "Bytes News" — an Indian finance media brand. Your audience is Indian retail investors aged 22-40.'

PLATFORM_PROMPTS = {
    "instagram-reel": f"""{PREAMBLE}

Write a voiceover script for an Instagram Reel (40 seconds).
RULES:
- EXACTLY 100-120 words. Open with a punchy hook.
- Cover 2-3 KEY facts with specific numbers (₹, Sensex, Nifty, crore, lakh)
- Explain WHY this matters. End with a forward-looking statement.
- Tone: confident, clear, slightly urgent. NO stage directions, NO greetings, NO emojis.
- ONLY spoken words. Return ONLY the script text.""",

    "youtube-short": f"""{PREAMBLE}

Write a voiceover script for a YouTube Short (60 seconds).
RULES:
- EXACTLY 150-170 words. Open with a compelling hook.
- Cover 3-4 key facts. Add one expert-level insight.
- End with engagement CTA. NO stage directions, NO emojis. ONLY spoken words.
Return ONLY the script text.""",

    "tiktok": f"""{PREAMBLE}

Write a voiceover script for TikTok (30 seconds).
RULES:
- EXACTLY 75-90 words. First sentence MUST be a scroll-stopper.
- Max 2 key facts. Use "you/your" heavily. Casual, punchy, short sentences.
- End with a hot take or question. ONLY spoken words.
Return ONLY the script text.""",

    "instagram-carousel": f"""{PREAMBLE}

Create an Instagram carousel (8-10 slides).
RULES:
- Slide 1: Bold hook headline only (max 8 words)
- Slides 2-8: header (3-5 words) and body (15-25 words)
- Slide 9: Key takeaway. Slide 10: CTA.
- Use specific numbers. NO emojis in headers.
Return ONLY valid JSON: array of {{"slideNumber": number, "header": string, "body": string}}""",

    "linkedin-carousel": f"""{PREAMBLE}

Create a LinkedIn carousel (8-10 slides).
RULES:
- Slide 1: Professional headline. Slides 2-8: header (3-6 words) and body (20-35 words) with data.
- Slide 9: Strategic implications. Slide 10: Professional CTA.
- Use industry terminology.
Return ONLY valid JSON: array of {{"slideNumber": number, "header": string, "body": string}}""",

    "youtube-longform": f"""{PREAMBLE}

Write a YouTube long-form script (3-5 minutes).
RULES:
- EXACTLY 500-700 words. Structure: [INTRO] ~60w, [WHAT HAPPENED] ~120w, [WHY IT MATTERS] ~120w, [DEEPER ANALYSIS] ~120w, [WHAT TO WATCH] ~80w, [OUTRO] ~50w
- Include specific numbers. Tone: like a market analyst on YouTube.
- ONLY spoken words with section headers.
Return ONLY the script text.""",
}


# ══════════════════════════════════════
# ROUTES
# ══════════════════════════════════════

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


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

        # Parse first <item>
        m = re.search(r"<item>(.*?)</item>", xml, re.DOTALL)
        if not m:
            continue
        item_xml = m.group(1)

        title = extract_cdata(item_xml, "title")
        if not title:
            continue

        # Dedup by first 4 words
        title_key = " ".join(title.lower().split()[:4])
        if title_key in seen:
            continue
        seen.add(title_key)

        # Check age
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

        # Source info
        source_name = extract_cdata(item_xml, "source")
        src_url_m = re.search(r'<source\s+url="([^"]+)"', item_xml)
        hostname = ""
        if src_url_m:
            try:
                hostname = urlparse(src_url_m.group(1)).hostname or ""
            except Exception:
                pass

        description = extract_cdata(item_xml, "description")
        summary = clean_html(description) or f"Latest news on {q['label']}"
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


@app.route("/api/scripts/generate", methods=["POST"])
def generate_scripts():
    data = request.json or {}
    topics_list = data.get("topics", [])
    platform = data.get("platform", "instagram-reel")

    if not topics_list or not platform:
        return jsonify({"error": "topics and platform are required"}), 400
    if len(topics_list) > 5:
        return jsonify({"error": "Maximum 5 topics at a time"}), 400

    prompt_template = PLATFORM_PROMPTS.get(platform, PLATFORM_PROMPTS["instagram-reel"])
    is_carousel = platform in ("instagram-carousel", "linkedin-carousel")

    scripts = []
    for topic in topics_list:
        article_text = topic.get("articleSummary", "")
        article_title = topic.get("articleTitle", topic.get("keyword", ""))

        prompt = f"""{prompt_template}

NEWS ARTICLE:
Title: {article_title}
Content: {article_text[:3000]}"""

        result = call_claude(prompt, 1500)

        script_obj = {
            "id": str(uuid.uuid4()),
            "topicId": topic.get("id", ""),
            "keyword": topic.get("keyword", ""),
            "articleTitle": article_title,
            "platform": platform,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "revisions": [],
        }

        if is_carousel:
            json_match = re.search(r"\[[\s\S]*\]", result)
            if json_match:
                slides = json.loads(json_match.group(0))
                script_obj["slides"] = slides
                script_obj["wordCount"] = sum(
                    len((s.get("header", "") + " " + s.get("body", "")).split()) for s in slides
                )
            else:
                script_obj["script"] = result.strip()
                script_obj["wordCount"] = len(result.split())
        else:
            script_obj["script"] = result.strip()
            wc = len(result.split())
            script_obj["wordCount"] = wc
            secs = round(wc / 2.5)
            script_obj["estimatedDuration"] = f"{secs // 60}:{secs % 60:02d}"

        scripts.append(script_obj)

    return jsonify({"scripts": scripts})


@app.route("/api/scripts/revise", methods=["POST"])
def revise_script():
    data = request.json or {}
    feedback = data.get("feedback", "")
    current_script = data.get("currentScript", "")
    current_slides = data.get("currentSlides")
    platform = data.get("platform", "instagram-reel")
    article_title = data.get("articleTitle", "")

    if not feedback:
        return jsonify({"error": "feedback is required"}), 400

    is_carousel = platform in ("instagram-carousel", "linkedin-carousel")
    current_content = json.dumps(current_slides, indent=2) if is_carousel else current_script

    prompt = f"""Revise this {platform} content based on user feedback. Keep the same format and word count requirements.

CURRENT VERSION:
{current_content}

USER FEEDBACK:
{feedback}

NEWS TOPIC: {article_title}

Return ONLY the revised content in the exact same format."""

    result = call_claude(prompt, 1500)

    revision = {"feedback": feedback, "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ")}

    if is_carousel:
        json_match = re.search(r"\[[\s\S]*\]", result)
        if json_match:
            slides = json.loads(json_match.group(0))
            revision["slides"] = slides
            revision["wordCount"] = sum(
                len((s.get("header", "") + " " + s.get("body", "")).split()) for s in slides
            )
    else:
        revision["script"] = result.strip()
        revision["wordCount"] = len(result.split())

    return jsonify({"revision": revision})


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
    return jsonify({"hasKey": bool(API_KEY), "isDefault": API_KEY == DEFAULT_API_KEY})


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
    import webbrowser
    webbrowser.open(f"http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
