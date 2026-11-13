"""D: 1차 판정 초안 보고서·산정·구상 API 진입점 (서비스 함수, HTTP 미연결).

확정 흐름 (단계별 승인 없음, 담당자 확인은 마지막 한 번)
1. submit_judgment  — 판단 결과(draft_ready) → 보고서 생성과 동시에 산정, 8·9번까지 자동으로 채움.
                      견적서가 없으면 금액 null. 추가 확인·보류면 산정하지 않음.
2. add_documents    — 견적·계약·증빙 추가 → 자동 재산정.
                      입력이 바뀌면 새 보고서 버전(확인 완료 풀림)
   upload_estimate  — 서식 견적서 PDF 자동 추출 후 add_documents
   upload_estimate_from_storage — 견적서 경로(POST /cases/{id}/estimates): 권한·경로 확인 후
                      Storage에서 임시 파일로 내려받아 upload_estimate(저장 경로는 Storage 경로)
3. set_agreed_fault — 협의 과실 입력·해제(이유 필수) → 9번 다시 계산한 새 버전
4. confirm_report   — 담당자 [확인 완료] 한 번: 최신·draft_ready면 확인 기록.
                      청구서 조건이 맞으면 구상 청구 패키지 생성, 아니면 package=None +
                      claim_blocked_reason(견적서 필요·지급 불가·상대 과실 0% 등)
5. get_claim        — 그 보고서 버전으로 만든 청구서 다시 조회
기본 가정(1차 판정 초안): 계약 서류 없음 → 자차 가입·보험기간 안·운전자 범위 안(한도 검사 생략),
자기부담금 → rules.py 기본 규칙, 면책 사실 미입력 → 해당 없음.
가정은 8번·청구서에 표시하고 막지 않는다.
HTTP 경로는 app/api/routes.py(구준휘 님)가 이 함수들을 감싸 붙인다.
경로 표는 docs/d-claims-reports.md.
오류: 내부 예외는 그대로 두고, 공개 서비스 함수 경계(_api_boundary)에서만 app/api/errors.py의
ApiException으로 바꾼다 — 없음 404, 담당 아님 403, 최신 아님 409, 처리 불가 입력·상태 422.
"""

import os
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date
from functools import wraps
from pathlib import Path

from app import storage
from app.api.errors import ApiException, forbidden, not_found, unprocessable, version_conflict
from app.calculation.damages import received_at
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
from app.calculation.liability import liability_from_report
from app.calculation.rules import DEFAULT_RULES, RuleSet
from app.claims.package import ClaimPackageBlocked, build_claim_package, compute_recovery
from app.reports.assembler import assemble_review
from app.reports.clauses import ReportClauses, fetch_report_clauses, select_report_clauses
from app.reports.render import package_html
from app.reports.repository import (
    CaseRecord,
    InMemoryReportRepository,
    NotCaseOwner,
    PostgresReportRepository,
    RecordNotFound,
    ReportsDbNotReady,
    VersionTaken,
    default_repository,
)
from app.retrieval.common import configure_retrieval, fetch_by_parent
from app.retrieval.embedding import (
    EmbeddingMismatch,
    EmbeddingSpec,
    OpenAIEmbedder,
    RetrievalNotConfigured,
)
from app.retrieval.evidence_store import local_evidence_store
from app.retrieval.loader import CorpusDataError, build_index, read_corpus
from app.schemas.facts import FactCard
from app.schemas.judgment import JudgmentResult
from app.schemas.reports import (
    ClaimResult,
    ConfirmResult,
    CounterpartyInfo,
    RecoveryAmounts,
    ReviewReport,
)
from app.tools._evidence import configure_evidence_store

__all__ = [
    "AgreedFaultInvalid",
    "CardNotConfirmed",
    "CaseFactsInvalid",
    "ClaimPackageBlocked",
    "ConfirmBlocked",
    "DocumentsInvalid",
    "InMemoryReportRepository",
    "PostgresReportRepository",
    "StaleVersion",
    "add_documents",
    "configure",
    "EmbeddingSettingsInvalid",
    "configure_with_knowledge_base",
    "confirm_report",
    "get_claim",
    "get_report",
    "services",
    "set_agreed_fault",
    "submit_judgment",
    "upload_estimate",
    "upload_estimate_from_storage",
]


class EmbeddingSettingsInvalid(RetrievalNotConfigured):
    """.env의 질의 임베딩 설정(OPENAI_EMBEDDING_*)이 handoff.json과 다르거나 형식이 틀렸다.

    OpenAIEmbedder.from_env의 EmbeddingMismatch를 연결 함수에서만 감싼다(원인은 __cause__).
    """


class StaleVersion(ValueError):
    """최신이 아닌 보고서 버전에 대한 확인 요청."""


class ConfirmBlocked(ValueError):
    """확인 완료할 수 없는 보고서(추가 확인·보류 판단)."""


class CaseFactsInvalid(ValueError):
    """면책 체크박스(case_facts) 입력이 허용 키·값이 아님."""


class CardNotConfirmed(ValueError):
    """확정되지 않은 사실 카드로 판단 결과를 넘김."""


class AgreedFaultInvalid(ValueError):
    """협의 과실 입력·해제 요청을 처리할 수 없음(이유 없음, 범위 밖, 산정 결과 없음 등)."""


class DocumentsInvalid(ValueError):
    """서류 모양이 아님(document_id 없음, kind가 견적서·자차 계약·증빙이 아님)."""


class StoragePathInvalid(ValueError):
    """견적서 Storage 경로가 그 사건의 견적서 폴더가 아님(다른 사건, '..' 등)."""


class EstimateNotPdf(ValueError):
    """내려받은 견적서가 PDF가 아니거나 PDF로 읽을 수 없음."""


DOCUMENT_KINDS = (
    "repair_estimate",
    "own_damage_contract",
    "evidence",
    # 계약 변경(C): 대물·대인 담당자 입력(app/schemas/reports.py, docs/contracts/liability-input.md)
    "injured_person",
    "property_damage_input",
)
# 견적서 Storage 내려받기(upload_estimate_from_storage)
ESTIMATE_MAX_BYTES = 10 * 1024 * 1024
PDF_HEADER = b"%PDF-"
# PDF 규격상 앞에 다른 바이트가 붙어도 앞 1024바이트 안에 머리가 있으면 정상
PDF_HEADER_WINDOW = 1024


# 하위 클래스를 먼저 본다(StaleVersion·ConfirmBlocked 등은 ValueError).
_API_ERRORS: tuple[tuple[type[Exception], Callable[[Exception], ApiException]], ...] = (
    (StaleVersion, lambda e: version_conflict(str(e))),
    (VersionTaken, lambda e: version_conflict(str(e))),
    (ConfirmBlocked, lambda e: unprocessable("confirm_blocked", str(e))),
    (CaseFactsInvalid, lambda e: unprocessable("invalid_case_facts", str(e))),
    (CardNotConfirmed, lambda e: unprocessable("card_not_confirmed", str(e))),
    (AgreedFaultInvalid, lambda e: unprocessable("invalid_agreed_fault", str(e))),
    (DocumentsInvalid, lambda e: unprocessable("invalid_documents", str(e))),
    (StoragePathInvalid, lambda e: unprocessable("invalid_storage_path", str(e))),
    (EstimateNotPdf, lambda e: unprocessable("estimate_not_pdf", str(e))),
    (storage.StorageObjectTooLarge, lambda e: unprocessable("estimate_too_large", str(e))),
    (
        storage.StorageObjectNotFound,
        lambda e: ApiException(404, "estimate_file_not_found", str(e)),
    ),
    (
        ReportsDbNotReady,
        lambda e: ApiException(
            503,
            "reports_db_not_ready",
            "보고서 저장소가 아직 준비되지 않았습니다(DB 마이그레이션 적용 전).",
            True,
        ),
    ),
    (NotCaseOwner, lambda e: forbidden(str(e))),
    (RecordNotFound, lambda e: not_found(str(e))),
)


def _api_boundary[**P, R](fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """공개 서비스 함수 경계: 알려진 내부 예외만 ApiException으로 바꾼다. 나머지는 그대로(500)."""

    @wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            for kind, to_api in _API_ERRORS:
                if isinstance(exc, kind):
                    raise to_api(exc) from exc
            raise

    return wrapper


@dataclass
class ReportServices:
    """바꿔 끼우는 의존성. 진짜 구현이 오면 configure()로 교체한다."""

    # DATABASE_URL이 있으면 Postgres, 없으면 메모리(테스트·데모). app/reports/repository.py
    repo: InMemoryReportRepository | PostgresReportRepository = field(
        default_factory=default_repository
    )
    rules: RuleSet = DEFAULT_RULES
    judge: Judge = flag_judge
    # app.retrieval.common.fetch_by_parent. 없으면 면책은 담당자 입력으로 판정
    fetch_clauses: object | None = None
    # 약관 schedule_value 레코드 body(대물·대인 숫자). 없으면 대인은 "자료 없음"으로 금액 없음
    schedule_values: tuple[dict, ...] = ()


services = ReportServices()


def configure(**kw: object) -> ReportServices:
    global services
    services = ReportServices(**{**services.__dict__, **kw})  # type: ignore[arg-type]
    return services


def configure_with_knowledge_base(
    base: Path | None = None, *, import_evidence_store: bool = False
) -> ReportServices:
    """지식베이스 연결을 한 번에 켠다. 통합(app/main.py)에서는 시작할 때 이 함수만 호출하면 된다.

    실제 색인 적재(db_import 파일) + OpenAIEmbedder(C 도구 검색용) + fetch_clauses + llm_judge.
    APP_ENV=local이고 Vercel이 아니면 같은 행으로 C의 EvidenceStore 로컬 어댑터도 등록한다
    (운영에서는 등록하지 않으므로 검증은 fail-closed 그대로).
    `base`는 db_import 폴더다. 없으면 로더 기본 경로(로컬 data/private)를 읽는다. 운영은 B가
    비공개 Storage에서 받아 SHA-256으로 검증한 사본 폴더를 넘긴다(app/corpus_import.py).
    `import_evidence_store`면 그 사본으로 운영에서도 로컬 어댑터를 등록한다(2026-11-13, 운영 판단용
    임시. CORPUS_IMPORT_EVIDENCE_STORE).
    키·모델·패키지가 없거나 코퍼스·handoff.json 파일이 없거나 못 쓰면 RetrievalNotConfigured
    (코퍼스 데이터 문제는 하위 클래스 CorpusDataError, .env 임베딩 모델·차원이 handoff와 다르거나
    틀리면 EmbeddingSettingsInvalid)를 내고 아무것도 바꾸지 않는다.
    모든 준비(읽기·색인·판·임베딩 비교·어댑터)를 끝낸 뒤에만 전역 설정을 바꾼다.
    호출하지 않으면 면책은 담당자 입력으로 판정한다. 색인을 메모리에 올리므로
    시작 시간과 메모리를 쓴다.
    """
    try:  # 설정을 먼저 확인하고 오래 걸리는 적재는 나중에
        embedder = OpenAIEmbedder.from_env()
    except EmbeddingMismatch as exc:  # 메시지는 모델·차원뿐이다(키는 담지 않음)
        raise EmbeddingSettingsInvalid(f"D: 질의 임베딩 설정이 맞지 않습니다: {exc}") from exc
    judge = llm_judge(openai_judge_llm())
    corpus = read_corpus() if base is None else read_corpus(base)
    index = build_index(corpus)
    edition = corpus.terms_edition()
    query_spec = EmbeddingSpec(embedder.model, embedder.dimensions, embedder.version)
    if index.spec != query_spec:  # 검색 때가 아니라 시작 때 드러낸다
        raise CorpusDataError(
            f"D: 코퍼스 임베딩 {index.spec}이 질의 임베더 {query_spec}와 다릅니다"
        )
    store = (local_evidence_store(corpus, allow_in_production=True) if import_evidence_store
             else local_evidence_store(corpus))
    # 여기부터 전역 설정. 위에서 실패하면 아무것도 바뀌지 않는다
    configure_retrieval(index=index, embedder=embedder, default_terms_edition=edition)
    if store is not None:
        configure_evidence_store(lambda: store)
    values = tuple(r["body"] for r in corpus.records if r.get("kind") == "schedule_value")
    return configure(fetch_clauses=fetch_by_parent, judge=judge, schedule_values=values)


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
    return assemble_review(
        judgment, calc, recovery=_recovery(rec, base, calc), now=_now(),
        clauses=await _report_clauses(base, calc),
        liability=liability_from_report(
            base,
            rec.documents,
            services.schedule_values,
            agreed_counterparty_fault=rec.fault_agreement.get("agreed_counterparty_fault"),  # type: ignore[arg-type]
        ),
        **kw,  # type: ignore[arg-type]
    )


def _accident_date(report: ReviewReport) -> str | None:
    fact = next((s for s in report["sections"] if s["no"] == 2), None)
    value = (fact or {}).get("body", {}).get("사고 일자")  # type: ignore[union-attr]
    return str(value) if value else None


async def _report_clauses(base: ReviewReport, calc) -> ReportClauses | None:
    """8·9번 약관 근거. 조항 조회가 없으면(코퍼스 없음·운영) None → 보고서에 "연결 안 됨"."""
    if services.fetch_clauses is None:
        return None
    accident_date = _accident_date(base)
    return await fetch_report_clauses(
        select_report_clauses(accident_date),
        fetch=services.fetch_clauses,  # type: ignore[arg-type]
        edition=calc.get("terms_edition"),
        accident_date=accident_date,
    )


def validate_documents(documents: list[dict[str, object]]) -> None:
    """서류 목록 검사. 각 서류는 document_id와 kind(견적서·자차 계약·증빙)가 있어야 한다."""
    if not isinstance(documents, list):
        raise DocumentsInvalid("documents는 목록이어야 합니다")
    for i, doc in enumerate(documents):
        if not isinstance(doc, dict) or not str(doc.get("document_id") or "").strip():
            raise DocumentsInvalid(f"documents[{i}]에 document_id가 없습니다")
        if doc.get("kind") not in DOCUMENT_KINDS:
            raise DocumentsInvalid(
                f"documents[{i}].kind는 {', '.join(DOCUMENT_KINDS)} 중 하나여야 합니다"
            )
        problem = _liability_input_problem(doc)
        if problem:
            raise DocumentsInvalid(f"documents[{i}]: {problem}")


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _liability_input_problem(doc: dict[str, object]) -> str | None:
    """대물·대인 입력 칸 검사. 모르는 값은 null(0으로 채우지 않음)."""
    kind = doc.get("kind")
    if kind not in ("injured_person", "property_damage_input"):
        return None
    try:
        received_at(doc)
    except (KeyError, TypeError, ValueError):
        return "received_at은 ISO 8601 시각이어야 합니다"
    if kind == "injured_person":
        if not str(doc.get("person_id") or "").strip():
            return "injured_person에 person_id가 없습니다"
        grade = doc.get("grade")
        if grade is not None and not (_is_int(grade) and 1 <= grade <= 14):  # type: ignore[operator]
            return "grade는 1~14 정수 또는 null이어야 합니다"
        cost = doc.get("treatment_cost")
        if cost is not None and not (_is_int(cost) and cost >= 0):  # type: ignore[operator]
            return "treatment_cost는 0 이상 정수(원) 또는 null이어야 합니다"
        if doc.get("is_driver") is not None and not isinstance(doc.get("is_driver"), bool):
            return "is_driver는 true/false/null이어야 합니다"
        if doc.get("label") is not None and not isinstance(doc.get("label"), str):
            return "label은 문자열 또는 null이어야 합니다"
        return None
    for key in ("property_damage_limit", "vehicle_value"):
        v = doc.get(key)
        if v is not None and not (_is_int(v) and v >= 0):  # type: ignore[operator]
            return f"{key}는 0 이상 정수(원) 또는 null이어야 합니다"
    release = doc.get("release_date")
    if release is not None:
        try:
            date.fromisoformat(str(release))
        except ValueError:
            return "release_date는 YYYY-MM-DD 날짜여야 합니다"
    return None


def validate_case_facts(case_facts: dict[str, object]) -> None:
    """면책 체크박스 입력 검사. 키는 무면허·음주·고의만, 값은 True/False/None(미입력)만."""
    extra = [k for k in case_facts if k not in EXCLUSION_FACT_KEYS]
    if extra:
        raise CaseFactsInvalid(
            f"case_facts에 허용되지 않은 키: {', '.join(map(str, extra))} (허용: "
            f"{', '.join(EXCLUSION_FACT_KEYS)})"
        )
    bad = [k for k, v in case_facts.items() if v is not None and not isinstance(v, bool)]
    if bad:
        raise CaseFactsInvalid(f"case_facts 값은 true/false/null만 허용: {', '.join(bad)}")


def _same_inputs(a: ReviewReport | None, b: ReviewReport) -> bool:
    if a is None or a["judgment"] != b["judgment"]:
        return False
    ca, cb = a.get("calculation"), b.get("calculation")
    if ca is None or cb is None:
        return ca is cb
    return (
        ca.get("inputs_hash") == cb.get("inputs_hash")
        and a.get("recovery") == b.get("recovery")
        and a.get("liability") == b.get("liability")  # 예전 보고서는 키가 없어 None
    )


@_api_boundary
async def submit_judgment(
    actor_id: str, card: FactCard, judgment: JudgmentResult, *, education_mode: bool = False
) -> ReviewReport:
    """B가 판단을 끝내면 호출. 카드가 확정 상태여야 한다. draft_ready면 산정까지 한 보고서가
    나온다."""
    if card["status"] != "confirmed":
        raise CardNotConfirmed("확정되지 않은 사실 카드입니다")
    rec = await services.repo.open(card["case_id"], actor_id)
    if rec.owner_id != actor_id:
        raise NotCaseOwner("담당 사건이 아닙니다")
    rec.card = card
    report = await _draft(rec, judgment, "판단 결과 반영", education_mode)
    rec.reports.append(report)
    await services.repo.save(rec, actor_id)
    return report


@_api_boundary
async def get_report(actor_id: str, case_id: str, version: int) -> ReviewReport:
    return (await services.repo.get(actor_id, case_id)).report(version)


@_api_boundary
async def add_documents(
    actor_id: str,
    case_id: str,
    documents: list[dict[str, object]],
    case_facts: dict[str, object] | None = None,
) -> ReviewReport | None:
    """서류 추가 → 자동 재산정. 금액·검증 입력이 바뀌면 새 버전(다시 확인 완료 필요), 같으면 최신
    버전 그대로.
    판단·비전은 다시 돌리지 않는다. 추가 확인·보류 사건이면 서류만 저장하고 None."""
    rec = await services.repo.get(actor_id, case_id)
    validate_documents(documents)  # 저장 전에 검사(잘못된 입력이면 아무것도 바꾸지 않음)
    for doc in documents:  # 진단서·영수증 증빙은 이 사건 medical/ 아래 파일만(서버 키는 RLS 밖)
        if doc.get("kind") == "evidence" and doc.get("doc_type") in MEDICAL_DOC_TYPES:
            medical_object_key(case_id, str(doc.get("storage_path") or ""))
    if case_facts:
        validate_case_facts(case_facts)
    known = {str(d["document_id"]) for d in rec.documents}
    added = [
        d for d in documents if str(d["document_id"]) not in known
    ]  # 같은 ID 재전송은 무시(멱등)
    rec.documents += added
    if case_facts:
        rec.case_facts.update(case_facts)
    base = rec.latest_ready()
    if base is None:
        await services.repo.save(rec, actor_id)
        return None
    kinds = ", ".join(sorted({str(d.get("kind")) for d in added})) or "입력 변경"
    draft = await _draft(
        rec, base["judgment"], f"서류 추가({kinds}) — 재산정", bool(base.get("education_mode"))
    )
    if not _same_inputs(base, draft):
        rec.reports.append(draft)
    await services.repo.save(rec, actor_id)
    return rec.latest


@_api_boundary
async def upload_estimate(
    actor_id: str, case_id: str, pdf_path: str, party_id: str, *, storage_path: str | None = None
) -> ReviewReport | None:
    """서식 견적서 PDF 업로드 → 자동 추출(read_by="pdf_extract") → add_documents(자동 재산정).

    `pdf_path`는 읽을 파일, `storage_path`는 견적서·증빙에 저장할 경로(없으면 `pdf_path`).
    pdfplumber가 없으면 EstimateReaderUnavailable(설치 안내)."""
    await services.repo.get(actor_id, case_id)  # 권한·사건 확인을 PDF 읽기보다 먼저
    stored_path = storage_path or pdf_path
    doc = read_estimate_pdf(pdf_path, party_id=party_id)
    doc["storage_path"] = stored_path
    evidence = {
        "kind": "evidence",
        "document_id": f"ev-{doc['document_id']}",
        "doc_type": "repair_estimate",
        "storage_path": stored_path,
    }  # 견적서 PDF는 청구서 증빙으로 자동 등록
    return await add_documents(actor_id, case_id, [doc, evidence])  # type: ignore[list-item]


def estimate_object_key(case_id: str, storage_path: str) -> str:
    """`<STORAGE_BUCKET>/<case_id>/estimates/<파일>`인지 확인하고 버킷을 뗀 객체 키를 돌려준다.

    서버 키는 RLS를 건너뛰므로 이 확인이 다른 사건 파일을 막는 방어다.
    '..'·'%'(인코딩 우회)·역슬래시·빈 조각·제어 문자는 거절한다. 비ASCII(한글 등)도 거절한다 —
    Supabase Storage가 그런 객체 이름을 400 InvalidKey로 거절해 503으로 보이지 않게.
    """
    return _case_object_key(case_id, storage_path, "estimates", "견적서")


# 진단서·진료비 영수증 증빙(C). 업로드는 A의 kind "medical_document"(PDF·JPEG)
MEDICAL_DOC_TYPES = ("medical_certificate", "medical_bill")
MEDICAL_EXTENSIONS = (".pdf", ".jpg", ".jpeg")


def medical_object_key(case_id: str, storage_path: str) -> str:
    """진단서·영수증 증빙 경로: `<STORAGE_BUCKET>/<case_id>/medical/<파일>.pdf|jpg|jpeg`만."""
    key = _case_object_key(case_id, storage_path, "medical", "진단서·진료비 영수증")
    if not storage_path.lower().endswith(MEDICAL_EXTENSIONS):
        raise StoragePathInvalid("진단서·진료비 영수증은 PDF·JPEG 파일이어야 합니다")
    return key


def _case_object_key(case_id: str, storage_path: str, folder: str, label: str) -> str:
    prefix = f"{storage.STORAGE_BUCKET}/{case_id}/{folder}/"
    name = storage_path.removeprefix(prefix)
    if (
        not storage_path.startswith(prefix)
        or ".." in storage_path
        or any(ch in storage_path for ch in "%\\")
        or not storage_path.isascii()
        or not storage_path.isprintable()
        or any(part in ("", ".") for part in name.split("/"))
    ):
        raise StoragePathInvalid(f"{label} 경로는 {prefix} 아래 파일이어야 합니다")
    return storage_path.removeprefix(f"{storage.STORAGE_BUCKET}/")


def _require_pdf_header(path: Path) -> None:
    with path.open("rb") as f:
        head = f.read(PDF_HEADER_WINDOW)
    if PDF_HEADER not in head:
        raise EstimateNotPdf("견적서 파일이 PDF가 아닙니다")


def _pdf_parse_errors() -> tuple[type[Exception], ...]:
    """pdfplumber·pdfminer의 PDF 파싱 오류만. 그 밖의 예외(코드 버그)는 잡지 않는다."""
    try:
        from pdfminer.psparser import PSException
        from pdfplumber.utils.exceptions import MalformedPDFException, PdfminerException
    except ImportError:  # pdfplumber 없음 → EstimateReaderUnavailable이 그대로 올라간다
        return ()
    return (PSException, PdfminerException, MalformedPDFException)


@_api_boundary
async def upload_estimate_from_storage(
    actor_id: str, case_id: str, storage_path: str, party_id: str
) -> ReviewReport | None:
    """견적서 경로: 권한 확인 → 경로 확인 → Storage에서 임시 파일로 내려받기 → upload_estimate.

    순서를 바꾸지 않는다(서버 키가 RLS를 건너뛰어 앞의 두 확인이 유일한 방어).
    견적서·증빙에는 임시 파일이 아니라 Storage 경로를 저장한다. 임시 파일은 결과와 상관없이 지운다.
    """
    await services.repo.get(actor_id, case_id)
    object_key = estimate_object_key(case_id, storage_path)
    fd, name = tempfile.mkstemp(prefix="estimate-", suffix=".pdf")
    os.close(fd)
    tmp = Path(name)
    try:
        await storage.download_object(object_key, tmp, max_bytes=ESTIMATE_MAX_BYTES)
        _require_pdf_header(tmp)
        try:
            return await upload_estimate(
                actor_id, case_id, str(tmp), party_id, storage_path=storage_path
            )
        except _pdf_parse_errors() as exc:
            raise EstimateNotPdf("견적서 PDF를 읽을 수 없습니다") from exc
    finally:
        tmp.unlink(missing_ok=True)


@_api_boundary
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
    rec = await services.repo.get(actor_id, case_id)
    base = rec.latest_ready()
    if base is None or not base.get("calculation"):
        raise AgreedFaultInvalid("산정 결과가 있는 보고서가 필요합니다")
    if not fault_change_reason:
        raise AgreedFaultInvalid("협의 과실을 바꾸거나 되돌리면 이유를 함께 입력해야 합니다")
    if clear:
        if agreed_counterparty_fault is not None:
            raise AgreedFaultInvalid(
                "협의 과실 되돌리기와 새 협의 과실 입력은 함께 할 수 없습니다"
            )
        if rec.fault_agreement.get("agreed_counterparty_fault") is None:
            raise AgreedFaultInvalid("되돌릴 협의 과실이 없습니다")
    elif agreed_counterparty_fault is None:
        raise AgreedFaultInvalid("협의 상대 과실(%)을 입력하세요 (되돌리려면 clear=True)")
    elif not 0 <= agreed_counterparty_fault <= 100:
        raise AgreedFaultInvalid("상대 과실은 0~100% 사이여야 합니다")
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
    await services.repo.save(rec, actor_id)
    return report


@_api_boundary
async def confirm_report(
    actor_id: str, case_id: str, version: int, *, counterparty: CounterpartyInfo | None = None
) -> ConfirmResult:
    """담당자 [확인 완료] 한 번. 막는 경우는 최신 버전 아님(StaleVersion)·draft_ready
    아님(ConfirmBlocked) 두 가지뿐.
    확인 기록을 남기고, 청구서 조건이 맞으면 구상 청구 패키지를 만든다. 아니면 package/html=None과
    사유를 돌려준다.
    결과: {report, package, html, claim_blocked_reasons, claim_blocked_reason}.
    claim_blocked_reasons는 해당 사유 전부, claim_blocked_reason은 그 첫 번째 값(없으면 None).
    같은 입력으로 다시 확인하면 같은 패키지를 돌려준다."""
    rec = await services.repo.get(actor_id, case_id)
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
    except ClaimPackageBlocked as exc:  # ValueError 하위 클래스라 먼저 잡는다
        await services.repo.save(rec, actor_id)  # 확인 기록은 남긴다
        return {
            "report": report,
            "package": None,
            "html": None,
            "claim_blocked_reasons": list(exc.reasons),
            "claim_blocked_reason": exc.reasons[0],
        }
    except ValueError as exc:  # 협의 과실 변경 이유 없음 등 패키지 입력 오류
        raise AgreedFaultInvalid(str(exc)) from exc
    same = next((p for p in rec.packages if p["idempotency_key"] == pkg["idempotency_key"]), None)
    if same is None:
        rec.packages.append(pkg)
    await services.repo.save(rec, actor_id)
    out = same or pkg
    return {
        "report": report,
        "package": out,
        "html": package_html(out),
        "claim_blocked_reasons": [],
        "claim_blocked_reason": None,
    }


@_api_boundary
async def get_claim(actor_id: str, case_id: str, version: int) -> ClaimResult:
    """그 보고서 버전으로 만든 구상 청구서(confirm_report가 만든 것). 버전·청구서가 없으면 404."""
    rec = await services.repo.get(actor_id, case_id)
    rec.report(version)  # 버전이 없으면 RecordNotFound
    pkg = next((p for p in reversed(rec.packages) if p["report_version"] == version), None)
    if pkg is None:
        raise RecordNotFound(f"보고서 v{version}으로 만든 청구서가 없습니다")
    return {"package": pkg, "html": package_html(pkg)}
