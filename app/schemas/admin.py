"""B(통합): 관리자(관제) 계약 — `/admin` 경로의 응답 모양.

담당자와 역할의 원천은 Supabase Auth의 app_metadata다. 서버 키로만 바꿀 수 있고 토큰에 실린다.
    staff_role   adjuster(담당자) · admin(관리자)
    coverage     property(대물) · injury(대인). 관리자는 없다
    region       담당 지역(Region). 아직 적지 않은 계정은 null
    auto_assign  false면 자동 배정에서 뺀다(시연·테스트 계정). 없으면 true
    career_years 경력 연차. specialties 전문 분야(Specialty 목록).
    high_value   고액 사건 처리 권한. away 휴가·부재(true면 자동 배정에서 뺀다). 없으면 0·[]·false

접수 건과 배정 이력의 표는 supabase/migrations/20261113200000_case_intakes_assignments.sql이다.
Region·IntakeStatus·AssignmentMethod·Coverage는 그 파일의 제약과 같아야 한다
(tests/migrations/test_migration_files.py가 대조한다).
접수 건의 배정 참고 정보(IntakeDetails)는 case_intakes.details(jsonb)에 그대로 들어간다
(20261115100000_case_intake_details.sql). 사람을 특정하는 정보는 넣지 않는다.
"""

from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

StaffRole = Literal["adjuster", "admin"]
Coverage = Literal["property", "injury"]
# 17개 시·도의 짧은 이름. 접수의 사고 장소를 이 값 하나로 판별해 같은 지역 담당자에게 배정한다
Region = Literal[
    "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기",
    "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주",
]
IntakeStatus = Literal["received", "assigned", "unassigned", "closed"]
AssignmentMethod = Literal["agent", "rule", "manual", "transfer"]
# 관제 화면의 진행 단계. unassigned는 담당자를 기다리는 접수,
# done은 종결(실제 접수는 보고서 확인 완료)
IntakeStage = Literal["unassigned", "intake", "analysis", "facts", "judgment", "report", "done"]


# 담당자의 전문 분야: 수입차 · 중상해 · 다중 추돌
Specialty = Literal["imported", "serious_injury", "multi_vehicle"]
VehicleType = Literal["passenger", "imported", "truck", "motorcycle"]  # 승용·수입차·화물차·이륜차
InjurySeverity = Literal["minor", "serious", "fatal"]  # 경상·중상·사망
# 자기신체사고(self_injury)·자동차상해(auto_injury)는 둘 중 하나만 가입한다
PersonalInjuryCover = Literal["none", "self_injury", "auto_injury"]


class Policy(BaseModel):
    """우리 계약자의 가입 담보(대인배상Ⅰ·대물배상은 의무 가입이라 따로 적지 않는다)."""

    model_config = ConfigDict(extra="forbid")

    bodily_injury_ii: bool = True  # 대인배상Ⅱ(무한)
    # 대물 한도(원)
    property_limit: int = Field(default=200_000_000, ge=20_000_000, le=2_000_000_000)
    own_damage: bool = True  # 자기차량손해
    personal_injury: PersonalInjuryCover = "self_injury"
    uninsured_motorist: bool = True  # 무보험차상해


class Policyholder(BaseModel):
    """계약자 이력. 사람을 특정하는 정보는 없다."""

    model_config = ConfigDict(extra="forbid")

    accidents_3y: int = Field(default=0, ge=0, le=20)  # 최근 3년 사고 횟수
    complaints: int = Field(default=0, ge=0, le=20)  # 민원 횟수
    long_term: bool = False  # 장기 고객(5년 이상)


class IntakeDetails(BaseModel):
    """접수 건의 배정 참고 정보. 접수 등록 요청과 응답에 같은 모양으로 쓴다."""

    model_config = ConfigDict(extra="forbid")

    estimated_damage: int | None = Field(default=None, ge=0, le=10_000_000_000)  # 예상 손해액(원)
    vehicle_type: VehicleType | None = None  # 우리 계약 차량의 차종
    vehicle_count: int | None = Field(default=None, ge=1, le=20)  # 사고에 관련된 차량 수
    injury_severity: InjurySeverity | None = None  # 가장 크게 다친 사람의 정도
    policy: Policy | None = None
    policyholder: Policyholder | None = None


class StaffCaseCounts(TypedDict):
    """그 사람이 대물 또는 대인 담당자로 맡은 접수 건 수."""

    open: int  # 진행 중(종결 전)
    closed: int  # 종결한 누계


class StaffMember(TypedDict):
    user_id: str  # auth.users.id — cases.assignee_id·injury_assignee_id에 들어가는 값
    email: str | None
    name: str | None  # user_metadata.name. 없으면 화면이 이메일을 보인다
    staff_role: StaffRole
    coverage: Coverage | None
    region: Region | None
    auto_assign: bool
    career_years: int
    specialties: list[Specialty]
    high_value: bool  # 고액 사건(예상 손해액이 큰 대물, 중상·사망 대인) 처리 권한
    away: bool  # 휴가·부재. true면 자동 배정 후보가 아니다
    cases: StaffCaseCounts


class AssignmentNote(TypedDict):
    """접수 건에 붙이는, 대물 담당자를 정한 가장 최근 배정."""

    method: AssignmentMethod
    reason: str
    created_at: str


class IntakeView(TypedDict):
    intake_id: str
    received_at: str
    accident_at: str | None
    location: str
    description: str
    injured_count: int
    details: IntakeDetails  # 없던 접수 건은 모든 값이 null이다
    region: Region | None
    status: IntakeStatus
    stage: IntakeStage
    property_assignee_id: str | None
    injury_assignee_id: str | None
    unassigned_reason: str | None
    case_id: str | None  # 실제 접수가 배정되면 생기는 사건. 가상 접수는 null
    simulated: bool
    closed_at: str | None
    assignment: AssignmentNote | None


class OverviewSummary(TypedDict):
    open: int  # 진행 중인 접수 건
    unassigned: int  # 담당자를 기다리는 접수 건
    received_today: int  # 오늘(한국 시간) 접수
    closed_today: int  # 오늘(한국 시간) 종결
    by_stage: dict[str, int]  # 진행 중인 건의 단계별 수(IntakeStage 가운데 unassigned·done 제외)


class AdminOverview(TypedDict):
    """담당자 목록(업무량 포함)과 전체 현황 숫자. 접수 건 목록은 `GET /admin/intakes`가 준다."""

    staff: list[StaffMember]
    summary: OverviewSummary
    generated_at: str


class AssignmentRecord(TypedDict):
    """배정 이력 한 줄 — 한 분야의 담당자를 정하거나 바꾼 일 한 번."""

    coverage: Coverage
    from_assignee_id: str | None  # 이관일 때 이전 담당자
    to_assignee_id: str
    method: AssignmentMethod
    reason: str
    decided_by: str | None  # 직접 배정·이관을 한 관리자. 자동 배정이면 null
    created_at: str


class IntakeDetail(IntakeView):
    """접수 건 하나와 배정 이력(오래된 것부터)."""

    assignments: list[AssignmentRecord]
