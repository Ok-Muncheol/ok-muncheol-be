"""D: 보고서·구상 패키지 출력 (Markdown / HTML / JSON / PDF).

표준 라이브러리만 쓴다. PDF는 WeasyPrint가 설치된 경우에만(지연 import) 만든다.
담당자 확인 완료 전 문서에는 '검토 초안' 표시를 넣는다. 예시 규칙 패키지에는 '약관 검수 전 초안'
표시.
"""

import html
import json
from pathlib import Path

from app.schemas.reports import ClaimPackage, ReviewReport

_CSS = """@page{size:A4;margin:16mm 15mm 18mm;\
@bottom-right{content:counter(page)" / "counter(pages);font-size:8pt;color:#9CA3AF}}
body{font-family:"Noto Sans CJK KR","Noto Sans KR",sans-serif;font-size:9.4pt;color:#1F2937;\
line-height:1.55}
header{border-bottom:2px solid #0A2E6E;padding-bottom:4mm;margin-bottom:4mm}.eb{color:#1D5BD8;\
font-weight:700;font-size:8.5pt;margin:0}
h1{font-size:16pt;color:#0A2E6E;margin:1.5mm 0}.meta{color:#6B7280;font-size:8.5pt;margin:0}
.badge{display:inline-block;padding:.5mm 2.5mm;border-radius:99px;font-size:8.5pt;font-weight:700;\
background:#E3ECFB;color:#1D5BD8;margin-right:1mm}
.warn{background:#FEF3E7;color:#B54708}.ok{background:#E7F6EC;color:#1E7B3C}
section{border:1px solid #D9DFEA;border-radius:6px;padding:3mm 4mm;margin-bottom:3mm;\
page-break-inside:avoid}
section.empty{background:#F7F8FA;color:#6B7280}h2{font-size:11pt;color:#0A2E6E;margin:0 0 2mm}
table{width:100%;border-collapse:collapse;font-size:8.4pt;margin:1mm 0}th{background:#0A2E6E;\
color:#fff;text-align:left;padding:1.2mm 2mm}
td{border-bottom:1px solid #E5E7EB;padding:1.2mm 2mm;vertical-align:top;word-break:keep-all}
table.kv td:first-child{width:26%;color:#0A2E6E;font-weight:700;background:#F7F9FC}
.subt{font-weight:700;color:#0A2E6E;font-size:8.8pt;margin:2mm 0 1mm;padding-left:2mm;\
border-left:2.5px solid #1D5BD8}
.ev{font-size:7.8pt;color:#6B7280;margin-top:1.5mm}code{background:#F1F4F9;padding:.2mm 1mm;\
border-radius:2px}
ul{margin:.5mm 0;padding-left:5mm}.note{font-size:8.2pt;color:#6B7280}.total td{font-weight:700;\
background:#E3ECFB}
.wm{position:fixed;top:45%;left:0;right:0;text-align:center;font-size:44pt;color:rgba(29,91,216,.07);transform:rotate(-24deg);font-weight:900}"""


def _e(v: object) -> str:
    return html.escape(str(v))


def _value(v: object) -> str:
    if v is None:
        return '<span class="note">—</span>'
    if v is True:
        return "예"
    if v is False:
        return "아니요"
    if isinstance(v, list):
        if not v:
            return '<span class="note">없음</span>'
        if all(isinstance(x, dict) for x in v):
            keys = list(v[0].keys())
            head = "".join(f"<th>{_e(k)}</th>" for k in keys)
            rows = "".join(
                "<tr>" + "".join(f"<td>{_value(x.get(k))}</td>" for k in keys) + "</tr>" for x in v
            )
            return f"<table><tr>{head}</tr>{rows}</table>"
        return "<ul>" + "".join(f"<li>{_value(x)}</li>" for x in v) + "</ul>"
    if isinstance(v, dict):

        def is_wide(x: object) -> bool:
            return (isinstance(x, dict) and bool(x)) or (
                isinstance(x, list) and bool(x) and all(isinstance(i, dict) for i in x)
            )

        simple = [(k, x) for k, x in v.items() if not is_wide(x)]
        wide = [(k, x) for k, x in v.items() if is_wide(x)]
        out = ""
        if simple:
            out += (
                '<table class="kv">'
                + "".join(f"<tr><td>{_e(k)}</td><td>{_value(x)}</td></tr>" for k, x in simple)
                + "</table>"
            )
        out += "".join(f'<p class="subt">{_e(k)}</p>{_value(x)}' for k, x in wide)
        return out
    return _e(v)


def report_html(report: ReviewReport) -> str:
    v = report["versions"]
    j = report["judgment"]
    approved = report["approval_status"] == "approved"
    status = {
        "draft_ready": "초안 작성 가능",
        "needs_confirmation": "추가 확인 필요",
        "deferred": "판단 보류",
    }[j["status"]]
    parts = [
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'><title>검토 초안 "
        f"{_e(v['case_id'])} v{v['report_version']}</title>"
        f"<style>{_CSS}</style></head><body>",
        "" if approved else '<div class="wm">검토 초안</div>',
        "<header><p class='eb'>사고 영상 기반 과실 검토 · 검토 초안 보고서</p>",
        f"<h1>사건 {_e(v['case_id'])}</h1><p class='meta'>보고서 v{v['report_version']}"
        + (f" (v{report.get('supersedes')} 대체)" if report.get("supersedes") else "")
        + f" · 카드 v{v['fact_card_version']} · 판단 v{v['judgment_version']} · "
        f"{_e(report.get('created_at', ''))}"
        + f" · 사유: {_e(report.get('reason', ''))}</p><p style='margin:2mm 0 0'>"
        f"<span class='badge {'ok' if j['status'] == 'draft_ready' else 'warn'}'>{status}</span>"
        f"<span class='badge {'ok' if approved else 'warn'}'>"
        f"{'확인 완료: ' + _e(report['approved_by']) if approved else '담당자 확인 전'}</span></p>"
        "</header>",
    ]
    for s in report["sections"]:
        if s["state"] == "empty":
            parts.append(
                f"<section class='empty'><h2>{s['no']}. {_e(s['title'])}</h2><span class='badge "
                f"warn'>{_e(s['empty_reason'])}</span></section>"
            )
        else:
            ev = "".join(f"<code>{_e(e)}</code> " for e in s["evidence_ids"])
            parts.append(
                f"<section><h2>{s['no']}. {_e(s['title'])}</h2>{_value(s['body'])}"
                + (f"<div class='ev'>근거: {ev}</div>" if ev else "")
                + "</section>"
            )
    parts.append(
        "<p class='note'>판단 로그와 계산 결과를 조립한 검토 초안입니다. 최종 판단과 확인은 "
        "담당자가 합니다. 금액은 계산 코드가 산출했습니다.</p></body></html>"
    )
    return "".join(parts)


def report_markdown(report: ReviewReport) -> str:
    v = report["versions"]
    lines = [
        f"# 검토 초안 보고서 — {v['case_id']} v{v['report_version']}",
        f"카드 v{v['fact_card_version']} · 판단 v{v['judgment_version']} · "
        f"{report['approval_status']}",
        "",
    ]
    for s in report["sections"]:
        lines.append(f"## {s['no']}. {s['title']}")
        if s["state"] == "empty":
            lines += [f"_{s['empty_reason']}_", ""]
            continue
        lines.append("```json\n" + json.dumps(s["body"], ensure_ascii=False, indent=2) + "\n```")
        if s["evidence_ids"]:
            lines.append("근거: " + ", ".join(f"`{e}`" for e in s["evidence_ids"]))
        lines.append("")
    return "\n".join(lines)


def package_html(pkg: ClaimPackage) -> str:
    a = pkg["amounts"]
    won = lambda x: "—" if x is None else f"{x:,}원"  # noqa: E731
    rows = "".join(
        f"<tr><td>{_e(r['claim_no'])}</td><td>{_e(r['insured_party_id'] or '—')}</td><td>"
        f"{_e(r['coverage'])}</td>"
        f"<td>{won(r['insurance_payment'])}</td><td>{r['counterparty_fault']}%</td><td>{won(r['planned_share'])}</td>"
        f"<td>{r['evidence_count']}건</td></tr>"
        for r in pkg["rows"]
    )
    cp = pkg["counterparty"] or {}
    notice = pkg.get("rule_notice")
    return (
        "<!doctype html><html lang='ko'><head><meta charset='utf-8'><title>구상 청구 패키지 "
        f"{_e(pkg['case_id'])}</title><style>{_CSS}</style></head><body>"
        + (f'<div class="wm">{_e(notice)}</div>' if notice else "")
        + f"<header><p class='eb'>구상 청구 패키지 (1차 판정 초안)</p><h1>사건 {_e(pkg['case_id'])}"
        f" → {_e(cp.get('insurer') or '상대 보험사 미입력')}</h1>"
        f"<p class='meta'>패키지 v{pkg['package_version']} · 근거 보고서 v{pkg['report_version']} ·"
        f" 상대 차량 {_e(cp.get('vehicle_no') or '미입력')}"
        + (f" · 상대 접수번호 {_e(cp['claim_no'])}" if cp.get("claim_no") else "")
        + f" · 규칙 {_e(pkg['rule_version'])}</p>"
        f"<p style='margin:2mm 0 0'><span class='badge'>상대 과실 {pkg['counterparty_fault']}%"
        "</span>"
        + (
            f"<span class='badge warn'>협의 변경: {_e(pkg['fault_change_reason'])}</span>"
            if pkg["fault_change_reason"]
            else ""
        )
        + (f"<span class='badge warn'>{_e(notice)}</span>" if notice else "")
        + (
            f"<span class='badge warn'>기본 가정 {len(pkg['assumptions'])}건</span>"
            if pkg.get("assumptions")
            else ""
        )
        + f"<span class='badge ok'>확인 완료: {_e(pkg['confirmed_by'])} · {_e(pkg['confirmed_at'])}"
        "</span></p></header>"
        "<section><h2>1. 구상 요약표</h2><table><tr><th>구상 건</th><th>보험가입자</th><th>담보"
        "</th><th>산정 보험금</th><th>상대 과실</th><th>예정액</th><th>증빙</th></tr>"
        + rows
        + "</table>"
        + _value(
            {
                "구상 예정액(산정 보험금 × 상대 과실)": won(a["planned"]),
                "청구액": won(a["claimed"]),
                "회수액": won(a["recovered"]) if a["recovered"] is not None else "미회수",
            }
        )
        + (
            "<p class='subt'>기본 가정(미입력 항목)</p>" + _value(list(pkg["assumptions"]))
            if pkg.get("assumptions")
            else ""
        )
        + "</section><section><h2>2. 과실비율 근거서</h2>"
        + _value(pkg["basis"])
        + "</section>"
        "<section><h2>3. 증빙 목록</h2>"
        + _value(pkg["evidence"])
        + (
            f"<p class='note'>증빙 없음(경고): {_e(', '.join(pkg['missing_evidence']))}</p>"
            if pkg["missing_evidence"]
            else ""
        )
        + f"</section><p class='note'>중복 방지 키 {_e(pkg['idempotency_key'])} · 보험사 간 실제 "
        "양식은 현직자 인터뷰 후 매핑.</p></body></html>"
    )


def to_json(obj: object, path: str | Path | None = None) -> str:
    text = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    if path:
        Path(path).write_text(text, encoding="utf-8")
    return text


def to_pdf(html_text: str, path: str | Path) -> Path:
    """WeasyPrint 필요(pip install weasyprint). 한글 글꼴(Noto Sans CJK KR) 설치 필요."""
    try:
        from weasyprint import HTML
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PDF 생성에는 weasyprint가 필요합니다") from exc
    HTML(string=html_text).write_pdf(str(path))
    return Path(path)
