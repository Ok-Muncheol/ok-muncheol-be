"""D: 보고서·산정·구상 버전 저장소.

지금은 메모리 구현이다. TODO(D 통합): Supabase 테이블(review_reports, calculations, claim_packages,
case_documents)과 RLS(담당 사건만)로 교체. 인터페이스(메서드 이름)는 그대로 둔다.
"""

from dataclasses import dataclass, field

from app.schemas.facts import FactCard
from app.schemas.reports import ClaimPackage, ReviewReport


class RecordNotFound(LookupError):
    """사건·보고서·청구서가 없음. API 경계에서 404로 바뀐다(KeyError 등 내부 오류와 구분)."""


class NotCaseOwner(PermissionError):
    """담당 사건이 아님. API 경계에서 403으로 바뀐다(파일 권한 오류와 구분)."""


@dataclass
class CaseRecord:
    case_id: str
    owner_id: str
    card: FactCard | None = None
    reports: list[ReviewReport] = field(default_factory=list)
    documents: list[dict[str, object]] = field(default_factory=list)
    case_facts: dict[str, object] = field(
        default_factory=dict
    )  # 면책 판단용 사실(무면허·음주·고의 등)
    packages: list[ClaimPackage] = field(default_factory=list)
    # 협의 과실: {"agreed_counterparty_fault": int | None, "fault_change_reason": str}. None 값 =
    # 협의 해제 기록
    fault_agreement: dict[str, object] = field(default_factory=dict)

    @property
    def latest(self) -> ReviewReport | None:
        return self.reports[-1] if self.reports else None

    def report(self, version: int) -> ReviewReport:
        for r in self.reports:
            if r["versions"]["report_version"] == version:
                return r
        raise RecordNotFound(f"보고서 v{version}이 없습니다")

    def latest_ready(self) -> ReviewReport | None:
        """최신 보고서가 산정 가능한 판단(draft_ready)이면 그 보고서. 추가 확인·보류면 None."""
        r = self.latest
        return r if r is not None and r["judgment"]["status"] == "draft_ready" else None


class InMemoryReportRepository:
    def __init__(self) -> None:
        self._cases: dict[str, CaseRecord] = {}

    def open(self, case_id: str, owner_id: str) -> CaseRecord:
        return self._cases.setdefault(case_id, CaseRecord(case_id=case_id, owner_id=owner_id))

    def get(self, actor_id: str, case_id: str) -> CaseRecord:
        rec = self._cases.get(case_id)
        if rec is None:
            raise RecordNotFound("사건이 없습니다")
        if rec.owner_id != actor_id:
            raise NotCaseOwner("담당 사건이 아닙니다")
        return rec
