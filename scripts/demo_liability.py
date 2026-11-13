"""D 시연: 대물·대인 1차 판정 — 상대 차 견적서·다친 사람·대물 입력 → 보고서 8번 새 칸 → 청구서.

실행: py -3.12 scripts/demo_liability.py   (결과 HTML은 demo_out/에 저장, 커밋하지 않음)
      py -3.12 scripts/demo_liability.py --corpus   (로컬 코퍼스로 8·9번 약관 근거 조항까지)
- 흐름: 판단 반영(우리 P1 = 도표 A, 우리 과실 30%) → 우리 견적서 PDF → 상대 차 견적서 PDF
  (party_id=P2) → 다친 사람 2명(injured_person)·대물 입력(property_damage_input) → 보고서 8번
  「대물·대인 1차 판정」 → 확인 완료 → 구상 청구서.
- 상대 차 견적서는 우리 견적서와 다른 PDF(tests/fixtures/estimate_sample_cp_3m.pdf, 3,456,789원)다.
  같은 PDF는 서류 ID(내용 해시)가 같아 다시 올려도 무시된다.
- 숫자 자료(급수별 대인Ⅰ 한도·위자료 등): --corpus면 코퍼스 schedule_value(1118 판 171개 중
  계산 가능 52개), 아니면 app.ingestion.injury_property_values.build()(같은 52개, PDF 대조 검수).
- --corpus: data/private/corpus-db-import/db_import로 약관 색인·로컬 EvidenceStore를 연결해 8·9번
  근거 조항을 붙인다(임베딩·LLM 호출 없음). 면책은 담당자 입력 미입력 → 기본 가정이다.
- 가상 사건(tests/fixtures_d.py). 실제 사고·약관 계약·금액이 아니다.
"""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
if hasattr(sys.stdout, "reconfigure"):  # 테스트가 출력을 가로채면 없다
    sys.stdout.reconfigure(errors="replace")

from app.api import reports as api  # noqa: E402
from app.calculation.estimate_reader import EstimateReaderUnavailable  # noqa: E402
from app.ingestion import injury_property_values  # noqa: E402
from app.reports.render import package_html, report_html  # noqa: E402
from app.retrieval.embedding import RetrievalNotConfigured  # noqa: E402
from tests import fixtures_d as fx  # noqa: E402

ACTOR, CASE = "handler-1", "synthetic-case"
OUT = ROOT / "demo_out"
FIXTURES = ROOT / "tests" / "fixtures"
OUR_PDF = FIXTURES / "estimate_sample_10m.pdf"  # 우리 차(P1) 견적서 10,000,000원
COUNTERPARTY_PDF = FIXTURES / "estimate_sample_cp_3m.pdf"  # 상대 차(P2) 견적서 3,456,789원
DOCUMENT_ID = "samsung-personal-auto-2026-11-11"
KEY = "대물·대인 1차 판정"
RECEIVED = "2026-10-26T10:00:00+09:00"
LIABILITY_INPUTS = [
    {
        "kind": "injured_person",
        "document_id": "demo-inj-driver",
        "person_id": "P2-driver",
        "label": "상대 운전자",
        "grade": 7,
        "treatment_cost": 3_000_000,
        "is_driver": True,
        "received_at": RECEIVED,
    },
    {
        "kind": "injured_person",
        "document_id": "demo-inj-passenger",
        "person_id": "P2-passenger",
        "label": "상대 동승자",
        "grade": 3,
        "treatment_cost": 15_000_000,
        "is_driver": False,
        "received_at": RECEIVED,
    },
    {
        "kind": "property_damage_input",
        "document_id": "demo-pd-1",
        "property_damage_limit": 200_000_000,
        "vehicle_value": 15_000_000,
        "release_date": "2025-03-01",
        "received_at": RECEIVED,
    },
]


def _section(report, no: int) -> dict:
    return next(s for s in report["sections"] if s["no"] == no)


def _fmt(v: object) -> str:
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)


def connect_local_corpus() -> tuple[dict, ...] | None:
    """약관 색인 + 로컬 EvidenceStore + fetch_by_parent. 코퍼스의 schedule_value를 돌려준다."""
    from app.retrieval.common import configure_retrieval, fetch_by_parent
    from app.retrieval.evidence_store import LocalCorpusEvidenceStore
    from app.retrieval.loader import build_index, read_corpus
    from app.tools._evidence import configure_evidence_store

    try:
        corpus = read_corpus()
    except RetrievalNotConfigured as exc:
        print(f"[코퍼스 없음] {exc} — 근거 조항 없이, 숫자 자료는 build()로 진행")
        return None
    configure_retrieval(
        index=build_index(corpus, ["policy_terms"]), default_terms_edition=corpus.terms_edition()
    )
    store = LocalCorpusEvidenceStore(corpus)
    configure_evidence_store(lambda: store)
    api.configure(fetch_clauses=fetch_by_parent)
    values = tuple(r["body"] for r in corpus.records if r.get("kind") == "schedule_value")
    ready = sum(v.get("calculation_ready") is True for v in values)
    print(
        f"[코퍼스 연결] 약관 판 {corpus.terms_edition()} · 8·9번 근거 조항 직접 조회 · "
        f"schedule_value {len(values)}개(계산 가능 {ready})"
    )
    return values


async def _estimate(path: Path, party_id: str, fallback_amount: int, doc_id: str):
    try:
        return await api.upload_estimate(ACTOR, CASE, str(path), party_id=party_id)
    except EstimateReaderUnavailable as exc:  # pdfplumber 없음 → 수동 견적으로 대신
        print(f"  ({exc} — 수동 견적 {fallback_amount:,}원으로 대신)")
        doc = fx.estimate(fallback_amount, doc_id=doc_id, party_id=party_id)
        return await api.add_documents(ACTOR, CASE, [doc])


def liability_block(report) -> None:
    body = _section(report, 8)["body"].get(KEY)
    print(f"\n[보고서 v{report['versions']['report_version']} — 8번 「{KEY}」]")
    if body is None:
        print("  (칸 없음 — 대물·대인 입력이 없는 사건)")
        return
    print(f"  {body['주의']}")
    print(f"  우리 과실: {body['우리 과실']}")
    p = body.get("대물")
    if p:
        print(f"  대물 · {p['상태']} · {p['계산식']}")
        for f in p["확인 필요"]:
            print(f"    - 확인 필요: {f['항목']} — {f['내용']}")
        for r in p["사유"] + p["기본 가정"]:
            print(f"    · {r}")
    for i in body.get("대인", []):
        print(f"  대인 · {i['피해자']}({i['상해 급수']}) · {i['상태']}")
        print(f"    {i['계산식']}")
        for r in i["사유"]:
            print(f"    · {r}")
    if body.get("대인 합계"):
        print(f"  대인 합계: {_fmt(body['대인 합계'])}")
    ids = _section(report, 8)["evidence_ids"]
    values = [e for e in ids if e.startswith(f"{DOCUMENT_ID}:별표")]
    print(f"  8번 근거: {len(ids)}개 (숫자 자료 record_id {len(values)}개)")
    for e in values:
        print(f"    - {e}")


async def main(out: Path = OUT, *, corpus: bool = False) -> dict:
    """시연을 끝까지 돌리고 마지막 보고서·청구서를 돌려준다(테스트에서도 부른다)."""
    api.configure(repo=api.InMemoryReportRepository(), fetch_clauses=None)
    values = connect_local_corpus() if corpus else None
    if values is None:
        values = tuple(injury_property_values.build({"document_id": DOCUMENT_ID}))
        print(f"[숫자 자료] injury_property_values.build() {len(values)}개(PDF 대조 검수)")
    api.configure(schedule_values=values)

    v1 = await api.submit_judgment(ACTOR, fx.card(), fx.judgment())
    print(f"\n[1. 판단 반영] 보고서 v{v1['versions']['report_version']} · 우리(P1) 과실 30%")
    v = await _estimate(OUR_PDF, "P1", 10_000_000, "demo-our-estimate")
    calc = v["calculation"]
    print(
        f"[2. 우리 견적서 PDF] v{v['versions']['report_version']} · 자차 보험금 "
        f"{calc['insurance_payment']:,}원"
    )
    v = await _estimate(COUNTERPARTY_PDF, "P2", 3_456_789, "demo-cp-estimate")
    print(
        f"[3. 상대 차 견적서 PDF(party_id=P2)] v{v['versions']['report_version']} · 대물 "
        f"{v['liability']['property']['amount']:,}원(자차 보험금 그대로 "
        f"{v['calculation']['insurance_payment']:,}원)"
    )
    v = await api.add_documents(ACTOR, CASE, LIABILITY_INPUTS)
    print(f"[4. 다친 사람 2명·대물 입력] v{v['versions']['report_version']}")
    liability_block(v)

    version = v["versions"]["report_version"]
    done = await api.confirm_report(ACTOR, CASE, version, counterparty=fx.counterparty())
    report, pkg = done["report"], done["package"]
    print(f"\n[5. 담당자 확인 완료 — 보고서 v{version}] {report['approval_status']}")
    if pkg:
        row = pkg["rows"][0]
        print(
            f"[6. 구상 청구서] {row['claim_no']} · 산정 보험금 {row['insurance_payment']:,}원 × "
            f"{row['counterparty_fault']}% = {row['planned_share']:,}원 (대물·대인과 별개)"
        )
    else:
        print(f"[6. 구상 청구서 없음] {done['claim_blocked_reasons']}")

    out.mkdir(parents=True, exist_ok=True)
    (out / "liability_report.html").write_text(report_html(report), encoding="utf-8")
    if pkg:
        (out / "liability_claim_package.html").write_text(package_html(pkg), encoding="utf-8")
    print(f"\n저장: {out / 'liability_report.html'}")
    if pkg:
        print(f"      {out / 'liability_claim_package.html'}")
    return done


if __name__ == "__main__":
    asyncio.run(main(corpus="--corpus" in sys.argv[1:]))
