import re
from typing import Any, Dict, List, Optional, Tuple
import fitz  # PyMuPDF


Q_START_RE = re.compile(
    r"^(?:Q(?:uestion)?\s*\d+[:.\-)\s]+|\d+\s*[.)\-:])\s*(.+)",
    re.IGNORECASE,
)
OPT_START_RE = re.compile(
    r"^([A-Da-dأ-د])\s*[.)\-:]\s*(.+)"
)
EXPL_START_RE = re.compile(
    r"^(?:Explanation|Rationale|Note|Answer|شرح|توضيح|ملاحظة)\s*[:\-]\s*(.+)",
    re.IGNORECASE,
)
ESSAY_SECTION_RE = re.compile(
    r"^(?:essay(?:\s+questions?)?|short\s+answer|written\s+questions?|الأسئلة\s+المقالية|أسئلة\s+مقالية|مقالي)\s*:?$",
    re.IGNORECASE,
)
GREETING_OR_INTRO_RE = re.compile(
    r"^(?:سلام\s+عليكم(?:\s+ورحمة\s+الله\s+وبركاته)?|مرحباً(?:\s+بكم)?|أهلاً\s+بكم|مقدمة|تعليمات|ملاحظات\s+عامة|hello|hi\b|welcome|instructions)\b",
    re.IGNORECASE,
)


def _int_to_rgb(color_int: int) -> Tuple[int, int, int]:
    r = (color_int >> 16) & 0xFF
    g = (color_int >> 8) & 0xFF
    b = color_int & 0xFF
    return r, g, b


def _red_signal_score(color_int: int) -> float:
    """يحسب قوة إشارة اللون الأحمر من 0.0 إلى 1.0."""
    r, g, b = _int_to_rgb(color_int)
    if r < 155:
        return 0.0
    dom = r - max(g, b)
    if dom < 65 or g > 110 or b > 110:
        return 0.0
    raw = (r - 150) / 105.0 * 0.5 + (dom - 60) / 195.0 * 0.5
    return max(0.0, min(1.0, round(raw, 3)))


def _overlap_ratio(rect_a, rect_b) -> float:
    """يحسب نسبة تداخل مستطيل الخيار مع مستطيل التظليل/الهايلايت."""
    x0 = max(rect_a[0], rect_b[0])
    y0 = max(rect_a[1], rect_b[1])
    x1 = min(rect_a[2], rect_b[2])
    y1 = min(rect_a[3], rect_b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter_area = (x1 - x0) * (y1 - y0)
    a_area = max(1e-6, (rect_a[2] - rect_a[0]) * (rect_a[3] - rect_a[1]))
    return min(1.0, round(inter_area / a_area, 3))


def _letter_to_idx(label: str) -> Optional[int]:
    ch = label.strip().upper()
    mapping = {
        "A": 0, "أ": 0, "ا": 0,
        "B": 1, "ب": 1,
        "C": 2, "ج": 2,
        "D": 3, "د": 3,
    }
    return mapping.get(ch)


def _is_non_question_intro(text: str, line_idx_on_page: int, has_active_question: bool) -> bool:
    cleaned = " ".join(text.split()).strip()
    if not cleaned:
        return True
    if Q_START_RE.match(cleaned) or OPT_START_RE.match(cleaned) or EXPL_START_RE.match(cleaned):
        return False
    if ESSAY_SECTION_RE.match(cleaned) or GREETING_OR_INTRO_RE.match(cleaned):
        return True
    if not has_active_question and line_idx_on_page <= 3 and len(cleaned) <= 120 and not cleaned.endswith("?"):
        return True
    return False


def extract_pdf_lines(pdf_bytes: bytes) -> List[Dict[str, Any]]:
    """الطبقة 1 و 2: استخراج الأسطر مع درجة قوة اللون الأحمر ونسبة تداخل الهايلايت."""
    if not pdf_bytes:
        raise ValueError("ملف PDF فارغ.")

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if doc.page_count == 0:
        raise ValueError("ملف PDF لا يحتوي على صفحات.")

    lines: List[Dict[str, Any]] = []
    for page_num, page in enumerate(doc, start=1):
        highlight_rects = [
            annot.rect
            for annot in (page.annots() or [])
            if annot.type[0] in (8, 9, 10, 11)
        ]

        text_dict = page.get_text("dict")
        has_any_text_on_page = False
        page_line_counter = 0

        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                joined = " ".join("".join(s.get("text", "") for s in spans).split()).strip()
                if not joined:
                    continue
                has_any_text_on_page = True
                page_line_counter += 1
                rect = line.get("bbox")

                red_scores = [
                    _red_signal_score(s.get("color", 0))
                    for s in spans
                    if s.get("text", "").strip()
                ]
                max_red = max(red_scores) if red_scores else 0.0

                hl_scores = [_overlap_ratio(rect, hr) for hr in highlight_rects]
                max_hl = max(hl_scores) if hl_scores else 0.0

                signal_strength = max(max_red, max_hl)
                marked = signal_strength >= 0.45

                lines.append({
                    "page": page_num,
                    "page_line_idx": page_line_counter,
                    "text": joined,
                    "marked": marked,
                    "signal_strength": signal_strength,
                    "red_score": max_red,
                    "hl_score": max_hl,
                })

        if not has_any_text_on_page:
            lines.append({
                "page": page_num,
                "page_line_idx": 0,
                "text": "",
                "marked": False,
                "signal_strength": 0.0,
                "scanned_page": True,
            })

    return lines


def _finalize_block(
    blk: Dict[str, Any],
    csv_rows: List[Tuple[str, str, str, str, str, int, str]],
    essay_questions: List[Dict[str, Any]],
    review_items: List[Dict[str, Any]],
):
    q_text = " ".join(blk["question"].split()).strip()
    if not q_text:
        return

    raw_opts = blk["options"]
    present_count = sum(1 for o in raw_opts if o is not None and o[0].strip())
    expl_text = " ".join(blk["explanation"].split()).strip()

    # فصل الأسئلة المقالية أو غير المكتملة عن أسئلة الاختيار من متعدد
    if present_count < 2:
        essay_questions.append({
            "page": blk["page"],
            "question": q_text,
            "explanation": expl_text,
        })
        return

    opts_clean: List[str] = []
    marked_candidates: List[Tuple[int, float]] = []

    for i in range(4):
        item = raw_opts[i]
        if item is None or not item[0].strip():
            opts_clean.append("-")
        else:
            opts_clean.append(" ".join(item[0].split()).strip())
            if item[1]:
                marked_candidates.append((i, float(item[2])))

    needs_review = False
    reasons: List[str] = []
    confidence = 0

    if present_count < 4:
        needs_review = True
        reasons.append(f"الخيارات المكتملة {present_count} من 4")

    if len(marked_candidates) == 1:
        chosen_idx, sig = marked_candidates[0]
        correct_idx = chosen_idx
        confidence = int(min(99, 75 + sig * 24))
        if sig < 0.65:
            needs_review = True
            reasons.append("شدة اللون الأحمر أو الهايلايت متوسطة وتحتاج تأكيداً")
    elif len(marked_candidates) > 1:
        # فرز حسب قوة الإشارة، مع وضع علامة مراجعة إلزامية لوجود أكثر من خيار ملوّن
        marked_candidates.sort(key=lambda x: x[1], reverse=True)
        correct_idx = marked_candidates[0][0]
        confidence = 35
        needs_review = True
        reasons.append("أكثر من خيار يحمل لوناً أحمر أو هايلايت")
    else:
        correct_idx = 0
        confidence = 15
        needs_review = True
        reasons.append("لم يتم رصد لون أحمر أو هايلايت واضح على أي خيار")

    if needs_review:
        reasons.append(f"نسبة الثقة: {confidence}%")
        review_note = f"[مراجعة مطلوبة: {' - '.join(reasons)}]"
        expl_text = f"{review_note} {expl_text}".strip()
        review_items.append({
            "question_number": len(csv_rows) + 1,
            "page": blk["page"],
            "question": q_text[:90],
            "confidence": confidence,
            "reasons": reasons,
        })

    csv_rows.append((
        q_text,
        opts_clean[0],
        opts_clean[1],
        opts_clean[2],
        opts_clean[3],
        correct_idx,
        expl_text,
    ))


def parse_mcq_to_csv_rows(lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    """يفصل المقدمات والأسئلة المقالية ويفرز أسئلة MCQ مع تقييم الثقة والتحقق."""
    scanned_pages = sorted({
        ln["page"] for ln in lines if ln.get("scanned_page")
    })
    text_lines = [ln for ln in lines if not ln.get("scanned_page") and ln.get("text")]

    if not text_lines:
        raise ValueError(
            "لم يتم العثور على نص قابل للقراءة في ملف PDF (قد يكون الملف صوراً ممسوحة ضوئياً)."
        )

    csv_rows: List[Tuple[str, str, str, str, str, int, str]] = []
    essay_questions: List[Dict[str, Any]] = []
    review_items: List[Dict[str, Any]] = []
    skipped_intro_count = 0
    essay_mode = False
    current: Optional[Dict[str, Any]] = None

    for ln in text_lines:
        txt = ln["text"].strip()
        mk = bool(ln.get("marked"))
        sig = float(ln.get("signal_strength", 0.0))
        page_no = ln.get("page", 1)
        page_line_idx = ln.get("page_line_idx", 1)

        if ESSAY_SECTION_RE.match(txt):
            if current is not None:
                _finalize_block(current, csv_rows, essay_questions, review_items)
                current = None
            essay_mode = True
            continue

        if _is_non_question_intro(txt, page_line_idx, current is not None):
            skipped_intro_count += 1
            continue

        q_m = Q_START_RE.match(txt)
        opt_m = OPT_START_RE.match(txt)
        expl_m = EXPL_START_RE.match(txt)

        if q_m:
            if current is not None:
                _finalize_block(current, csv_rows, essay_questions, review_items)
            q_body = q_m.group(1).strip()
            if essay_mode:
                essay_questions.append({
                    "page": page_no,
                    "question": q_body,
                    "explanation": "",
                })
                current = None
            else:
                current = {
                    "page": page_no,
                    "question": q_body,
                    "options": [None, None, None, None],  # (text, marked, signal_strength)
                    "explanation": "",
                }
        elif opt_m and current is not None:
            opt_idx = _letter_to_idx(opt_m.group(1))
            opt_txt = opt_m.group(2).strip()
            if opt_idx is not None:
                prev = current["options"][opt_idx]
                if prev is None:
                    current["options"][opt_idx] = (opt_txt, mk, sig)
                else:
                    current["options"][opt_idx] = (
                        f"{prev[0]} {opt_txt}".strip(),
                        prev[1] or mk,
                        max(prev[2], sig),
                    )
        elif expl_m and current is not None:
            part = expl_m.group(1).strip()
            current["explanation"] = f"{current['explanation']} {part}".strip()
        elif essay_mode and essay_questions:
            essay_questions[-1]["question"] = f"{essay_questions[-1]['question']} {txt}".strip()
        elif current is not None:
            filled_indices = [i for i, v in enumerate(current["options"]) if v is not None]
            if filled_indices:
                last_i = filled_indices[-1]
                prev_txt, prev_mk, prev_sig = current["options"][last_i]
                current["options"][last_i] = (
                    f"{prev_txt} {txt}".strip(),
                    prev_mk or mk,
                    max(prev_sig, sig),
                )
            else:
                current["question"] = f"{current['question']} {txt}".strip()

    if current is not None:
        _finalize_block(current, csv_rows, essay_questions, review_items)

    if not csv_rows and not essay_questions:
        raise ValueError("تعذر استخراج أسئلة من الملف.")

    return {
        "csv_rows": csv_rows,
        "essay_questions": essay_questions,
        "essay_count": len(essay_questions),
        "total_extracted": len(csv_rows),
        "verified_count": len(csv_rows) - len(review_items),
        "needs_review_count": len(review_items),
        "review_items": review_items,
        "skipped_intro_count": skipped_intro_count,
        "scanned_pages": scanned_pages,
    }


def extract_pdf_mcq_rows(pdf_bytes: bytes) -> Dict[str, Any]:
    """نقطة الدخول الموحدة: استخراج الأسطر وتحليلها وفق طبقات التحقق."""
    lines = extract_pdf_lines(pdf_bytes)
    return parse_mcq_to_csv_rows(lines)
