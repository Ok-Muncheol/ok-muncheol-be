"""D 시연: 판단 → 견적 없음(금액 미정) → 견적서 PDF → 보고서 전체 → 확인 완료 → 구상 청구서.

실행: py -3.12 scripts/demo_d.py   (결과 HTML은 demo_out/에 저장, 커밋하지 않음)
- 가상 사건(tests/fixtures_d.py). 실제 사고·약관·금액이 아니다.
- 규칙은 예시(example-)라 산정은 확정되지 않고, 패키지에 "예시 규칙 기준 — 약관 검수 전 초안" 표시가
  붙는다.
- 계약·면책 입력 없음 → 기본 가정(자차 가입·보험기간 안·운전자 범위 안, 자기부담금 기본 규칙, 면책
  해당 없음).
- 견적은 tests/fixtures/estimate_sample_10m.pdf 업로드(자동 추출, 증빙으로 자동 등록).
  pdfplumber가 없으면 수동 견적으로 대신한다.
"""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
sys.stdout.reconfigure(errors="replace")  # type: ignore[union-attr]

from app.api import reports as api  # noqa: E402
from app.calculation.estimate_reader import EstimateReaderUnavailable  # noqa: E402
from app.reports.render import package_html, report_html  # noqa: E402
from tests import fixtures_d as fx  # noqa: E402

ACTOR, CASE = "handler-1", "synthetic-case"
OUT = ROOT / "demo_out"
ESTIMATE_PDF = (
    ROOT / "tests" / "fixtures" / "estimate_sample_10m.pdf"
)  # 서식 원본 위에 가상 값을 찍은 견적서


def _section(report, no: int) -> dict:
    return next(s for s in report["sections"] if s["no"] == no)


def summary(step: str, report) -> None:
    print(f"\n[{step}]")
    if report is None:
        print("  (새 보고서 버전 없음)")
        return
    calc, rec = report.get("calculation"), report.get("recovery")
    print(
        f"  보고서 v{report['versions']['report_version']} · {report['approval_status']} · 사유: "
        f"{report['reason']}"
    )
    print(
        f"  산정 상태: {calc['status'] if calc else '산정 없음'} · 자차 보험금: "
        f"{calc and calc['insurance_payment']}"
        f" · 9번 예정액/청구액: {rec and rec['planned']} / {rec and rec['claimed']}"
    )
    for r in calc["reasons"] if calc else []:
        print(f"  - {r}")
    for a in calc.get("assumptions") or [] if calc else []:
        print(f"  · 가정: {a}")


def _fmt(v: object) -> str:
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)


def full_report(report) -> None:
    print(f"\n[보고서 전체 — v{report['versions']['report_version']}]")
    for s in report["sections"]:
        print(
            f"  {s['no']}. {s['title']}"
            + (f"  (비어 있음: {s['empty_reason']})" if s["state"] == "empty" else "")
        )
        for k, v in s["body"].items():
            if isinstance(v, dict) and v and len(_fmt(v)) > 110:
                print(f"     {k}:")
                for kk, vv in v.items():
                    print(f"       {kk}: {_fmt(vv)}")
            elif isinstance(v, list) and v and len(_fmt(v)) > 110:
                print(f"     {k}:")
                for item in v:
                    print(f"       - {_fmt(item)}")
            else:
                print(f"     {k}: {_fmt(v)}")


async def main() -> None:
    api.configure(repo=api.InMemoryReportRepository(), fetch_clauses=None)  # 예시 규칙(기본값)

    v1 = await api.submit_judgment(ACTOR, fx.card(), fx.judgment())
    summary("1. 판단 결과 반영 → 바로 산정 (견적 없음, 금액 미정)", v1)
    early = await api.confirm_report(ACTOR, CASE, 1, counterparty=fx.counterparty())
    print(
        f"  (이 상태에서 확인 완료 → {early['report']['approval_status']}, 청구서 없음: "
        f"{early['claim_blocked_reasons']})"
    )

    try:
        v = await api.upload_estimate(ACTOR, CASE, str(ESTIMATE_PDF), party_id="P1")
        summary(f"2. 견적서 PDF 업로드({ESTIMATE_PDF.name}) → 자동 추출·재산정", v)
    except EstimateReaderUnavailable as exc:
        print(f"\n[2. 견적서 PDF 업로드] {exc} — 수동 견적(10,000,000원)과 증빙으로 대신 진행")
        v = await api.add_documents(ACTOR, CASE, [fx.estimate(), *fx.evidence()])
        summary("2. 견적 추가(수동) → 재산정", v)
    full_report(v)

    version = v["versions"]["report_version"]
    out = await api.confirm_report(ACTOR, CASE, version, counterparty=fx.counterparty())
    report, pkg = out["report"], out["package"]
    print(f"\n[3. 담당자 확인 완료 — 보고서 v{version}]")
    print(
        f"  {report['approval_status']} · 확인자 {report['approved_by']} · {report['approved_at']}"
        f" · 청구서 없음 사유: {out['claim_blocked_reasons'] or '없음'}"
    )

    row = pkg["rows"][0]
    print(
        f"\n[4. 구상 청구서]\n  패키지 v{pkg['package_version']} · 근거 보고서 v"
        f"{pkg['report_version']} · "
        f"상대 과실 {pkg['counterparty_fault']}% · 확인 {pkg['confirmed_by']}"
    )
    print(f"  표시: {pkg['rule_notice']}")
    for a in pkg["assumptions"]:
        print(f"  기본 가정: {a}")
    print(
        f"  구상 1행: {row['claim_no']} · 보험가입자 {row['insured_party_id']} · {row['coverage']} "
        "· "
        f"산정 보험금 {row['insurance_payment']:,}원 × {row['counterparty_fault']}% = "
        f"{row['planned_share']:,}원 · 증빙 {row['evidence_count']}건"
    )
    print(f"  금액: {pkg['amounts']}")
    print(f"  증빙: {[e['파일'] + ' (' + e['문서 유형'] + ')' for e in pkg['evidence']]}")
    print(f"  증빙 누락: {pkg['missing_evidence'] or '없음'} · 규칙 {pkg['rule_version']}")

    OUT.mkdir(exist_ok=True)
    (OUT / "report.html").write_text(report_html(report), encoding="utf-8")
    (OUT / "claim_package.html").write_text(package_html(pkg), encoding="utf-8")
    print(f"\n저장: {OUT / 'report.html'}\n      {OUT / 'claim_package.html'}")


if __name__ == "__main__":
    asyncio.run(main())
