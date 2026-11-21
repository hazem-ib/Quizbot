import re
from typing import Any, Dict, List, Optional, Tuple
import fitz  # PyMuPDF


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


def _is_arabic_dominant(text: str) -> bool:
    if not text:
        return False
    ar_count = len(re.findall(r"[\u0600-\u06FF]", text))
    en_count = len(re.findall(r"[A-Za-z]", text))
    return ar_count > en_count


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
                # تقسيم الكلمة الطويلة جداً إن وجدت
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


def _get_font() -> fitz.Font:
    # تحميل خط يدعم العربية والإنجليزية والرموز العلمية إن توفر في النظام
    candidate_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidate_paths:
        try:
            return fitz.Font(fontfile=path)
        except Exception:
            continue
    return fitz.Font("helv")


def _get_bold_font() -> fitz.Font:
    candidate_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for path in candidate_paths:
        try:
            return fitz.Font(fontfile=path)
        except Exception:
            continue
    return fitz.Font("hebo")


def build_bank_pdf_bytes(
    bank_name: str,
    questions: List[Dict[str, Any]],
    solved: bool = False,
    settings: Optional[Dict[str, Any]] = None,
) -> bytes:
    cfg = settings or {}

    # الألوان من إعدادات الاستوديو مع قيم افتراضية نظيفة
    col_page_bg = _hex_to_rgb(cfg.get("col_page_bg"), (1.0, 1.0, 1.0))
    col_card_bg = _hex_to_rgb(cfg.get("col_card_bg"), (0.973, 0.980, 0.988))
    col_card_border = _hex_to_rgb(cfg.get("col_card_border"), (0.886, 0.910, 0.941))
    col_text = _hex_to_rgb(cfg.get("col_text"), (0.067, 0.094, 0.153))
    col_q_num = _hex_to_rgb(cfg.get("col_q_num"), (0.114, 0.306, 0.847))
    col_badge = _hex_to_rgb(cfg.get("col_badge"), (0.145, 0.388, 0.922))
    col_badge_text = _hex_to_rgb(cfg.get("col_badge_text"), (1.0, 1.0, 1.0))
    col_correct = _hex_to_rgb(cfg.get("col_correct"), (0.725, 0.110, 0.110))
    correct_underline = bool(cfg.get("correct_underline", True))
    show_expl = bool(cfg.get("show_expl", True))

    # الخطوط والمسافات
    raw_body_font = float(cfg.get("body_font") or 11.5)
    body_size = max(8.5, min(16.0, raw_body_font * 0.82))
    opt_size = max(8.0, body_size - 0.5)
    expl_size = max(7.5, body_size - 1.2)
    line_height = body_size * 1.42
    opt_line_height = opt_size * 1.38
    expl_line_height = expl_size * 1.35

    gap_q_opts = max(4.0, min(20.0, float(cfg.get("gap_q_opts") or 8.0) * 0.7))
    gap_opts = max(2.0, min(14.0, float(cfg.get("gap_opts") or 5.0) * 0.7))
    gap_questions = max(6.0, min(24.0, float(cfg.get("gap_questions") or 10.0) * 0.75))

    font_reg = _get_font()
    font_bold = _get_bold_font()

    # أبعاد صفحة A4 بالنقاط (595.28 x 841.89)
    page_w, page_h = 595.28, 841.89
    margin_x = 34.0
    margin_top = 36.0
    margin_bottom = 36.0
    content_w = page_w - 2 * margin_x
    card_pad_x = 14.0
    card_pad_y = 11.0
    inner_w = content_w - 2 * card_pad_x

    doc = fitz.open()
    letters = "ABCDEFGHIJ"
    mode_tag = "Solved" if solved else "Unsolved"

    def _new_page(is_first: bool = False) -> Tuple[fitz.Page, float]:
        pg = doc.new_page(width=page_w, height=page_h)
        if col_page_bg != (1.0, 1.0, 1.0):
            pg.draw_rect(fitz.Rect(0, 0, page_w, page_h), color=None, fill=col_page_bg)

        y_cursor = margin_top
        if is_first:
            # ترويسة المستند في الصفحة الأولى فقط
            header_h = 34.0
            pg.draw_line(
                fitz.Point(margin_x, y_cursor + header_h),
                fitz.Point(page_w - margin_x, y_cursor + header_h),
                color=col_q_num,
                width=1.4,
            )
            title_tw = fitz.TextWriter(pg.rect)
            title_tw.append((margin_x, y_cursor + 18), f"{bank_name}", font=font_bold, fontsize=14)
            title_tw.write_text(pg, color=col_text)

            meta_str = f"{mode_tag}  •  {len(questions)} Questions"
            meta_w = font_reg.text_length(meta_str, fontsize=9.5)
            meta_tw = fitz.TextWriter(pg.rect)
            meta_tw.append((page_w - margin_x - meta_w, y_cursor + 18), meta_str, font=font_reg, fontsize=9.5)
            meta_tw.write_text(pg, color=(0.38, 0.43, 0.50))

            y_cursor += header_h + 12.0
        return pg, y_cursor

    def _draw_footer(pg: fitz.Page, p_num: int, total_p: int) -> None:
        footer_text = f"{bank_name} ({mode_tag})   —   Page {p_num} of {total_p}"
        tw_w = font_reg.text_length(footer_text, fontsize=8.5)
        tw = fitz.TextWriter(pg.rect)
        tw.append(((page_w - tw_w) / 2.0, page_h - 18.0), footer_text, font=font_reg, fontsize=8.5)
        tw.write_text(pg, color=(0.48, 0.52, 0.58))

    page, cur_y = _new_page(is_first=True)

    for idx, q in enumerate(questions, start=1):
        q_raw = str(q.get("question", "")).strip()
        q_is_ar = _is_arabic_dominant(q_raw)
        opts = q.get("options") or [q.get("opt1", ""), q.get("opt2", ""), q.get("opt3", ""), q.get("opt4", "")]
        opts = [str(o).strip() for o in opts if str(o).strip() and str(o).strip() != "-"]
        c_idx = int(q.get("correct_idx", 0) or 0)
        expl_raw = str(q.get("explanation", "") or "").strip()

        # 1) حساب التفاف نص السؤال (مع ترك مساحة لشارة Q{idx}. في الزاوية)
        badge_reserve = 44.0
        q_lines = _wrap_text(f"{idx}. {q_raw}", font_bold, body_size, inner_w - badge_reserve)
        q_block_h = max(1, len(q_lines)) * line_height

        # 2) حساب التفاف كل خيار
        opt_blocks: List[Tuple[int, str, List[str], bool]] = []
        opts_total_h = 0.0
        badge_col_w = 24.0
        for o_i, o_val in enumerate(opts):
            letter = letters[o_i] if o_i < len(letters) else str(o_i + 1)
            is_corr = solved and (o_i == c_idx)
            f_use = font_bold if is_corr else font_reg
            o_lines = _wrap_text(o_val, f_use, opt_size, inner_w - badge_col_w)
            opt_blocks.append((o_i, letter, o_lines, is_corr))
            opts_total_h += max(1, len(o_lines)) * opt_line_height + (gap_opts if o_i < len(opts) - 1 else 0.0)

        # 3) حساب التفاف الشرح (في النسخة المحلولة فقط إن وُجد)
        expl_lines: List[str] = []
        expl_block_h = 0.0
        if solved and show_expl and expl_raw:
            expl_lines = _wrap_text(f"Explanation: {expl_raw}", font_reg, expl_size, inner_w)
            expl_block_h = 8.0 + len(expl_lines) * expl_line_height

        # الارتفاع الكلي للكارت بالكامل
        card_h = card_pad_y + q_block_h + gap_q_opts + opts_total_h + expl_block_h + card_pad_y

        # منع تقطيع الكارت: لو مش هيكفي في الصفحة الحالية، افتح صفحة جديدة فوراً
        if cur_y + card_h > (page_h - margin_bottom) and cur_y > margin_top + 10.0:
            page, cur_y = _new_page(is_first=False)

        # رسم خلفية وإطار الكارت
        card_rect = fitz.Rect(margin_x, cur_y, page_w - margin_x, cur_y + card_h)
        page.draw_rect(card_rect, color=col_card_border, fill=col_card_bg, width=0.8, radius=0.04)

        inner_y = cur_y + card_pad_y

        # شارة رقم السؤال Q{idx}. في الزاوية اليمنى
        q_badge_str = f"Q{idx}."
        qb_w = font_bold.text_length(q_badge_str, fontsize=body_size)
        qb_tw = fitz.TextWriter(page.rect)
        qb_tw.append((page_w - margin_x - card_pad_x - qb_w, inner_y + body_size), q_badge_str, font=font_bold, fontsize=body_size)
        qb_tw.write_text(page, color=col_q_num)

        # رسم سطور السؤال
        q_tw = fitz.TextWriter(page.rect)
        for line_i, line_str in enumerate(q_lines):
            ly = inner_y + body_size + line_i * line_height
            if q_is_ar:
                lw = font_bold.text_length(line_str, fontsize=body_size)
                lx = page_w - margin_x - card_pad_x - badge_reserve - lw
            else:
                lx = margin_x + card_pad_x
            q_tw.append((lx, ly), line_str, font=font_bold, fontsize=body_size)
        q_tw.write_text(page, color=col_text)

        inner_y += q_block_h + gap_q_opts

        # رسم الخيارات
        for o_i, letter, o_lines, is_corr in opt_blocks:
            f_use = font_bold if is_corr else font_reg
            txt_color = col_correct if is_corr else col_text

            # دائرة حرف الخيار (A, B, C, D)
            circle_cx = margin_x + card_pad_x + 7.5
            circle_cy = inner_y + opt_size * 0.65
            circle_fill = col_correct if is_corr else col_badge
            circle_txt_col = (1.0, 1.0, 1.0) if is_corr else col_badge_text

            page.draw_circle(fitz.Point(circle_cx, circle_cy), 7.2, color=None, fill=circle_fill)
            lw_let = font_bold.text_length(letter, fontsize=7.5)
            let_tw = fitz.TextWriter(page.rect)
            let_tw.append((circle_cx - lw_let / 2.0, circle_cy + 2.6), letter, font=font_bold, fontsize=7.5)
            let_tw.write_text(page, color=circle_txt_col)

            # نص الخيار + خط سفلي للإجابة الصحيحة إن مفعّل
            opt_tw = fitz.TextWriter(page.rect)
            for l_idx, l_str in enumerate(o_lines):
                oy = inner_y + opt_size + l_idx * opt_line_height
                ox = margin_x + card_pad_x + badge_col_w
                opt_tw.append((ox, oy), l_str, font=f_use, fontsize=opt_size)
                if is_corr and correct_underline:
                    uw = f_use.text_length(l_str, fontsize=opt_size)
                    page.draw_line(
                        fitz.Point(ox, oy + 2.0),
                        fitz.Point(ox + uw, oy + 2.0),
                        color=col_correct,
                        width=0.9,
                    )
            opt_tw.write_text(page, color=txt_color)

            inner_y += max(1, len(o_lines)) * opt_line_height + gap_opts

        # رسم الشرح إن وجد
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
                ex_tw.append((margin_x + card_pad_x, ey), e_str, font=font_reg, fontsize=expl_size)
            ex_tw.write_text(page, color=(0.30, 0.35, 0.42))

        cur_y += card_h + gap_questions

    # ترقيم الصفحات في التذييل
    total_pages = len(doc)
    for p_idx in range(total_pages):
        _draw_footer(doc[p_idx], p_idx + 1, total_pages)

    # حفظ مع ضغط الكائنات لتقليل الحجم لأقصى حد
    pdf_bytes = doc.tobytes(garbage=4, deflate=True, clean=True)
    doc.close()
    return pdf_bytes
