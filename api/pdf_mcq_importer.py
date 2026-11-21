import io
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


def _int_to_rgb(color_int: int) -> Tuple[int, int, int]:
    r = (color_int >> 16) & 0xFF
    g = (color_int >> 8) & 0xFF
    b = color_int & 0xFF
    return r, g, b


def _is_red_rgb(color_int: int) -> bool:
    """يرصد درجات الأحمر الواضحة ويتجنب الرمادي والأسود والألوان الباهتة."""
    r, g, b = _int_to_rgb(color_int)
    return r >= 165 and g <= 95 and b <= 95 and (r - max(g, b)) >= 75


def _rects_overlap(a, b) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _letter_to_idx(label: str) -> Optional[int]:
    ch = label.strip().upper()
    mapping = {
        "A": 0, "أ": 0, "ا": 0,
        "B": 1, "ب": 1,
        "C": 2, "ج": 2,
        "D": 3, "د": 3,
    }
    return mapping.get(ch)


def extract_pdf_lines(pdf_bytes: bytes) -> List[Dict[str, Any]]:
    """
    المرحلة 1: استخراج النص من ملف PDF مع رصد اللون الأحمر أو الهايلايت لكل سطر/مقطع.
    """
    if not pdf_bytes:
        raise ValueError("ملف PDF فارغ.")

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if doc.page_count == 0:
        raise ValueError("ملف PDF لا يحتوي على صفحات.")

    lines: List[Dict[str, Any]] = []
    for page_num, page in enumerate(doc, start=1):
        highlight_rects = []
        for annot in page.annots() or []:
            # أنواع التعليقات التي تمثل تظليلاً/تحديداً في PDF
            if annot.type[0] in (8, 9, 10, 11):
                highlight_rects.append(annot.rect)

        text_dict = page.get_text("dict")
        has_any_text_on_page = False

        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                joined = " ".join("".join(s.get("text", "") for s in spans).split()).strip()
                if not joined:
                    continue
                has_any_text_on_page = True
                rect = line.get("bbox")
                marked_spans = [
                    s for s in spans
                    if s.get("text", "").strip() and _is_red_rgb(s.get("color", 0))
                ]
                has_red = len(marked_spans) > 0
                has_hl = any(_rects_overlap(rect, hr) for hr in highlight_rects)

                lines.append({
                    "page": page_num,
                    "text": joined,
                    "marked": has_red or has_hl,
                })

        if not has_any_text_on_page:
            lines.append({
                "page": page_num,
                "text": "",
                "marked": False,
                "scanned_page": True,
            })

    return lines


def parse_mcq_to_csv_rows(lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    المرحلة 2 و 3: تفصيص كل سؤال (Q) وخياراته (A, B, C, D) وفرزها كصفوف مطابقة للـ CSV
    مع احتياطات أمان صارمة لعدم تمرير الإجابات غير المؤكدة وكأنها محسومة.
    """
    scanned_pages = sorted({
        ln["page"] for ln in lines if ln.get("scanned_page")
    })
    text_lines = [ln for ln in lines if not ln.get("scanned_page") and ln.get("text")]

    if not text_lines:
        raise ValueError(
            "لم يتم العثور على نص قابل للقراءة في ملف PDF (قد يكون الملف صوراً ممسوحة ضوئياً ويحتاج معالجة بصرية)."
        )

    parsed_blocks: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for ln in text_lines:
        txt = ln["text"].strip()
        mk = bool(ln.get("marked"))
        page_no = ln.get("page", 1)

        q_m = Q_START_RE.match(txt)
        opt_m = OPT_START_RE.match(txt)
        expl_m = EXPL_START_RE.match(txt)

        if q_m:
            if current is not None:
                parsed_blocks.append(current)
            current = {
                "page": page_no,
                "question": q_m.group(1).strip(),
                "options": [None, None, None, None],  # index 0..3 -> (text, marked)
                "extra_options": [],
                "explanation": "",
            }
        elif opt_m and current is not None:
            opt_idx = _letter_to_idx(opt_m.group(1))
            opt_txt = opt_m.group(2).strip()
            if opt_idx is not None:
                prev = current["options"][opt_idx]
                if prev is None:
                    current["options"][opt_idx] = (opt_txt, mk)
                else:
                    current["options"][opt_idx] = (
                        f"{prev[0]} {opt_txt}".strip(),
                        prev[1] or mk,
                    )
            else:
                current["extra_options"].append((opt_txt, mk))
        elif expl_m and current is not None:
            part = expl_m.group(1).strip()
            current["explanation"] = f"{current['explanation']} {part}".strip()
        elif current is not None:
            # لو كان هناك خيار سابق لم يكتمل سطره، نكمله
            filled_indices = [i for i, v in enumerate(current["options"]) if v is not None]
            if filled_indices:
                last_i = filled_indices[-1]
                prev_txt, prev_mk = current["options"][last_i]
                current["options"][last_i] = (
                    f"{prev_txt} {txt}".strip(),
                    prev_mk or mk,
                )
            else:
                current["question"] = f"{current['question']} {txt}".strip()

    if current is not None:
        parsed_blocks.append(current)

    csv_rows: List[Tuple[str, str, str, str, str, int, str]] = []
    review_items: List[Dict[str, Any]] = []
    skipped_count = 0

    for idx, blk in enumerate(parsed_blocks, start=1):
        q_text = " ".join(blk["question"].split()).strip()
        raw_opts = blk["options"]
        present_count = sum(1 for o in raw_opts if o is not None and o[0].strip())

        if not q_text or present_count < 2:
            skipped_count += 1
            continue

        opts_clean: List[str] = []
        marked_indices: List[int] = []
        for i in range(4):
            item = raw_opts[i]
            if item is None or not item[0].strip():
                opts_clean.append("-")
            else:
                opts_clean.append(" ".join(item[0].split()).strip())
                if item[1]:
                    marked_indices.append(i)

        needs_review = False
        reasons: List[str] = []

        if present_count < 4:
            needs_review = True
            reasons.append(f"عدد الخيارات الظاهرة {present_count} من 4")

        if len(marked_indices) == 1:
            correct_idx = marked_indices[0]
        elif len(marked_indices) > 1:
            correct_idx = marked_indices[0]
            needs_review = True
            reasons.append("أكثر من خيار ملوّن بالأحمر أو مظلل")
        else:
            correct_idx = 0
            needs_review = True
            reasons.append("لم يُرصد لون أحمر أو هايلايت واضح على أي خيار")

        expl_text = " ".join(blk["explanation"].split()).strip()
        if needs_review:
            review_note = f"[مراجعة مطلوبة: {' - '.join(reasons)}]"
            expl_text = f"{review_note} {expl_text}".strip()
            review_items.append({
                "question_number": len(csv_rows) + 1,
                "page": blk["page"],
                "question": q_text[:90],
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

    if not csv_rows:
        raise ValueError("تعذر استخراج أسئلة MCQ مكتملة من الملف.")

    return {
        "csv_rows": csv_rows,
        "total_extracted": len(csv_rows),
        "verified_count": len(csv_rows) - len(review_items),
        "needs_review_count": len(review_items),
        "review_items": review_items,
        "skipped_count": skipped_count,
        "scanned_pages": scanned_pages,
    }


def extract_pdf_mcq_rows(pdf_bytes: bytes) -> Dict[str, Any]:
    """نقطة الدخول الموحدة: استخراج الأسطر من PDF ثم فرزها إلى صفوف مطابقة للـ CSV."""
    lines = extract_pdf_lines(pdf_bytes)
    return parse_mcq_to_csv_rows(lines)
