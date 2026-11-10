"""D: 1차 판정 초안 보고서·산정·구상 API 진입점 (서비스 함수, HTTP 미연결).

확정 흐름 (단계별 승인 없음, 담당자 확인은 마지막 한 번)
1. submit_judgment  — 판단 결과(draft_ready) → 보고서 생성과 동시에 산정, 8·9번까지 자동으로 채움.
                      견적서가 없으면 금액 null. 추가 확인·보류면 산정하지 않음.
2. add_documents    — 견적·계약·증빙 추가 → 자동 재산정.
                      입력이 바뀌면 새 보고서 버전(확인 완료 풀림)
   upload_estimate  — 서식 견적서 PDF 자동 추출 후 add_documents
3. set_agreed_fault — 협의 과실 입력·해제(이유 필수) → 9번 다시 계산한 새 버전
4. confirm_report   — 담당자 [확인 완료] 한 번: 최신·draft_ready면 확인 기록.
                      청구서 조건이 맞으면 구상 청구 패키지 생성, 아니면 package=None +
                      claim_blocked_reason(견적서 필요·지급 불가·상대 과실 0% 등)
기본 가정(1차 판정 초안): 계약 서류 없음 → 자차 가입·보험기간 안·운전자 범위 안(한도 검사 생략),
자기부담금 → rules.py 기본 규칙, 면책 사실 미입력 → 해당 없음.
가정은 8번·청구서에 표시하고 막지 않는다.
HTTP 라우터는 D 통합(app/main.py)에서 이 함수들을 감싸 붙인다.
"""

from dataclasses import dataclass, field
from datetime import UTC

from app.calculation.estimate_reader import read_estimate_pdf
from app.calculation.exclusions import (
    EXCLUSION_FACT_KEYS,
    Judge,
    flag_judge,
    llm_judge,
    openai_judge_llm,
    review_exclusions,
    review_exclusions_from_facts,
)
from app.calculation.insurance import calculate_insurance
from app.calculation.rules import DEFAULT_RULES, RuleSet
from app.claims.package import ClaimPackageBlocked, build_claim_package, compute_recovery
from app.reports.assembler import assemble_review
from app.reports.render import package_html
from app.reports.repository import CaseRecord, InMemoryReportRepository
from app.retrieval.common import configure_retrieval, fetch_by_parent
from app.retrieval.embedding import OpenAIEmbedder
from app.retrieval.loader import handoff_terms_edition, load_index
from app.schemas.facts import FactCard
from app.schemas.judgment import JudgmentResult
from app.schemas.reports import CounterpartyInfo, RecoveryAmounts, ReviewReport

__all__ = [
    "ClaimPackageBlocked",
    "ConfirmBlocked",
    "InMemoryReportRepository",
    "StaleVersion",
    "add_documents",
    "configure",
    "configure_with_knowledge_base",
    "confirm_report",
    "get_report",
    "services",
    "set_agreed_fault",
    "submit_judgment",
    "upload_estimate",
]


class StaleVersion(ValueError):
    """최신이 아닌 보고서 버전에 대한 확인 요청."""


class ConfirmBlocked(ValueError):
    """확인 완료할 수 없는 보고서(추가 확인·보류 판단)."""


@dataclass
class ReportServices:
    """바꿔 끼우는 의존성. 진짜 구현이 오면 configure()로 교체한다."""

    repo: InMemoryReportRepository = field(default_factory=InMemoryReportRepository)
    rules: RuleSet = DEFAULT_RULES
    judge: Judge = flag_judge
    # app.retrieval.common.fetch_by_parent. 없으면 면책은 담당자 입력으로 판정
    fetch_clauses: object | None = None


services = ReportServices()


def configure(**kw: object) -> ReportServices:
    global services
    services = ReportServices(**{**services.__dict__, **kw})  # type: ignore[arg-type]
    return services


def configure_with_knowledge_base() -> ReportServices:
    """지식베이스 연결을 한 번에 켠다. 통합(app/main.py)에서는 시작할 때 이 함수만 호출하면 된다.

    실제 색인 적재 + OpenAIEmbedder(C 도구 검색용) + fetch_clauses + llm_judge.
    키·모델·패키지가 없으면 RetrievalNotConfigured를 내고 아무것도 바꾸지 않는다. 호출하지 않으면
    면책은 담당자 입력으로 판정한다. 색인을 메모리에 올리므로 시작 시간과 메모리를 쓴다.
    """
    embedder = OpenAIEmbedder.from_env()  # 설정을 먼저 확인하고 오래 걸리는 적재는 나중에
    judge = llm_judge(openai_judge_llm())
    index = load_index()
    edition = handoff_terms_edition()
    configure_retrieval(index=index, embedder=embedder, default_terms_edition=edition)
    return configure(fetch_clauses=fetch_by_parent, judge=judge)


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


async def _exclusions(rec: CaseRecord):
    """면책 확인. 조항 조회가 없거나 미구현이면 담당자 입력(case_facts)으로 판정한다."""
    if services.fetch_clauses is None:
        return review_exclusions_from_facts(rec.case_facts, rules=services.rules)
    contract = next(
        (d for d in reversed(rec.documents) if d.get("kind") == "own_damage_contract"), None
    )
    try:
        return await review_exclusions(
            contract,
            rec.case_facts,  # type: ignore[arg-type]
            fetch=services.fetch_clauses,
            judge=services.judge,
            rules=services.rules,
        )  # type: ignore[arg-type]
    except NotImplementedError:
        return review_exclusions_from_facts(rec.case_facts, rules=services.rules)


def _recovery(rec: CaseRecord, base: ReviewReport, calc) -> RecoveryAmounts:
    """9번 자동 작성. 협의 과실이 있으면 그 값, 해제 기록이면 판단 과실 + 해제 이유."""
    agreed = rec.fault_agreement.get("agreed_counterparty_fault")
    reason = rec.fault_agreement.get("fault_change_reason")
    recovery = compute_recovery(
        base,
        calc,
        agreed_counterparty_fault=agreed,  # type: ignore[arg-type]
        fault_change_reason=reason if agreed is not None else None,
    )  # type: ignore[arg-type]
    if rec.fault_agreement and agreed is None:  # 협의 해제 기록: 과실은 None, 이유는 남긴다
        recovery["agreed_counterparty_fault"] = None
        recovery["fault_change_reason"] = reason  # type: ignore[typeddict-item]
    return recovery


async def _draft(
    rec: CaseRecord, judgment: JudgmentResult, reason: str, education_mode: bool
) -> ReviewReport:
    """보고서 한 버전 조립. draft_ready면 바로 산정해 8·9번까지 채운다(저장은 호출자가)."""
    kw = {
        "card": rec.card,
        "previous": rec.latest,
        "reason": reason,
        "education_mode": education_mode,
    }
    base = assemble_review(judgment, now=_now(), **kw)  # type: ignore[arg-type]
    if judgment["status"] != "draft_ready" or judgment["fault_A"] is None:
        return base
    calc = calculate_insurance(
        base, rec.documents, exclusion_checks=await _exclusions(rec), rules=services.rules
    )
    return assemble_review(judgment, calc, recovery=_recovery(rec, base, calc), now=_now(), **kw)  # type: ignore[arg-type]


def validate_case_facts(case_facts: dict[str, object]) -> None:
    """면책 체크박스 입력 검사. 키는 무면허·음주·고의만, 값은 True/False/None(미입력)만."""
    extra = [k for k in case_facts if k not in EXCLUSION_FACT_KEYS]
    if extra:
        raise ValueError(
            f"case_facts에 허용되지 않은 키: {', '.join(map(str, extra))} (허용: "
            f"{', '.join(EXCLUSION_FACT_KEYS)})"
        )
    bad = [k for k, v in case_facts.items() if v is not None and not isinstance(v, bool)]
    if bad:
        raise ValueError(f"case_facts 값은 true/false/null만 허용: {', '.join(bad)}")


def _same_inputs(a: ReviewReport | None, b: ReviewReport) -> bool:
    if a is None or a["judgment"] != b["judgment"]:
        return False
    ca, cb = a.get("calculation"), b.get("calculation")
    if ca is None or cb is None:
        return ca is cb
    return ca.get("inputs_hash") == cb.get("inputs_hash") and a.get("recovery") == b.get("recovery")


async def submit_judgment(
    actor_id: str, card: FactCard, judgment: JudgmentResult, *, education_mode: bool = False
) -> ReviewReport:
    """B가 판단을 끝내면 호출. 카드가 확정 상태여야 한다. draft_ready면 산정까지 한 보고서가
    나온다."""
    if card["status"] != "confirmed":
        raise ValueError("확정되지 않은 사실 카드입니다")
    rec = services.repo.open(card["case_id"], actor_id)
    if rec.owner_id != actor_id:
        raise PermissionError("담당 사건이 아닙니다")
    rec.card = card
    report = await _draft(rec, judgment, "판단 결과 반영", education_mode)
    rec.reports.append(report)
    return report


async def get_report(actor_id: str, case_id: str, version: int) -> ReviewReport:
    return services.repo.get(actor_id, case_id).report(version)


async def add_documents(
    actor_id: str,
    case_id: str,
    documents: list[dict[str, object]],
    case_facts: dict[str, object] | None = None,
) -> ReviewReport | None:
    """서류 추가 → 자동 재산정. 금액·검증 입력이 바뀌면 새 버전(다시 확인 완료 필요), 같으면 최신
    버전 그대로.
    판단·비전은 다시 돌리지 않는다. 추가 확인·보류 사건이면 서류만 저장하고 None."""
    rec = services.repo.get(actor_id, case_id)
    if case_facts:
        validate_case_facts(case_facts)  # 서류 저장 전에 검사(잘못된 입력이면 아무것도 바꾸지 않음)
    known = {str(d["document_id"]) for d in rec.documents}
    added = [
        d for d in documents if str(d["document_id"]) not in known
    ]  # 같은 ID 재전송은 무시(멱등)
    rec.documents += added
    if case_facts:
        rec.case_facts.update(case_facts)
    base = rec.latest_ready()
    if base is None:
        return None
    kinds = ", ".join(sorted({str(d.get("kind")) for d in added})) or "입력 변경"
    draft = await _draft(
        rec, base["judgment"], f"서류 추가({kinds}) — 재산정", bool(base.get("education_mode"))
    )
    if _same_inputs(base, draft):
        return base
    rec.reports.append(draft)
    return draft


async def upload_estimate(
    actor_id: str, case_id: str, pdf_path: str, party_id: str
) -> ReviewReport | None:
    """서식 견적서 PDF 업로드 → 자동 추출(read_by="pdf_extract") → add_documents(자동 재산정).
    pdfplumber가 없으면 EstimateReaderUnavailable(설치 안내)."""
    services.repo.get(actor_id, case_id)  # 권한·사건 확인을 PDF 읽기보다 먼저
    doc = read_estimate_pdf(pdf_path, party_id=party_id)
    evidence = {
        "kind": "evidence",
        "document_id": f"ev-{doc['document_id']}",
        "doc_type": "repair_estimate",
        "storage_path": pdf_path,
    }  # 견적서 PDF는 청구서 증빙으로 자동 등록
    return await add_documents(actor_id, case_id, [doc, evidence])  # type: ignore[list-item]


async def set_agreed_fault(
    actor_id: str,
    case_id: str,
    *,
    agreed_counterparty_fault: int | None = None,
    fault_change_reason: str | None = None,
    clear: bool = False,
) -> ReviewReport:
    """협의 과실 입력(이유 필수) 또는 clear=True로 판단 과실로 되돌리기(이유 필수).
    9번을 다시 계산한 새 버전을 만든다(다시 확인 완료 필요). 이후 재산정에도 이 값을 이어 쓴다."""
    rec = services.repo.get(actor_id, case_id)
    base = rec.latest_ready()
    if base is None or not base.get("calculation"):
        raise ValueError("산정 결과가 있는 보고서가 필요합니다")
    if not fault_change_reason:
        raise ValueError("협의 과실을 바꾸거나 되돌리면 이유를 함께 입력해야 합니다")
    if clear:
        if agreed_counterparty_fault is not None:
            raise ValueError("협의 과실 되돌리기와 새 협의 과실 입력은 함께 할 수 없습니다")
        if rec.fault_agreement.get("agreed_counterparty_fault") is None:
            raise ValueError("되돌릴 협의 과실이 없습니다")
    elif agreed_counterparty_fault is None:
        raise ValueError("협의 상대 과실(%)을 입력하세요 (되돌리려면 clear=True)")
    elif not 0 <= agreed_counterparty_fault <= 100:
        raise ValueError("상대 과실은 0~100% 사이여야 합니다")
    rec.fault_agreement = {
        "agreed_counterparty_fault": None if clear else agreed_counterparty_fault,
        "fault_change_reason": fault_change_reason,
    }
    report = await _draft(
        rec,
        base["judgment"],
        "협의 과실 해제" if clear else "협의 과실 입력",
        bool(base.get("education_mode")),
    )
    rec.reports.append(report)
    return report


async def confirm_report(
    actor_id: str, case_id: str, version: int, *, counterparty: CounterpartyInfo | None = None
) -> dict[str, object]:
    """담당자 [확인 완료] 한 번. 막는 경우는 최신 버전 아님(StaleVersion)·draft_ready
    아님(ConfirmBlocked) 두 가지뿐.
    확인 기록을 남기고, 청구서 조건이 맞으면 구상 청구 패키지를 만든다. 아니면 package/html=None과
    사유를 돌려준다.
    결과: {report, package, html, claim_blocked_reasons, claim_blocked_reason}.
    claim_blocked_reasons는 해당 사유 전부, claim_blocked_reason은 그 첫 번째 값(없으면 None).
    같은 입력으로 다시 확인하면 같은 패키지를 돌려준다."""
    rec = services.repo.get(actor_id, case_id)
    report = rec.report(version)
    if report is not rec.latest:
        raise StaleVersion("최신 버전만 확인 완료할 수 있습니다")
    if report["judgment"]["status"] != "draft_ready":
        raise ConfirmBlocked("추가 확인·보류 상태의 보고서는 확인 완료할 수 없습니다")
    now = _now()
    if report["approval_status"] != "approved":
        report.update({"approval_status": "approved", "approved_by": actor_id, "approved_at": now})
    try:
        pkg = build_claim_package(
            report,
            documents=rec.documents,
            counterparty=counterparty,
            confirmed_by=actor_id,
            package_version=len(rec.packages) + 1,
            rules=services.rules,
            now=now,
        )
    except ClaimPackageBlocked as exc:
        return {
            "report": report,
            "package": None,
            "html": None,
            "claim_blocked_reasons": list(exc.reasons),
            "claim_blocked_reason": exc.reasons[0],
        }
    same = next((p for p in rec.packages if p["idempotency_key"] == pkg["idempotency_key"]), None)
    if same is None:
        rec.packages.append(pkg)
    out = same or pkg
    return {
        "report": report,
        "package": out,
        "html": package_html(out),
        "claim_blocked_reasons": [],
        "claim_blocked_reason": None,
    }
