"""B(통합): 가상 접수의 단계 일정. 관제 화면을 채우는 시연용이고 실제 파이프라인은 타지 않는다.

가상 접수는 배정할 때 일정(facts_at·judgment_at·report_at·closes_at)을 한 번 정해 둔다.
그 뒤 단계는 현재 시각과 일정을 비교하기만 하면 나온다 — 단계가 넘어갈 때마다 무엇을 실행할
필요가 없고, 분석·판단·보고서 표에는 아무것도 쓰지 않는다. closes_at이 지나면 종결로 본다.

종결된 자리에 들어올 다음 접수도 미리 만들어 둔다(다음 건의 received_at = 앞 건의 closes_at).
그래서 표에는 접수 시각이 아직 오지 않은 가상 접수가 있고, 조회는 늘 received_at <= 지금인
것만 본다(VISIBLE_SQL). 실행 중에 돌아가는 것이 없다.
"""

import random
from datetime import datetime, timedelta

# 진행 중 단계의 순서(관제 화면과 같은 이름)
STAGES: tuple[str, ...] = ("intake", "facts", "judgment", "report")
# 처리 기간: 평균 3일. 감마 분포(모양 4)라 대부분 1.5~5일에 모이고 가끔 길다
MEAN_DAYS = 3.0
_SHAPE = 4.0
MIN_DAYS, MAX_DAYS = 0.5, 9.0
# 전체 처리 기간 가운데 각 단계가 시작되는 지점(접수 → 사실 카드 → 판단 → 보고서 → 종결)
_STARTS = {"facts": 0.12, "judgment": 0.40, "report": 0.62}
_JITTER = 0.06


def plan(received_at: datetime, rng: random.Random) -> dict[str, datetime]:
    """접수 시각에서 단계별 일정을 정한다. 순서는 늘 접수 ≤ 사실 카드 ≤ 판단 ≤ 보고서 ≤ 종결이다."""
    days = min(max(rng.gammavariate(_SHAPE, MEAN_DAYS / _SHAPE), MIN_DAYS), MAX_DAYS)
    total = timedelta(days=days)
    points = sorted(
        min(max(start + rng.uniform(-_JITTER, _JITTER), 0.02), 0.95) for start in _STARTS.values()
    )
    return {
        "facts_at": received_at + total * points[0],
        "judgment_at": received_at + total * points[1],
        "report_at": received_at + total * points[2],
        "closes_at": received_at + total,
    }


def stage_at(row: dict, now: datetime) -> str:
    """가상 접수의 지금 단계. 종결 시각이 지났으면 done이다(종결 처리 전이어도)."""
    if now >= row["closes_at"]:
        return "done"
    if now >= row["report_at"]:
        return "report"
    if now >= row["judgment_at"]:
        return "judgment"
    if now >= row["facts_at"]:
        return "facts"
    return "intake"


def is_open(row: dict, now: datetime) -> bool:
    """배정돼 진행 중인지. 가상 접수는 종결 시각 전까지, 실제 접수는 종결 처리 전까지다."""
    if row["status"] != "assigned":
        return False
    return not row["simulated"] or now < row["closes_at"]


# 아래 SQL 조각은 %(now)s를 넘기고 접수 건 별칭을 i로 쓴다.
# 접수 시각이 온 건만 보인다(미리 만들어 둔 가상 접수 제외)
VISIBLE_SQL = "i.received_at <= %(now)s"
# 진행 중: 배정됐고, 가상 접수면 종결 시각 전
OPEN_SQL = "(i.status = 'assigned' and (not i.simulated or i.closes_at > %(now)s))"
# 종결: 실제 접수는 종결 처리됨, 가상 접수는 종결 시각이 지남
CLOSED_SQL = (
    "(i.status = 'closed'"
    " or (i.simulated and i.status = 'assigned' and i.closes_at <= %(now)s))"
)
# 담당자를 기다리는 중: 미배정이고, 가상 접수면 종결 시각 전(지나면 대기열에서 빠진다)
WAITING_SQL = (
    "(i.status in ('received', 'unassigned')"
    " and (not i.simulated or i.closes_at > %(now)s))"
)
# stage_at과 같은 판정
STAGE_SQL = """case
  when %(now)s >= i.closes_at then 'done'
  when %(now)s >= i.report_at then 'report'
  when %(now)s >= i.judgment_at then 'judgment'
  when %(now)s >= i.facts_at then 'facts'
  else 'intake' end"""
