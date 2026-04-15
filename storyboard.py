"""
Storyboard service — structure script, generate visual breakdown, render PDF.
Ports from storyboard-studio services/generator.py and services/exporter.py.
"""
import json
import re
import io
from typing import List, Dict, Any, Optional

import anthropic

from bytes_patterns import (
    CATEGORY_PATTERNS,
    CATEGORY_LABELS,
    script_structurer_system,
    VISUAL_GENERATOR_SYSTEM,
    CATEGORY_DETECTION_PROMPT,
)


def _client():
    """Lazy Anthropic client — reads env at call time."""
    import os
    return anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))


MODEL = "claude-sonnet-4-20250514"


def structure_script(raw_script: str, category_key: str) -> str:
    """
    Restructure a raw script to follow a category's STRICT visual framework pattern.
    Returns the formatted script with [Avatar + …] / [B-roll + …] line prefixes.
    """
    pattern = CATEGORY_PATTERNS.get(category_key, CATEGORY_PATTERNS["stock"])
    system = script_structurer_system(pattern)
    user_msg = f"""Restructure this raw script to STRICTLY follow the **{category_key}** category framework.

IMPORTANT: REORDER the content. The pattern dictates which information comes first, second, etc. Extract and rearrange accordingly.

## Raw Script
{raw_script}

Output the restructured script following the EXACT pattern above."""

    resp = _client().messages.create(
        model=MODEL,
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return resp.content[0].text.strip() if resp.content else ""


def revise_structured_script(
    current_script: str, feedback: str, category_key: str
) -> str:
    """Revise a structured script based on user feedback while keeping the category pattern."""
    pattern = CATEGORY_PATTERNS.get(category_key, CATEGORY_PATTERNS["stock"])
    system = script_structurer_system(pattern)
    user_msg = f"""Here is the current structured script following the **{category_key}** pattern:

{current_script}

## User Feedback
{feedback}

Revise the script incorporating the feedback. The output MUST still follow the EXACT {category_key} pattern (same line count, same visual markers). Return ONLY the revised script."""

    resp = _client().messages.create(
        model=MODEL,
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    return resp.content[0].text.strip() if resp.content else ""


def generate_visuals(structured_script: str, category_key: str) -> List[Dict[str, Any]]:
    """
    Generate a shot-by-shot JSON visual breakdown from a structured script.
    Returns list of shot dicts: {shot, voiceover, visual_type, supers, broll_description, duration_hint, notes}
    """
    user_msg = f"""Generate a shot-by-shot visual breakdown for this **{category_key}** category script.

## Script
{structured_script}

Output ONLY a valid JSON array. No markdown wrapping, no explanation."""

    resp = _client().messages.create(
        model=MODEL,
        max_tokens=2048,
        system=VISUAL_GENERATOR_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = resp.content[0].text.strip() if resp.content else "[]"
    # Strip any markdown fences
    raw = re.sub(r"```(?:json)?\s*", "", raw).replace("```", "").strip()
    # Extract JSON array
    m = re.search(r"\[[\s\S]*\]", raw)
    if m:
        raw = m.group(0)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def detect_category(article_title: str, article_text: str) -> str:
    """Classify article into one of 5 Bytes categories via LLM. Returns category key."""
    content = (article_title or "") + "\n\n" + (article_text or "")[:2000]
    resp = _client().messages.create(
        model=MODEL,
        max_tokens=20,
        system=CATEGORY_DETECTION_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    raw = resp.content[0].text.strip().lower() if resp.content else "stock"
    # Extract just the category word
    for key in CATEGORY_PATTERNS.keys():
        if key in raw:
            return key
    return "stock"  # safe default


# ──────────────────────────────────────────────────────────────────────
# PDF Export
# ──────────────────────────────────────────────────────────────────────

def render_storyboard_pdf(
    visuals: List[Dict[str, Any]],
    script_text: str = "",
    title: str = "A1 Bytes — Visual Storyboard",
) -> io.BytesIO:
    """
    Render the visual storyboard to a PDF.
    Simplified port of storyboard-studio bytes_visuals_to_pdf — no external Unsplash images,
    no aparna.png; just clean cards with shot details.
    """
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    page_w, page_h = landscape(A4)
    c = canvas.Canvas(buffer, pagesize=landscape(A4))

    FW = 140  # frame width in points
    FH = FW * 16 / 9  # 9:16 ratio
    MARGIN = 10 * mm
    GAP = 12
    SHOTS_PER_ROW = 4
    TEXT_AREA = 90  # space below frame for shot text

    type_labels = {
        "presenter": "Presenter",
        "broll": "B-Roll",
        "presenter_stockcard": "Stock Card",
    }
    border_colors = {
        "presenter": colors.Color(0.55, 0.72, 0.95),
        "broll": colors.Color(0.45, 0.82, 0.55),
        "presenter_stockcard": colors.Color(0.95, 0.75, 0.3),
    }
    fill_colors = {
        "presenter": colors.Color(0.91, 0.88, 0.83),
        "broll": colors.Color(0.2, 0.25, 0.3),
        "presenter_stockcard": colors.Color(0.95, 0.93, 0.85),
    }

    def draw_shot(shot_data: Dict[str, Any], x: float, y: float):
        vtype = shot_data.get("visual_type", "presenter")
        shot_num = shot_data.get("shot", "?")
        duration = shot_data.get("duration_hint", "3s")
        voiceover = shot_data.get("voiceover", "")
        supers = shot_data.get("supers", []) or []
        broll_desc = shot_data.get("broll_description", "") or ""

        # Frame background
        c.setFillColor(fill_colors.get(vtype, fill_colors["presenter"]))
        c.setStrokeColor(border_colors.get(vtype, border_colors["presenter"]))
        c.setLineWidth(2)
        c.roundRect(x, y, FW, FH, 6, fill=1, stroke=1)

        # Type label (top of frame)
        c.setFillColor(colors.Color(0, 0, 0, 0.7))
        c.setFont("Helvetica-Bold", 8)
        c.drawString(x + 8, y + FH - 14, type_labels.get(vtype, "Shot"))

        # Shot number pill (top-right)
        c.setFillColor(colors.Color(0, 0, 0, 0.75))
        c.roundRect(x + FW - 55, y + FH - 16, 50, 11, 3, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 6)
        c.drawCentredString(x + FW - 30, y + FH - 12, f"SHOT {shot_num} · {duration}")

        # Supers (centered in frame)
        if supers:
            c.setFillColor(colors.Color(0, 0, 0, 0.8) if vtype != "broll" else colors.Color(1, 1, 1, 0.9))
            y_pos = y + FH / 2 + len(supers) * 6
            for sup in supers[:5]:
                c.setFont("Helvetica-Bold", 8)
                text = str(sup)[:30]
                c.drawCentredString(x + FW / 2, y_pos, text)
                y_pos -= 13

        # B-roll description at bottom of frame for b-roll shots
        if vtype == "broll" and broll_desc:
            c.setFillColor(colors.Color(1, 1, 1, 0.6))
            c.setFont("Helvetica-Oblique", 6)
            c.drawString(x + 6, y + 10, broll_desc[:50])

        # Voiceover text below frame
        c.setFillColor(colors.black)
        c.setFont("Helvetica", 6.5)
        vo_wrap = voiceover if len(voiceover) <= 200 else voiceover[:197] + "..."
        # Simple word wrap
        words = vo_wrap.split()
        line = ""
        ty = y - 10
        max_w = FW - 4
        for word in words:
            test = (line + " " + word).strip()
            if c.stringWidth(test, "Helvetica", 6.5) > max_w:
                c.drawString(x, ty, line)
                ty -= 8
                line = word
                if ty < y - TEXT_AREA + 10:
                    break
            else:
                line = test
        if line and ty >= y - TEXT_AREA + 10:
            c.drawString(x, ty, line)

    # ─── Page 1: Title + script ───
    c.setFillColor(colors.Color(0.1, 0.1, 0.12))
    c.rect(0, page_h - 40, page_w, 40, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(MARGIN, page_h - 26, title)

    # Full script on page 1
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(MARGIN, page_h - 60, "Script")
    c.setFont("Helvetica", 9)
    y = page_h - 78
    for line in script_text.split("\n"):
        if y < MARGIN + 20:
            c.showPage()
            y = page_h - MARGIN
        if not line.strip():
            y -= 6
            continue
        # Color the tag prefix
        tag_match = re.match(r"^(\[[^\]]+\])\s*(.*)", line)
        if tag_match:
            tag = tag_match.group(1)
            rest = tag_match.group(2)
            c.setFillColor(colors.Color(0.29, 0.44, 0.65))
            c.setFont("Helvetica-Bold", 9)
            c.drawString(MARGIN, y, tag)
            c.setFillColor(colors.black)
            c.setFont("Helvetica", 9)
            c.drawString(MARGIN + c.stringWidth(tag, "Helvetica-Bold", 9) + 4, y, rest[:160])
        else:
            c.drawString(MARGIN, y, line[:180])
        y -= 12

    # ─── Page 2+: Shot grid ───
    c.showPage()
    c.setFillColor(colors.Color(0.1, 0.1, 0.12))
    c.rect(0, page_h - 40, page_w, 40, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(MARGIN, page_h - 26, "Shot Breakdown")
    c.setFont("Helvetica", 9)
    c.drawString(MARGIN, page_h - 38, f"{len(visuals)} shots")

    # Grid layout
    row_height = FH + TEXT_AREA + 30
    col_width = FW + GAP
    start_x = MARGIN
    start_y = page_h - 80 - FH

    x = start_x
    y = start_y
    for i, shot in enumerate(visuals):
        if i > 0 and i % SHOTS_PER_ROW == 0:
            y -= row_height
            x = start_x
            if y < MARGIN + row_height - FH:
                c.showPage()
                c.setFillColor(colors.Color(0.1, 0.1, 0.12))
                c.rect(0, page_h - 40, page_w, 40, fill=1, stroke=0)
                c.setFillColor(colors.white)
                c.setFont("Helvetica-Bold", 14)
                c.drawString(MARGIN, page_h - 26, "Shot Breakdown (cont.)")
                y = page_h - 80 - FH
        draw_shot(shot, x, y)
        x += col_width

    c.save()
    buffer.seek(0)
    return buffer
