import hashlib
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
ANSWER_KEY_LINE_RE = re.compile(
    r"^(?:answer\s*key|model\s*answer|correct\s*answer|الإجابة\s+النموذجية|مفتاح\s+الإجابة)\s*[:\-]\s*([A-Da-dأ-د1-4])",
    re.IGNORECASE,
)


def _int_to_rgb(color_int: int) -> Tuple[int, int, int]:
    r = (color_int >> 16) & 0xFF
    g = (color_int >> 8) & 0xFF
    b = color_int & 0xFF
    return r, g, b


def _red_signal_score(color_int: int) -> float:
    """الطبقة 1: تقييم قوة اللون الأحمر (0.0 إلى 1.0) مع استبعاد الرمادي والأسود."""
    r, g, b = _int_to_rgb(color_int)
    if r < 155:
        return 0.0
    dom = r - max(g, b)
    if dom < 65 or g > 110 or b > 110:
        return 0.0
    raw = (r - 150) / 105.0 * 0.5 + (dom - 60) / 195.0 * 0.5
    return max(0.0, min(1.0, round(raw, 3)))


def _overlap_ratio(rect_a, rect_b) -> float:
    """الطبقة 2: حساب نسبة التداخل الهندسي بين الخيار ومناطق التظليل/الهايلايت."""
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
    """الطبقة 3: استبعاد المقدمات والترحيب والعناوين العامة غير المرتبطة بسؤال."""
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


# ---------------------------------------------------------------------------
# الطبقة 6: فحص الرسومات والمستطيلات الخلفية الملوّنة داخل الصفحة (Drawing Rects)
# ---------------------------------------------------------------------------
def _extract_colored_drawing_rects(page: fitz.Page) -> List[Tuple[fitz.Rect, float]]:
    """
    يرصد المستطيلات المرسومة خلف النص (مثل صناديق التظليل أو أشرطة التلوين)
    ويقدّر شدة الإشارة الحمراء أو التظليلية لكل مستطيل.
    """
    results: List[Tuple[fitz.Rect, float]] = []
    try:
        drawings = page.get_drawings() or []
    except Exception:
        return results

    for drw in drawings:
        rect = drw.get("rect")
        if not rect or rect.is_empty or rect.width <= 4 or rect.height <= 4:
            continue
        fill = drw.get("fill")
        stroke = drw.get("color")
        score = 0.0

        for col in (fill, stroke):
            if not col or len(col) < 3:
                continue
            r_val = int(round(col[0] * 255)) if max(col[:3]) <= 1.0 else int(round(col[0]))
            g_val = int(round(col[1] * 255)) if max(col[:3]) <= 1.0 else int(round(col[1]))
            b_val = int(round(col[2] * 255)) if max(col[:3]) <= 1.0 else int(round(col[2]))
            rgb_int = (max(0, min(255, r_val)) << 16) + (max(0, min(255, g_val)) << 8) + max(0, min(255, b_val))
            red_s = _red_signal_score(rgb_int)
            if red_s > score:
                score = red_s
            elif max(r_val, g_val, b_val) >= 180 and abs(r_val - g_val) > 25:
                score = max(score, 0.55)

        if score > 0.0:
            results.append((rect, score))
    return results


# ---------------------------------------------------------------------------
# الطبقة 7: فحص التباين النسبي بين الخيارات داخل نفس السؤال (Relative Contrast)
# ---------------------------------------------------------------------------
def _apply_relative_contrast_check(
    option_signals: List[float],
) -> Tuple[Optional[int], float, List[str]]:
    """
    يقارن قوة الإشارة بين الخيارات الأربعة؛ إذا كان خيار واحد يتفوق بوضوح على بقية
    الخيارات حتى لو كان لونه أفتح قليلاً، يُسجّل كمرشح مع توضيح السبب.
    """
    notes: List[str] = []
    if not option_signals or len(option_signals) < 2:
        return None, 0.0, notes

    max_val = max(option_signals)
    if max_val <= 0.0:
        return None, 0.0, notes

    sorted_vals = sorted(option_signals, reverse=True)
    second_val = sorted_vals[1]
    top_indices = [i for i, v in enumerate(option_signals) if abs(v - max_val) < 1e-6]

    if len(top_indices) == 1 and (max_val - second_val) >= 0.35:
        return top_indices[0], min(0.95, max_val), notes

    if len(top_indices) > 1:
        notes.append("تساوٍ في درجة التلوين/التظليل بين أكثر من خيار")
        return None, max_val * 0.5, notes

    return None, max_val * 0.6, notes


# ---------------------------------------------------------------------------
# الطبقة 8: كشف التكرار والأسئلة المتطابقة أو شبه المتطابقة داخل الملف
# ---------------------------------------------------------------------------
def _normalize_for_fingerprint(text: str) -> str:
    cleaned = re.sub(r"[^\w\s\u0600-\u06FF]", " ", text.lower())
    return " ".join(cleaned.split()).strip()


def _compute_question_fingerprint(q_text: str, opts: List[str]) -> str:
    base = _normalize_for_fingerprint(q_text) + "||" + "|".join(
        _normalize_for_fingerprint(o) for o in opts if o and o != "-"
    )
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# الطبقة 9: فحص سلامة النصوص ومنع الحقن أو الرموز التالفة / الضوضاء
# ---------------------------------------------------------------------------
def _sanitize_cell_text(val: str) -> str:
    """ينظف السلاسل من محارف التحكم غير المرئية ويمنع حقن الصيغ في جداول CSV."""
    cleaned = "".join(ch for ch in (val or "") if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    cleaned = " ".join(cleaned.split()).strip()
    if cleaned and cleaned[0] in ("=", "+", "-", "@", "\t", "\r"):
        cleaned = "'" + cleaned
    return cleaned


# ---------------------------------------------------------------------------
# الطبقة 10: فحص حدود الحجم والصفحات وحماية الذاكرة والأداء (Resource Guard)
# ---------------------------------------------------------------------------
MAX_PDF_BYTES = 25 * 1024 * 1024  # 25 MB حد أقصى آمن
MAX_PDF_PAGES = 250              # 250 صفحة حد أقصى آمن


def _enforce_pdf_limits(pdf_bytes: bytes, doc: fitz.Document):
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise ValueError("حجم ملف PDF يتجاوز الحد المسموح به للأمان والأداء (25 ميجابايت).")
    if doc.page_count > MAX_PDF_PAGES:
        raise ValueError(f"عدد صفحات الملف ({doc.page_count}) يتجاوز الحد المسموح به ({MAX_PDF_PAGES} صفحة).")
    if doc.is_encrypted and not doc.needs_pass:
        pass
    elif doc.needs_pass:
        raise ValueError("ملف PDF محمي بكلمة مرور ولا يمكن قراءته تلقائياً.")


def extract_pdf_lines(pdf_bytes: bytes) -> List[Dict[str, Any]]:
    """يستخرج النص مع درجات اللون الأحمر، الهايلايت، والرسومات الملوّنة لكل سطر."""
    if not pdf_bytes:
        raise ValueError("ملف PDF فارغ.")
    if len(pdf_bytes) > MAX_PDF_BYTES:
        raise ValueError("حجم ملف PDF يتجاوز الحد المسموح به للأمان والأداء (25 ميجابايت).")

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if doc.page_count == 0:
        raise ValueError("ملف PDF لا يحتوي على صفحات.")
    _enforce_pdf_limits(pdf_bytes, doc)

    lines: List[Dict[str, Any]] = []
    for page_num, page in enumerate(doc, start=1):
        highlight_rects = [
            annot.rect
            for annot in (page.annots() or [])
            if annot.type[0] in (8, 9, 10, 11)
        ]
        drawing_rects = _extract_colored_drawing_rects(page)

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

                drw_scores = [
                    _overlap_ratio(rect, dr) * d_score
                    for dr, d_score in drawing_rects
                ]
                max_drw = max(drw_scores) if drw_scores else 0.0

                signal_strength = max(max_red, max_hl, max_drw)
                marked = signal_strength >= 0.45

                lines.append({
                    "page": page_num,
                    "page_line_idx": page_line_counter,
                    "text": joined,
                    "marked": marked,
                    "signal_strength": signal_strength,
                    "red_score": max_red,
                    "hl_score": max_hl,
                    "drw_score": max_drw,
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
    seen_fingerprints: Dict[str, int],
):
    q_text = _sanitize_cell_text(blk["question"])
    if not q_text:
        return

    raw_opts = blk["options"]
    present_count = sum(1 for o in raw_opts if o is not None and o[0].strip())
    expl_text = _sanitize_cell_text(blk["explanation"])

    if present_count < 2:
        essay_questions.append({
            "page": blk["page"],
            "question": q_text,
            "explanation": expl_text,
        })
        return

    opts_clean: List[str] = []
    option_signals: List[float] = []
    marked_candidates: List[Tuple[int, float]] = []

    for i in range(4):
        item = raw_opts[i]
        if item is None or not item[0].strip():
            opts_clean.append("-")
            option_signals.append(0.0)
        else:
            clean_opt = _sanitize_cell_text(item[0]) or "-"
            sig_val = float(item[2])
            opts_clean.append(clean_opt)
            option_signals.append(sig_val)
            if item[1]:
                marked_candidates.append((i, sig_val))

    needs_review = False
    reasons: List[str] = []
    confidence = 0

    if present_count < 4:
        needs_review = True
        reasons.append(f"الخيارات المكتملة {present_count} من 4")

    contrast_idx, contrast_sig, contrast_notes = _apply_relative_contrast_check(option_signals)
    reasons.extend(contrast_notes)

    if len(marked_candidates) == 1:
        chosen_idx, sig = marked_candidates[0]
        correct_idx = chosen_idx
        confidence = int(min(99, 78 + sig * 21))
        if sig < 0.65:
            needs_review = True
            reasons.append("شدة اللون الأحمر أو التظليل متوسطة وتحتاج تأكيداً")
    elif len(marked_candidates) > 1:
        marked_candidates.sort(key=lambda x: x[1], reverse=True)
        correct_idx = marked_candidates[0][0]
        confidence = 30
        needs_review = True
        reasons.append("أكثر من خيار يحمل لوناً أحمر أو تظليلاً")
    elif contrast_idx is not None and contrast_sig >= 0.55:
        correct_idx = contrast_idx
        confidence = int(min(85, 65 + contrast_sig * 20))
        needs_review = True
        reasons.append("تم الاستدلال عبر التباين النسبي بين الخيارات ويُنصح بمراجعته")
    else:
        correct_idx = 0
        confidence = 10
        needs_review = True
        reasons.append("لم يتم رصد لون أحمر أو تظليل واضح على أي خيار")

    # فحص التكرار داخل نفس الملف (الطبقة 8)
    fp = _compute_question_fingerprint(q_text, opts_clean)
    if fp in seen_fingerprints:
        prev_q_num = seen_fingerprints[fp]
        needs_review = True
        reasons.append(f"سؤال مكرر أو مطابق للسؤال #{prev_q_num}")
    else:
        seen_fingerprints[fp] = len(csv_rows) + 1

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
    """يفصل المقدمات والأسئلة المقالية ويفرز أسئلة MCQ مع 10 طبقات تحقق وحماية."""
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
    seen_fingerprints: Dict[str, int] = {}
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
                _finalize_block(current, csv_rows, essay_questions, review_items, seen_fingerprints)
                current = None
            essay_mode = True
            continue

        ans_key_m = ANSWER_KEY_LINE_RE.match(txt)
        if ans_key_m and current is not None:
            key_idx = _letter_to_idx(ans_key_m.group(1))
            if key_idx is not None:
                prev_opt = current["options"][key_idx]
                if prev_opt is not None:
                    current["options"][key_idx] = (
                        prev_opt[0],
                        True,
                        max(float(prev_opt[2]), 0.85),
                    )
            continue

        if _is_non_question_intro(txt, page_line_idx, current is not None):
            skipped_intro_count += 1
            continue

        q_m = Q_START_RE.match(txt)
        opt_m = OPT_START_RE.match(txt)
        expl_m = EXPL_START_RE.match(txt)

        if q_m:
            if current is not None:
                _finalize_block(current, csv_rows, essay_questions, review_items, seen_fingerprints)
            q_body = q_m.group(1).strip()
            if essay_mode:
                essay_questions.append({
                    "page": page_no,
                    "question": _sanitize_cell_text(q_body),
                    "explanation": "",
                })
                current = None
            else:
                current = {
                    "page": page_no,
                    "question": q_body,
                    "options": [None, None, None, None],
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
                        max(float(prev[2]), sig),
                    )
        elif expl_m and current is not None:
            part = expl_m.group(1).strip()
            current["explanation"] = f"{current['explanation']} {part}".strip()
        elif essay_mode and essay_questions:
            essay_questions[-1]["question"] = _sanitize_cell_text(
                f"{essay_questions[-1]['question']} {txt}".strip()
            )
        elif current is not None:
            filled_indices = [i for i, v in enumerate(current["options"]) if v is not None]
            if filled_indices:
                last_i = filled_indices[-1]
                prev_txt, prev_mk, prev_sig = current["options"][last_i]
                current["options"][last_i] = (
                    f"{prev_txt} {txt}".strip(),
                    prev_mk or mk,
                    max(float(prev_sig), sig),
                )
            else:
                current["question"] = f"{current['question']} {txt}".strip()

    if current is not None:
        _finalize_block(current, csv_rows, essay_questions, review_items, seen_fingerprints)

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
    """نقطة الدخول الموحدة: استخراج الأسطر وتحليلها وفق طبقات التحقق والحماية."""
    lines = extract_pdf_lines(pdf_bytes)
    return parse_mcq_to_csv_rows(lines)
