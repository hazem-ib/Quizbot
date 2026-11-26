import os
import re
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF
import arabic_reshaper
from bidi.algorithm import get_display

_ARABIC_CHAR_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")
_LATIN_CHAR_RE = re.compile(r"[A-Za-z]")


def _has_arabic(text: str) -> bool:
    return bool(_ARABIC_CHAR_RE.search(text or ""))


def _is_arabic_dominant(text: str) -> bool:
    if not text:
        return False
    ar_count = len(_ARABIC_CHAR_RE.findall(text))
    en_count = len(_LATIN_CHAR_RE.findall(text))
    return ar_count > en_count


def _shape_single_line(line: str) -> str:
    """Shape a single line safely without reversing multi-line order."""
    s = str(line or "")
    if not s or not _has_arabic(s):
        return s
    reshaped = arabic_reshaper.reshape(s)
    return get_display(reshaped)


def _hex_to_rgb(hex_color: Optional[str], default: Tuple[float, float, float]) -> Tuple[float, float, float]:
    if not hex_color or not isinstance(hex_color, str):
        return default
    h = hex_color.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    if len(h) != 6:
        return default
    try:
        r = int(h[0:2], 16) / 255.0
        g = int(h[2:4], 16) / 255.0
        b = int(h[4:6], 16) / 255.0
        return (r, g, b)
    except ValueError:
        return default


def _clean_question_text(raw: str) -> str:
    txt = str(raw or "").strip()
    txt = re.sub(
        r"^\s*(?:Q\.?\s*|#)?\d+\s*(?:[\.\)\:\-–—]+|\s+(?=[\u0600-\u06FFA-Za-z]))\s*",
        "",
        txt,
        flags=re.IGNORECASE,
    )
    txt = re.sub(r"[\.…]{2,}\s*$", "", txt).strip()
    return txt


def _wrap_text(text: str, font: fitz.Font, fontsize: float, max_width: float) -> List[str]:
    if not text:
        return []
    paragraphs = str(text).splitlines() or [str(text)]
    lines: List[str] = []
    for para in paragraphs:
        words = para.split(" ")
        current = ""
        for w in words:
            candidate = f"{current} {w}".strip() if current else w
            if font.text_length(candidate, fontsize=fontsize) <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                if font.text_length(w, fontsize=fontsize) <= max_width:
                    current = w
                else:
                    chunk = ""
                    for ch in w:
                        if font.text_length(chunk + ch, fontsize=fontsize) <= max_width:
                            chunk += ch
                        else:
                            if chunk:
                                lines.append(chunk)
                            chunk = ch
                    current = chunk
        if current:
            lines.append(current)
    return lines if lines else [""]


def _pick_font(style: str, bold: bool = False) -> fitz.Font:
    here = os.path.dirname(os.path.abspath(__file__))
    style_key = (style or "soft_rounded").strip().lower()

    local_candidates: List[str] = []
    if style_key == "bold_friendly":
        local_candidates = [
            os.path.join(here, "fonts", "Cairo-Bold.ttf"),
            os.path.join(here, "fonts", "NotoSansArabic-Bold.ttf"),
            os.path.join(here, "fonts", "Amiri-Bold.ttf"),
            os.path.join(here, "fonts", "Amiri-Regular.ttf"),
        ]
    elif style_key == "clean_sans":
        local_candidates = [
            os.path.join(here, "fonts", "NotoSansArabic-Bold.ttf" if bold else "NotoSansArabic-Regular.ttf"),
            os.path.join(here, "fonts", "Cairo-Bold.ttf" if bold else "Cairo-Regular.ttf"),
            os.path.join(here, "fonts", "Amiri-Bold.ttf" if bold else "Amiri-Regular.ttf"),
        ]
    else:  # soft_rounded
        local_candidates = [
            os.path.join(here, "fonts", "Cairo-Bold.ttf" if bold else "Cairo-Regular.ttf"),
            os.path.join(here, "fonts", "NotoSansArabic-Bold.ttf" if bold else "NotoSansArabic-Regular.ttf"),
            os.path.join(here, "fonts", "Amiri-Bold.ttf" if bold else "Amiri-Regular.ttf"),
            os.path.join(here, "fonts", "NotoNaskhArabic-Regular.ttf"),
        ]

    if style_key == "soft_rounded":
        sys_candidates = (
            [
                "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
            ]
            if bold
            else [
                "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
            ]
        )
    elif style_key == "bold_friendly":
        sys_candidates = [
            "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]
    else:  # clean_sans
        sys_candidates = (
            [
                "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            ]
            if bold
            else [
                "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            ]
        )

    for path in local_candidates + sys_candidates:
        if os.path.exists(path):
            try:
                return fitz.Font(fontfile=path)
            except Exception:
                continue

    return fitz.Font("hebo" if bold else "helv")


def _option_label(idx: int, style: str) -> str:
    up = "ABCDEFGHIJ"
    lo = "abcdefghij"
    ar = ["أ", "ب", "ج", "د", "هـ", "و", "ز", "ح", "ط", "ي"]
    st = (style or "A").strip()
    if st == "1":
        return str(idx + 1)
    if st == "a":
        return lo[idx] if idx < len(lo) else str(idx + 1)
    if st == "ar":
        return ar[idx] if idx < len(ar) else str(idx + 1)
    return up[idx] if idx < len(up) else str(idx + 1)


def build_bank_pdf_bytes(
    bank_name: str,
    questions: List[Dict[str, Any]],
    solved: bool = False,
    settings: Optional[Dict[str, Any]] = None,
) -> bytes:
    cfg = settings or {}

    col_page_bg = _hex_to_rgb(cfg.get("col_page_bg"), (0.996, 0.992, 0.984))
    col_card_bg = _hex_to_rgb(cfg.get("col_card_bg"), (0.965, 0.978, 0.992))
    col_card_border = _hex_to_rgb(cfg.get("col_card_border"), (0.855, 0.894, 0.941))
    col_text = _hex_to_rgb(cfg.get("col_text"), (0.118, 0.161, 0.231))
    col_q_num = _hex_to_rgb(cfg.get("col_q_num"), (0.145, 0.388, 0.922))
    col_badge = _hex_to_rgb(cfg.get("col_badge"), (0.859, 0.918, 0.996))
    col_badge_text = _hex_to_rgb(cfg.get("col_badge_text"), (0.114, 0.306, 0.847))
    col_correct = _hex_to_rgb(cfg.get("col_correct"), (0.020, 0.588, 0.412))
    correct_underline = bool(cfg.get("correct_underline", True))
    show_expl = bool(cfg.get("show_expl", True))
    opt_style = str(cfg.get("opt_style") or "A")
    font_style = str(cfg.get("font_style") or "soft_rounded")

    raw_body_font = float(cfg.get("body_font") or 13.0)
    body_size = max(10.0, min(17.5, raw_body_font * 0.92))
    opt_size = max(9.5, body_size - 0.3)
    expl_size = max(9.0, body_size - 1.2)
    line_height = body_size * 1.48
    opt_line_height = opt_size * 1.44
    expl_line_height = expl_size * 1.40

    gap_q_opts = max(6.0, min(22.0, float(cfg.get("gap_q_opts") or 10.0) * 0.85))
    gap_opts = max(4.0, min(16.0, float(cfg.get("gap_opts") or 6.0) * 0.85))
    gap_questions = max(8.0, min(26.0, float(cfg.get("gap_questions") or 13.0) * 0.85))

    font_reg = _pick_font(font_style, bold=False)
    font_bold = _pick_font(font_style, bold=True)

    page_w, page_h = 595.28, 841.89
    margin_x = 34.0
    margin_top = 36.0
    margin_bottom = 36.0
    content_w = page_w - 2 * margin_x
    card_pad_x = 15.0
    card_pad_y = 12.0
    inner_w = content_w - 2 * card_pad_x

    doc = fitz.open()
    clean_bank_name = str(bank_name or "Question Bank").strip()
    bank_is_ar = _is_arabic_dominant(clean_bank_name)

    def _new_page(is_first: bool = False) -> Tuple[fitz.Page, float]:
        pg = doc.new_page(width=page_w, height=page_h)
        if col_page_bg != (1.0, 1.0, 1.0):
            pg.draw_rect(fitz.Rect(0, 0, page_w, page_h), color=None, fill=col_page_bg)

        y_cursor = margin_top
        if is_first:
            header_h = 34.0
            pg.draw_line(
                fitz.Point(margin_x, y_cursor + header_h),
                fitz.Point(page_w - margin_x, y_cursor + header_h),
                color=col_q_num,
                width=1.5,
            )
            title_shaped = _shape_single_line(clean_bank_name)
            title_tw = fitz.TextWriter(pg.rect)
            if bank_is_ar:
                tw_title_w = font_bold.text_length(title_shaped, fontsize=14.5)
                title_tw.append((page_w - margin_x - tw_title_w, y_cursor + 19), title_shaped, font=font_bold, fontsize=14.5)
            else:
                title_tw.append((margin_x, y_cursor + 19), title_shaped, font=font_bold, fontsize=14.5)
            title_tw.write_text(pg, color=col_text)

            meta_raw = f"{len(questions)} سؤال" if bank_is_ar else f"{len(questions)} Questions"
            meta_str = _shape_single_line(meta_raw)
            meta_w = font_reg.text_length(meta_str, fontsize=9.5)
            meta_tw = fitz.TextWriter(pg.rect)
            if bank_is_ar:
                meta_tw.append((margin_x, y_cursor + 19), meta_str, font=font_reg, fontsize=9.5)
            else:
                meta_tw.append((page_w - margin_x - meta_w, y_cursor + 19), meta_str, font=font_reg, fontsize=9.5)
            meta_tw.write_text(pg, color=(0.38, 0.43, 0.50))

            y_cursor += header_h + 13.0
        return pg, y_cursor

    def _draw_footer(pg: fitz.Page, p_num: int, total_p: int) -> None:
        footer_text = _shape_single_line(f"{clean_bank_name}   •   {p_num} / {total_p}")
        tw_w = font_reg.text_length(footer_text, fontsize=8.5)
        tw = fitz.TextWriter(pg.rect)
        tw.append(((page_w - tw_w) / 2.0, page_h - 18.0), footer_text, font=font_reg, fontsize=8.5)
        tw.write_text(pg, color=(0.48, 0.52, 0.58))

    page, cur_y = _new_page(is_first=True)

    for idx, q in enumerate(questions, start=1):
        q_raw = _clean_question_text(q.get("question", ""))
        q_is_ar = _is_arabic_dominant(q_raw)
        opts = q.get("options") or [q.get("opt1", ""), q.get("opt2", ""), q.get("opt3", ""), q.get("opt4", "")]
        opts = [str(o).strip() for o in opts if str(o).strip() and str(o).strip() != "-"]
        c_idx = int(q.get("correct_idx", 0) or 0)
        expl_raw = str(q.get("explanation", "") or "").strip()

        q_full_str = f"{idx}. {q_raw}"
        q_raw_lines = _wrap_text(q_full_str, font_bold, body_size, inner_w)
        q_shaped_lines = [_shape_single_line(ln) for ln in q_raw_lines]
        q_block_h = max(1, len(q_shaped_lines)) * line_height

        opt_blocks: List[Tuple[int, str, List[Tuple[str, bool]], bool]] = []
        opts_total_h = 0.0
        badge_col_w = 26.0
        for o_i, o_val in enumerate(opts):
            letter = _shape_single_line(_option_label(o_i, opt_style))
            is_corr = solved and (o_i == c_idx)
            f_use = font_bold if is_corr else font_reg
            o_is_ar = _is_arabic_dominant(o_val)
            o_raw_lines = _wrap_text(o_val, f_use, opt_size, inner_w - badge_col_w)
            o_shaped_lines = [(_shape_single_line(ln), o_is_ar) for ln in o_raw_lines]
            opt_blocks.append((o_i, letter, o_shaped_lines, is_corr))
            opts_total_h += max(1, len(o_shaped_lines)) * opt_line_height + (gap_opts if o_i < len(opts) - 1 else 0.0)

        expl_is_ar = _is_arabic_dominant(expl_raw)
        expl_lines: List[str] = []
        expl_block_h = 0.0
        if solved and show_expl and expl_raw:
            expl_raw_lines = _wrap_text(expl_raw, font_reg, expl_size, inner_w)
            expl_lines = [_shape_single_line(ln) for ln in expl_raw_lines]
            expl_block_h = 8.0 + len(expl_lines) * expl_line_height

        card_h = card_pad_y + q_block_h + gap_q_opts + opts_total_h + expl_block_h + card_pad_y

        if cur_y + card_h > (page_h - margin_bottom) and cur_y > margin_top + 10.0:
            page, cur_y = _new_page(is_first=False)

        card_rect = fitz.Rect(margin_x, cur_y, page_w - margin_x, cur_y + card_h)
        page.draw_rect(card_rect, color=col_card_border, fill=col_card_bg, width=0.9, radius=0.06)

        inner_y = cur_y + card_pad_y

        q_tw = fitz.TextWriter(page.rect)
        for line_i, line_str in enumerate(q_shaped_lines):
            ly = inner_y + body_size + line_i * line_height
            if q_is_ar:
                lw = font_bold.text_length(line_str, fontsize=body_size)
                lx = page_w - margin_x - card_pad_x - lw
            else:
                lx = margin_x + card_pad_x
            q_tw.append((lx, ly), line_str, font=font_bold, fontsize=body_size)
        q_tw.write_text(page, color=col_text)

        inner_y += q_block_h + gap_q_opts

        for o_i, letter, o_lines, is_corr in opt_blocks:
            f_use = font_bold if is_corr else font_reg
            txt_color = col_correct if is_corr else col_text

            circle_cx = margin_x + card_pad_x + 8.5
            circle_cy = inner_y + opt_size * 0.65
            circle_fill = col_correct if is_corr else col_badge
            circle_txt_col = (1.0, 1.0, 1.0) if is_corr else col_badge_text

            page.draw_circle(fitz.Point(circle_cx, circle_cy), 8.0, color=None, fill=circle_fill)
            lw_let = font_bold.text_length(letter, fontsize=8.0)
            let_tw = fitz.TextWriter(page.rect)
            let_tw.append((circle_cx - lw_let / 2.0, circle_cy + 2.7), letter, font=font_bold, fontsize=8.0)
            let_tw.write_text(page, color=circle_txt_col)

            opt_tw = fitz.TextWriter(page.rect)
            for l_idx, (l_str, l_is_ar) in enumerate(o_lines):
                oy = inner_y + opt_size + l_idx * opt_line_height
                if l_is_ar:
                    lw = f_use.text_length(l_str, fontsize=opt_size)
                    ox = page_w - margin_x - card_pad_x - lw
                else:
                    ox = margin_x + card_pad_x + badge_col_w
                opt_tw.append((ox, oy), l_str, font=f_use, fontsize=opt_size)
                if is_corr and correct_underline:
                    uw = f_use.text_length(l_str, fontsize=opt_size)
                    page.draw_line(
                        fitz.Point(ox, oy + 2.2),
                        fitz.Point(ox + uw, oy + 2.2),
                        color=col_correct,
                        width=1.0,
                    )
            opt_tw.write_text(page, color=txt_color)

            inner_y += max(1, len(o_lines)) * opt_line_height + gap_opts

        if expl_lines:
            inner_y += 2.0
            page.draw_line(
                fitz.Point(margin_x + card_pad_x, inner_y),
                fitz.Point(page_w - margin_x - card_pad_x, inner_y),
                color=col_card_border,
                width=0.6,
            )
            inner_y += 5.0
            ex_tw = fitz.TextWriter(page.rect)
            for e_i, e_str in enumerate(expl_lines):
                ey = inner_y + expl_size + e_i * expl_line_height
                if expl_is_ar:
                    ew = font_reg.text_length(e_str, fontsize=expl_size)
                    ex_x = page_w - margin_x - card_pad_x - ew
                else:
                    ex_x = margin_x + card_pad_x
                ex_tw.append((ex_x, ey), e_str, font=font_reg, fontsize=expl_size)
            ex_tw.write_text(page, color=(0.30, 0.35, 0.42))

        cur_y += card_h + gap_questions

    total_pages = len(doc)
    for p_idx in range(total_pages):
        _draw_footer(doc[p_idx], p_idx + 1, total_pages)

    pdf_bytes = doc.tobytes(garbage=4, deflate=True, clean=True)
    doc.close()
    return pdf_bytes
