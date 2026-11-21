import html
from typing import Any, Dict, List
import weasyprint


def build_bank_pdf_bytes(
    bank_name: str,
    questions: List[Dict[str, Any]],
    solved: bool = False,
    settings: Dict[str, Any] | None = None,
) -> bytes:
    cfg = settings or {}
    mode_label = "Solved (نسخة محلولة)" if solved else "Unsolved (نسخة غير محلولة)"
    letters = "ABCDEFGHIJ"

    cards_html: List[str] = []
    for idx, q in enumerate(questions, start=1):
        q_text = html.escape(str(q.get("question", "")).strip())
        opts = q.get("options") or []
        correct_idx = q.get("correct_idx", 0)
        expl = str(q.get("explanation", "") or "").strip()

        opts_items: List[str] = []
        for o_idx, opt_val in enumerate(opts):
            letter = letters[o_idx] if o_idx < len(letters) else str(o_idx + 1)
            opt_escaped = html.escape(str(opt_val).strip())
            is_correct = solved and (o_idx == correct_idx)
            row_class = "opt correct" if is_correct else "opt"
            badge = ' <span class="badge">✓ صحيح</span>' if is_correct else ""
            opts_items.append(
                f'<div class="{row_class}"><span class="opt-key">{letter}.</span> {opt_escaped}{badge}</div>'
            )

        expl_html = ""
        if solved and expl:
            expl_html = f'<div class="expl"><strong>التوضيح:</strong> {html.escape(expl)}</div>'

        cards_html.append(
            f"""
            <article class="q-card">
              <div class="q-header">
                <span class="q-num">Q{idx}.</span>
                <span class="q-text">{q_text}</span>
              </div>
              <div class="opts">
                {"".join(opts_items)}
              </div>
              {expl_html}
            </article>
            """
        )

    doc_html = f"""<!DOCTYPE html>
<html lang="ar" dir="auto">
<head>
<meta charset="utf-8">
<style>
  @page {{
    size: A4;
    margin: 14mm 14mm 16mm 14mm;
  }}
  body {{
    font-family: "DejaVu Sans", "Noto Sans Arabic", Arial, sans-serif;
    font-size: 10.5pt;
    line-height: 1.45;
    color: #111827;
    margin: 0;
  }}
  .doc-header {{
    border-bottom: 1.5px solid #2563eb;
    padding-bottom: 8px;
    margin-bottom: 12px;
    display: flex;
    justify-content: space-between;
    align-items: baseline;
  }}
  .doc-title {{
    font-size: 13pt;
    font-weight: 700;
    margin: 0;
  }}
  .doc-meta {{
    font-size: 9pt;
    color: #4b5563;
  }}
  .q-card {{
    border: 1px solid #e5e7eb;
    border-radius: 6px;
    padding: 9px 11px;
    margin-bottom: 10px;
    break-inside: avoid;
    page-break-inside: avoid;
  }}
  .q-header {{
    font-weight: 600;
    margin-bottom: 6px;
  }}
  .q-num {{
    color: #1d4ed8;
    margin-inline-end: 6px;
  }}
  .opts {{
    display: flex;
    flex-direction: column;
    gap: 4px;
  }}
  .opt {{
    padding: 3px 6px;
    border-radius: 4px;
  }}
  .opt-key {{
    font-weight: 600;
    margin-inline-end: 4px;
  }}
  .opt.correct {{
    background: #dcfce7;
    border-inline-start: 3px solid #16a34a;
    font-weight: 600;
  }}
  .badge {{
    color: #15803d;
    font-size: 8.5pt;
    margin-inline-start: 6px;
  }}
  .expl {{
    margin-top: 7px;
    padding-top: 6px;
    border-top: 1px dashed #d1d5db;
    font-size: 9.5pt;
    color: #374151;
  }}
</style>
</head>
<body>
  <header class="doc-header">
    <h1 class="doc-title">{html.escape(bank_name)}</h1>
    <div class="doc-meta">{html.escape(mode_label)} • {len(questions)} سؤال</div>
  </header>
  <main>
    {"".join(cards_html)}
  </main>
</body>
</html>
"""
    return weasyprint.HTML(string=doc_html).write_pdf()
