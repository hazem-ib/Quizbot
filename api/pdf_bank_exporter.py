import io
import os
import re
from typing import List, Dict, Any, Optional
import weasyprint


def _esc(val: Any) -> str:
    s = "" if val is None else str(val)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _is_rtl_text(text: str) -> bool:
    return bool(re.search(r"[\u0600-\u06FF]", text or ""))


def _letter_label(idx: int, style: str = "A") -> str:
    letters_upper = "ABCDEFGHIJ"
    letters_lower = "abcdefghij"
    arabic_letters = ["أ", "ب", "ج", "د", "هـ", "و", "ز", "ح", "ط", "ي"]
    if style == "1":
        return str(idx + 1)
    if style == "a":
        return letters_lower[idx] if idx < len(letters_lower) else str(idx + 1)
    if style == "ar":
        return arabic_letters[idx] if idx < len(arabic_letters) else str(idx + 1)
    return letters_upper[idx] if idx < len(letters_upper) else str(idx + 1)


def _render_custom_headings_html(headings: List[Dict[str, Any]], placement: str) -> str:
    if not headings:
        return ""
    matched = [h for h in headings if (h.get("placement") or "top_first") == placement]
    if not matched:
        return ""

    blocks = []
    for h in matched:
        text = (h.get("text") or "").strip()
        if not text:
            continue
        sub = (h.get("subtitle") or "").strip()
        align = h.get("align") if h.get("align") in ("right", "center", "left") else "center"
        font_px = int(h.get("font_size") or 16)
        font_px = max(11, min(32, font_px))
        color = h.get("color") or "#0f172a"
        if not re.fullmatch(r"#[0-9a-fA-F]{3,8}", str(color)):
            color = "#0f172a"
        bg = h.get("bg_color") or "transparent"
        if bg != "transparent" and not re.fullmatch(r"#[0-9a-fA-F]{3,8}", str(bg)):
            bg = "transparent"
        bordered = bool(h.get("bordered", False))
        dir_attr = "rtl" if _is_rtl_text(text) else "ltr"

        style_parts = [
            f"text-align:{align};",
            f"font-size:{font_px}px;",
            f"color:{color};",
            "font-weight:700;",
            "line-height:1.45;",
        ]
        if bg != "transparent":
            style_parts.append(f"background:{bg}; padding:8px 12px; border-radius:8px;")
        if bordered:
            style_parts.append(f"border:1.5px solid {color}; padding:8px 12px; border-radius:8px;")

        sub_html = f'<div style="font-size:{max(10, font_px - 4)}px; font-weight:500; opacity:0.8; margin-top:3px;">{_esc(sub)}</div>' if sub else ""
        blocks.append(
            f'<div class="custom-heading" dir="{dir_attr}" style="{" ".join(style_parts)}">'
            f"{_esc(text)}{sub_html}</div>"
        )

    return "\n".join(blocks)


def build_bank_pdf_bytes(
    bank_name: str,
    questions: List[Dict[str, Any]],
    solved: bool = False,
    settings: Optional[Dict[str, Any]] = None,
) -> bytes:
    cfg = settings or {}
    theme = cfg.get("theme") or "academic"
    columns = 2 if int(cfg.get("columns") or 1) == 2 else 1
    option_layout = cfg.get("option_layout") or "vertical"
    option_style = cfg.get("option_style") or "A"

    body_font_px = float(cfg.get("body_font_size") or 12.5)
    body_font_px = max(9.5, min(20.0, body_font_px))

    margin_mm = float(cfg.get("margin_mm") or 14)
    margin_mm = max(8.0, min(28.0, margin_mm))

    gap_q_opts_px = float(cfg.get("gap_q_opts") or 8)
    gap_q_opts_px = max(2.0, min(32.0, gap_q_opts_px))

    gap_opts_px = float(cfg.get("gap_opts") or 5)
    gap_opts_px = max(1.0, min(24.0, gap_opts_px))

    gap_questions_px = float(cfg.get("gap_questions") or 16)
    gap_questions_px = max(6.0, min(42.0, gap_questions_px))

    show_explanations = bool(cfg.get("show_explanations", True)) if solved else False
    show_page_numbers = bool(cfg.get("show_page_numbers", True))
    show_mode_banner = bool(cfg.get("show_mode_banner", True))
    start_num = int(cfg.get("start_number") or 1)
    if start_num < 1:
        start_num = 1

    custom_headings = cfg.get("custom_headings") or []
    if not isinstance(custom_headings, list):
        custom_headings = []

    before_q_map: Dict[int, List[Dict[str, Any]]] = {}
    for h in custom_headings:
        pl = str(h.get("placement") or "")
        m = re.fullmatch(r"before_q_(\d+)", pl)
        if m:
            q_target = int(m.group(1))
            before_q_map.setdefault(q_target, []).append(h)

    top_first_html = _render_custom_headings_html(custom_headings, "top_first")
    running_header_html = _render_custom_headings_html(custom_headings, "running_header")
    running_footer_html = _render_custom_headings_html(custom_headings, "running_footer")

    # ألوان وأنماط القوالب الاحترافية
    if theme == "modern":
        accent = "#1d4ed8"
        q_card_bg = "#f8fafc"
        q_card_border = "#e2e8f0"
        q_card_pad = "11px 13px"
        q_card_radius = "8px"
    elif theme == "exam":
        accent = "#0f172a"
        q_card_bg = "#ffffff"
        q_card_border = "#cbd5e1"
        q_card_pad = "10px 12px"
        q_card_radius = "4px"
    else:  # academic
        accent = "#1e3a8a"
        q_card_bg = "transparent"
        q_card_border = "transparent"
        q_card_pad = "2px 0"
        q_card_radius = "0px"

    mode_label = "نسخة محلولة (Solved Key)" if solved else "نسخة اختبار (Unsolved)"
    mode_badge_bg = "#fef2f2" if solved else "#eff6ff"
    mode_badge_fg = "#b91c1c" if solved else "#1d4ed8"
    mode_badge_border = "#fecaca" if solved else "#bfdbfe"

    page_number_css = ""
    if show_page_numbers or running_header_html or running_footer_html:
        top_box = f'"{_esc(bank_name)}"' if not running_header_html else "none"
        bottom_box = '"صفحة " counter(page) " من " counter(pages)' if show_page_numbers else "none"
        page_number_css = f"""
        @page {{
            size: A4;
            margin: {margin_mm + 8}mm {margin_mm}mm {margin_mm + 8}mm {margin_mm}mm;
            @top-center {{
                content: {top_box};
                font-size: 9pt;
                color: #64748b;
            }}
            @bottom-center {{
                content: {bottom_box};
                font-size: 9pt;
                color: #64748b;
            }}
        }}
        """
    else:
        page_number_css = f"""
        @page {{
            size: A4;
            margin: {margin_mm}mm;
        }}
        """

    grid_cols_css = "1fr 1fr" if columns == 2 else "1fr"
    opt_display_css = "grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: " + f"{gap_opts_px}px {gap_opts_px * 1.6}px;" if option_layout == "grid" else f"block;"

    questions_html_parts = []
    for idx, q in enumerate(questions):
        q_num = start_num + idx
        if q_num in before_q_map:
            questions_html_parts.append(_render_custom_headings_html(before_q_map[q_num], f"before_q_{q_num}"))

        q_text = (q.get("question") or "").strip()
        q_dir = "rtl" if _is_rtl_text(q_text) else "ltr"
        opts = q.get("options") or []
        if not isinstance(opts, list) or len(opts) < 2:
            opts = [q.get("opt1", "-"), q.get("opt2", "-"), q.get("opt3", "-"), q.get("opt4", "-")]
            opts = [str(x).strip() for x in opts if str(x).strip() and str(x).strip() != "-"]

        c_idx = q.get("correct_idx", 0)
        if not isinstance(c_idx, int) or not (0 <= c_idx < len(opts)):
            c_idx = 0

        expl_text = (q.get("explanation") or "").strip()

        opts_items_html = []
        for o_idx, opt_val in enumerate(opts):
            opt_str = str(opt_val).strip()
            lbl = _letter_label(o_idx, option_style)
            is_correct = solved and (o_idx == c_idx)
            opt_dir = "rtl" if _is_rtl_text(opt_str) else "ltr"

            if is_correct:
                opt_style = (
                    f"color: #dc2626; text-decoration: underline; text-underline-offset: 3px; "
                    f"font-weight: 700; margin-bottom: {0 if option_layout == 'grid' else gap_opts_px}px;"
                )
            else:
                opt_style = (
                    f"color: #1e293b; font-weight: 400; "
                    f"margin-bottom: {0 if option_layout == 'grid' else gap_opts_px}px;"
                )

            opts_items_html.append(
                f'<div class="opt-row" dir="{opt_dir}" style="{opt_style}">'
                f'<span class="opt-lbl">{_esc(lbl)}.</span> '
                f'<span class="opt-txt">{_esc(opt_str)}</span>'
                f"</div>"
            )

        expl_html = ""
        if show_explanations and expl_text:
            ex_dir = "rtl" if _is_rtl_text(expl_text) else "ltr"
            expl_html = (
                f'<div class="q-expl" dir="{ex_dir}" style="margin-top: {max(6, gap_q_opts_px)}px; '
                f'padding: 7px 10px; background: #fffbeb; border-right: 3px solid #d97706; '
                f'border-radius: 6px; font-size: {max(9.5, body_font_px - 1.2)}px; color: #78350f;">'
                f"<strong>Explanation:</strong> {_esc(expl_text)}"
                f"</div>"
            )

        q_block_html = (
            f'<div class="q-card" style="margin-bottom: {gap_questions_px}px; padding: {q_card_pad}; '
            f'background: {q_card_bg}; border: 1px solid {q_card_border}; border-radius: {q_card_radius}; '
            f'break-inside: avoid; page-break-inside: avoid;">'
            f'<div class="q-title" dir="{q_dir}" style="font-size: {body_font_px}px; font-weight: 700; '
            f'color: #0f172a; margin-bottom: {gap_q_opts_px}px; line-height: 1.55;">'
            f'<span class="q-num" style="color: {accent}; margin-inline-end: 6px;">Q{q_num}.</span>'
            f'<span class="q-body">{_esc(q_text)}</span>'
            f"</div>"
            f'<div class="q-opts" style="display: {opt_display_css} padding-inline-start: 10px; font-size: {body_font_px}px; line-height: 1.48;">'
            f'{"".join(opts_items_html)}'
            f"</div>"
            f"{expl_html}"
            f"</div>"
        )
        questions_html_parts.append(q_block_html)

    banner_html = ""
    if show_mode_banner:
        banner_html = (
            f'<div class="doc-header" style="border-bottom: 2px solid {accent}; padding-bottom: 10px; margin-bottom: 14px; '
            f'display: flex; justify-content: space-between; align-items: center; gap: 10px;">'
            f'<div>'
            f'<div style="font-size: 16px; font-weight: 800; color: #0f172a;">{_esc(bank_name)}</div>'
            f'<div style="font-size: 10px; color: #64748b; margin-top: 2px;">Total Questions: {len(questions)}</div>'
            f'</div>'
            f'<span style="background: {mode_badge_bg}; color: {mode_badge_fg}; border: 1px solid {mode_badge_border}; '
            f'padding: 4px 10px; border-radius: 999px; font-size: 10.5px; font-weight: 700;">{_esc(mode_label)}</span>'
            f'</div>'
        )

    html_doc = f"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8" />
<style>
  {page_number_css}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 0;
    font-family: "DejaVu Sans", "Noto Sans Arabic", "Arial", sans-serif;
    color: #0f172a;
    background: #ffffff;
  }}
  .custom-heading {{
    margin-bottom: 12px;
    break-inside: avoid;
    page-break-inside: avoid;
  }}
  .questions-container {{
    display: grid;
    grid-template-columns: {grid_cols_css};
    gap: {14 if columns == 2 else 0}px {18 if columns == 2 else 0}px;
    align-items: start;
  }}
  .opt-lbl {{
    font-weight: 700;
  }}
</style>
</head>
<body>
  {running_header_html}
  {top_first_html}
  {banner_html}
  <div class="questions-container">
    {"".join(questions_html_parts)}
  </div>
  {running_footer_html}
</body>
</html>"""

    pdf_buffer = io.BytesIO()
    weasyprint.HTML(string=html_doc).write_pdf(pdf_buffer)
    return pdf_buffer.getvalue()
