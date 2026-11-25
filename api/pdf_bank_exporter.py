import os
import re
import textwrap
from typing import Optional, List, Dict, Any

import fitz  # PyMuPDF
import arabic_reshaper
from bidi.algorithm import get_display

_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")


def _has_arabic(text: str) -> bool:
    return bool(_ARABIC_RE.search(text or ""))


def shape_text(text: str) -> str:
    """Reshape Arabic ligatures and apply BiDi reordering so PyMuPDF renders connected RTL text."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    if not _has_arabic(raw):
        return raw
    reshaped = arabic_reshaper.reshape(raw)
    return get_display(reshaped)


def _find_arabic_font() -> Optional[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "fonts", "Amiri-Regular.ttf"),
        os.path.join(here, "fonts", "NotoNaskhArabic-Regular.ttf"),
        os.path.join(here, "fonts", "Cairo-Regular.ttf"),
        os.path.join(here, "..", "fonts", "Amiri-Regular.ttf"),
        os.path.join(here, "..", "fonts", "NotoNaskhArabic-Regular.ttf"),
        os.path.join(here, "..", "fonts", "Cairo-Regular.ttf"),
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
        "/usr/share/fonts/truetype/amiri/Amiri-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _wrap_lines(text: str, max_chars: int) -> List[str]:
    paragraphs = str(text or "").splitlines() or [""]
    lines: List[str] = []
    for para in paragraphs:
        stripped = para.strip()
        if not stripped:
            lines.append("")
            continue
        wrapped = textwrap.wrap(
            stripped,
            width=max_chars,
            break_long_words=True,
            break_on_hyphens=False,
        )
        lines.extend(wrapped or [""])
    return lines


def _extract_options(q: Dict[str, Any]) -> List[str]:
    opts = q.get("options")
    if isinstance(opts, list) and len(opts) >= 2:
        cleaned = [str(x).strip() for x in opts if str(x).strip()]
        if len(cleaned) >= 2:
            return cleaned[:10]
    fallback = [
        str(q.get(k, "")).strip()
        for k in ("opt1", "opt2", "opt3", "opt4")
        if str(q.get(k, "")).strip() not in ("", "-")
    ]
    return fallback if len(fallback) >= 2 else [str(q.get("opt1") or "-"), str(q.get("opt2") or "-")]


def build_bank_pdf_bytes(
    bank_name: str,
    questions: list,
    solved: bool = False,
    settings: Optional[dict] = None,
) -> bytes:
    cfg = settings or {}
    font_size = float(cfg.get("font_size", 11.5))
    show_explanations = bool(cfg.get("show_explanations", True))
    brand_title = str(cfg.get("header_title") or bank_name or "بنك الأسئلة").strip()

    doc = fitz.open()
    page_w, page_h = fitz.paper_size("a4")
    margin_x = 42.0
    margin_top = 48.0
    margin_bottom = 44.0
    usable_w = page_w - 2 * margin_x

    font_path = _find_arabic_font()
    font_name = "arfont" if font_path else "helv"

    def _register_font(page: fitz.Page):
        if font_path:
            page.insert_font(fontname=font_name, fontfile=font_path)

    def _new_page(is_first: bool = False) -> tuple[fitz.Page, float]:
        page = doc.new_page(width=page_w, height=page_h)
        _register_font(page)
        y = margin_top

        # Header bar
        header_rect = fitz.Rect(margin_x, y - 14, page_w - margin_x, y + 26)
        page.draw_rect(header_rect, color=(0.18, 0.24, 0.34), fill=(0.95, 0.96, 0.98), width=0.8)

        mode_badge = "نموذج الإجابة (Solved)" if solved else "نموذج الأسئلة (Unsolved)"
        title_is_ar = _has_arabic(brand_title)
        title_display = shape_text(brand_title)
        badge_display = shape_text(mode_badge)

        if title_is_ar:
            page.insert_textbox(
                fitz.Rect(margin_x + 10, y - 8, page_w - margin_x - 10, y + 12),
                title_display,
                fontname=font_name,
                fontsize=13.5,
                color=(0.08, 0.12, 0.20),
                align=fitz.TEXT_ALIGN_RIGHT,
            )
            page.insert_textbox(
                fitz.Rect(margin_x + 10, y + 9, page_w - margin_x - 10, y + 24),
                badge_display,
                fontname=font_name,
                fontsize=9.5,
                color=(0.12, 0.45, 0.28) if solved else (0.35, 0.40, 0.48),
                align=fitz.TEXT_ALIGN_LEFT,
            )
        else:
            page.insert_textbox(
                fitz.Rect(margin_x + 10, y - 8, page_w - margin_x - 10, y + 12),
                title_display,
                fontname=font_name,
                fontsize=13.5,
                color=(0.08, 0.12, 0.20),
                align=fitz.TEXT_ALIGN_LEFT,
            )
            page.insert_textbox(
                fitz.Rect(margin_x + 10, y + 9, page_w - margin_x - 10, y + 24),
                badge_display,
                fontname=font_name,
                fontsize=9.5,
                color=(0.12, 0.45, 0.28) if solved else (0.35, 0.40, 0.48),
                align=fitz.TEXT_ALIGN_RIGHT,
            )

        y += 42.0
        if is_first:
            meta_line = shape_text(f"إجمالي الأسئلة: {len(questions)}")
            page.insert_textbox(
                fitz.Rect(margin_x, y - 4, page_w - margin_x, y + 14),
                meta_line,
                fontname=font_name,
                fontsize=9.5,
                color=(0.40, 0.45, 0.52),
                align=fitz.TEXT_ALIGN_RIGHT,
            )
            y += 20.0
        return page, y

    page, cur_y = _new_page(is_first=True)
    ar_letters = ["أ", "ب", "ج", "د", "هـ", "و", "ز", "ح", "ط", "ي"]
    en_letters = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]

    for q_idx, q in enumerate(questions, start=1):
        q_raw = str(q.get("question") or "").strip()
        opts = _extract_options(q)
        c_idx = q.get("correct_idx") if isinstance(q.get("correct_idx"), int) else 0
        expl_raw = str(q.get("explanation") or "").strip()

        q_is_ar = _has_arabic(q_raw) or any(_has_arabic(o) for o in opts)
        chars_per_line = max(42, int(usable_w / (font_size * 0.54)))

        q_full_text = f"{q_idx}. {q_raw}"
        q_lines = _wrap_lines(q_full_text, chars_per_line)

        opt_blocks: List[tuple[bool, List[str]]] = []
        for o_i, opt_val in enumerate(opts):
            is_correct = solved and (o_i == c_idx)
            label = ar_letters[o_i] if q_is_ar else en_letters[o_i]
            prefix = f"✓ ({label}) " if is_correct else f"({label}) "
            opt_lines = _wrap_lines(f"{prefix}{opt_val}", chars_per_line - 4)
            opt_blocks.append((is_correct, opt_lines))

        expl_lines: List[str] = []
        if solved and show_explanations and expl_raw:
            expl_lines = _wrap_lines(f"التوضيح: {expl_raw}", chars_per_line - 4)

        line_h = font_size * 1.52
        needed_h = (
            len(q_lines) * line_h
            + sum(len(ol) * (line_h * 0.94) + 3 for _, ol in opt_blocks)
            + (len(expl_lines) * (line_h * 0.90) + 12 if expl_lines else 0)
            + 20
        )

        if cur_y + needed_h > page_h - margin_bottom and cur_y > margin_top + 60:
            page, cur_y = _new_page(is_first=False)

        align_mode = fitz.TEXT_ALIGN_RIGHT if q_is_ar else fitz.TEXT_ALIGN_LEFT

        # Question stem
        for line in q_lines:
            if cur_y + line_h > page_h - margin_bottom:
                page, cur_y = _new_page(is_first=False)
            page.insert_textbox(
                fitz.Rect(margin_x, cur_y, page_w - margin_x, cur_y + line_h + 2),
                shape_text(line),
                fontname=font_name,
                fontsize=font_size,
                color=(0.07, 0.09, 0.14),
                align=align_mode,
            )
            cur_y += line_h

        cur_y += 3.0

        # Options
        opt_font_size = max(9.5, font_size - 0.7)
        opt_line_h = opt_font_size * 1.48
        for is_correct, opt_lines in opt_blocks:
            block_h = len(opt_lines) * opt_line_h + 4
            if cur_y + block_h > page_h - margin_bottom:
                page, cur_y = _new_page(is_first=False)

            if is_correct:
                highlight_rect = fitz.Rect(margin_x + 4, cur_y - 1, page_w - margin_x - 4, cur_y + block_h - 1)
                page.draw_rect(
                    highlight_rect,
                    color=(0.13, 0.55, 0.34),
                    fill=(0.90, 0.98, 0.93),
                    width=0.7,
                )

            text_color = (0.06, 0.42, 0.24) if is_correct else (0.20, 0.24, 0.30)
            for line in opt_lines:
                page.insert_textbox(
                    fitz.Rect(margin_x + 10, cur_y + 1, page_w - margin_x - 10, cur_y + opt_line_h + 2),
                    shape_text(line),
                    fontname=font_name,
                    fontsize=opt_font_size,
                    color=text_color,
                    align=align_mode,
                )
                cur_y += opt_line_h
            cur_y += 3.0

        # Explanation box (Solved PDF only)
        if expl_lines:
            expl_font_size = max(9.0, font_size - 1.2)
            expl_line_h = expl_font_size * 1.45
            expl_h = len(expl_lines) * expl_line_h + 8
            if cur_y + expl_h > page_h - margin_bottom:
                page, cur_y = _new_page(is_first=False)

            expl_align = fitz.TEXT_ALIGN_RIGHT if _has_arabic(expl_raw) else fitz.TEXT_ALIGN_LEFT
            expl_rect = fitz.Rect(margin_x + 6, cur_y, page_w - margin_x - 6, cur_y + expl_h)
            page.draw_rect(expl_rect, color=(0.75, 0.80, 0.88), fill=(0.96, 0.97, 0.99), width=0.5)
            cur_y += 4.0
            for line in expl_lines:
                page.insert_textbox(
                    fitz.Rect(margin_x + 12, cur_y, page_w - margin_x - 12, cur_y + expl_line_h + 2),
                    shape_text(line),
                    fontname=font_name,
                    fontsize=expl_font_size,
                    color=(0.24, 0.30, 0.40),
                    align=expl_align,
                )
                cur_y += expl_line_h
            cur_y += 5.0

        # Divider between questions
        cur_y += 5.0
        page.draw_line(
            fitz.Point(margin_x, cur_y),
            fitz.Point(page_w - margin_x, cur_y),
            color=(0.86, 0.88, 0.92),
            width=0.5,
        )
        cur_y += 10.0

    # Page numbers footer
    total_pages = len(doc)
    for p_idx, p in enumerate(doc, start=1):
        _register_font(p)
        footer_txt = shape_text(f"صفحة {p_idx} من {total_pages}")
        p.insert_textbox(
            fitz.Rect(margin_x, page_h - margin_bottom + 10, page_w - margin_x, page_h - 14),
            footer_txt,
            fontname=font_name,
            fontsize=8.5,
            color=(0.48, 0.52, 0.58),
            align=fitz.TEXT_ALIGN_CENTER,
        )

    pdf_bytes = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return pdf_bytes
