"""B(통합): 관리자(관제) 조회 계약 — `GET /admin/overview`.

담당자와 역할의 원천은 Supabase Auth의 app_metadata다. 서버 키로만 바꿀 수 있고 토큰에 실린다.
    staff_role  adjuster(담당자) · admin(관리자)
    coverage    property(대물) · injury(대인). 관리자는 없다
    region      담당 지역(예: "서울"). 아직 적지 않은 계정은 null
"""

from typing import Literal, TypedDict

StaffRole = Literal["adjuster", "admin"]
Coverage = Literal["property", "injury"]


class StaffCaseCounts(TypedDict):
    """그 사람이 맡은 사건 수."""

    property: int  # 대물 담당자로 맡은 사건
    injury: int  # 대인 담당자로 맡은 사건
    open: int  # 그 가운데 확인 완료(보고서 승인) 전인 사건. 두 역할을 겸한 사건은 한 번만 센다


class StaffMember(TypedDict):
    user_id: str  # auth.users.id — cases.assignee_id·injury_assignee_id에 들어가는 값
    email: str | None
    name: str | None  # user_metadata.name. 없으면 화면이 이메일을 보인다
    staff_role: StaffRole
    coverage: Coverage | None
    region: str | None
    cases: StaffCaseCounts


class AdminOverview(TypedDict):
    """담당자 목록과 전체 사건의 배정 현황.

    cases의 항목은 담당자 화면과 같은 CaseSummary에 `assignee_id`(대물 담당자)와
    `injury_assignee_id`(대인 담당자, 없으면 null)를 더한 것이다. 최근 바뀐 순.
    담당자 목록에 없는 ID(역할이 없는 계정)가 사건을 맡고 있을 수 있다.
    """

    staff: list[StaffMember]
    cases: list[dict[str, object]]
