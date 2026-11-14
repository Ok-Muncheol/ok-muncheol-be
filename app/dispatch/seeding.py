"""B(통합): 가상 접수를 미리 만들어 관제 화면을 채운다(시연용). 실행은 scripts/seed_dispatch.py.

진행 중인 사건 수를 일정하게 두는 방식이다. 사건 자리(slot)를 정해진 수만큼 두고, 자리마다
접수 → 종결 → 그 종결 시각에 다음 접수 … 를 정해진 기간(start~horizon)만큼 이어서 만든다.
평균 처리 기간이 3일이므로 자리 450개면 하루에 150건쯤 접수되고 150건쯤 종결된다.

접수는 모두 실제 배정 경로(service.register)를 거친다. 배정 시각과 후보의 업무량은 그 접수가
들어오는 시각 기준이라, 앞으로 올 접수도 그때의 업무량으로 미리 배정해 둔다. 접수 시각이 아직
오지 않은 건은 조회에 보이지 않다가 그 시각이 되면 나타난다(app/dispatch/simulation.py).
"""

import asyncio
import heapq
import random
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.dispatch import service

# 접수가 들어오는 지역의 비중(대략 인구 비례, 합 100). 담당자 배치와 같은 비중이다
REGION_WEIGHTS = {
    "서울": 15, "경기": 20, "인천": 6, "부산": 7, "대구": 4, "광주": 3, "대전": 3, "울산": 3,
    "세종": 2, "강원": 4, "충북": 4, "충남": 5, "전북": 4, "전남": 4, "경북": 6, "경남": 7,
    "제주": 3,
}
# 사고 장소 글. 일부는 시·도 이름 없이 시 이름만 쓴다(지역 판별이 할 일이 있게)
PLACES = {
    "서울": ["서울 강남구 테헤란로", "서울특별시 마포구 월드컵북로", "서울 송파구 올림픽로",
           "서울 영등포구 여의대로", "서울 노원구 동일로"],
    "경기": ["경기도 수원시 팔달구 인계로", "성남시 분당구 판교역로", "경기 고양시 일산동구 중앙로",
           "용인시 기흥구 중부대로", "경기도 화성시 동탄대로"],
    "인천": ["인천 남동구 인하로", "인천광역시 연수구 컨벤시아대로", "인천 부평구 부평대로"],
    "부산": ["부산 해운대구 해운대로", "부산광역시 부산진구 중앙대로", "부산 사상구 낙동대로"],
    "대구": ["대구 수성구 달구벌대로", "대구광역시 달서구 월배로"],
    "광주": ["광주 서구 상무대로", "광주광역시 북구 무등로"],
    "대전": ["대전 유성구 대학로", "대전광역시 서구 둔산로"],
    "울산": ["울산 남구 삼산로", "울산광역시 중구 번영로"],
    "세종": ["세종특별자치시 한누리대로", "세종 조치원읍 새내로"],
    "강원": ["강원특별자치도 춘천시 중앙로", "원주시 서원대로", "강원 강릉시 경강로"],
    "충북": ["충북 청주시 상당구 상당로", "충청북도 충주시 중원대로"],
    "충남": ["충남 천안시 서북구 불당대로", "아산시 배방읍 온천대로"],
    "전북": ["전북특별자치도 전주시 완산구 팔달로", "익산시 인북로"],
    "전남": ["전남 순천시 중앙로", "여수시 좌수영로", "전라남도 목포시 영산로"],
    "경북": ["경북 포항시 북구 중앙로", "구미시 송정대로", "경상북도 경주시 태종로"],
    "경남": ["경남 창원시 성산구 중앙대로", "김해시 가락로", "경상남도 진주시 진주대로"],
    "제주": ["제주특별자치도 제주시 연북로", "제주 서귀포시 중문관광로"],
}
SPOTS = ["사거리", "교차로", "삼거리", "주유소 앞", "지하차도 입구", "버스정류장 부근",
         "아파트 정문 앞"]
ACCIDENTS = [
    "신호 교차로에서 직진하던 차와 맞은편 좌회전 차가 부딪쳤습니다.",
    "옆 차로에서 차로를 바꾸던 차가 직진 차의 옆면을 긁었습니다.",
    "신호등 없는 교차로에서 오른쪽 길에서 나온 차와 부딪쳤습니다.",
    "주차장에서 나오던 차가 도로를 지나던 차와 부딪쳤습니다.",
    "좌회전 차로에서 직진하던 차와 옆 차로 차가 접촉했습니다.",
    "교차로에서 우회전하던 차가 직진 차와 부딪쳤습니다.",
    "유턴하던 차가 뒤따라 직진하던 차와 부딪쳤습니다.",
]
INJURIES = [
    "상대 운전자가 목 통증으로 병원에 갔습니다.",
    "상대 차 동승자가 허리를 다쳤다고 합니다.",
    "상대 운전자와 동승자가 통증을 호소합니다.",
]
INJURY_RATE = 0.3


def synthetic_intake(rng: random.Random, received_at: datetime) -> service.NewIntake:
    """가상의 접수 한 건. 사람·차량을 특정할 내용은 없다."""
    region = rng.choices(list(REGION_WEIGHTS), weights=list(REGION_WEIGHTS.values()))[0]
    location = f"{rng.choice(PLACES[region])} {rng.choice(SPOTS)}"
    description = rng.choice(ACCIDENTS)
    injured = 0
    if rng.random() < INJURY_RATE:
        injured = 2 if rng.random() < 0.25 else 1
        description += " " + rng.choice(INJURIES)
    accident_at = received_at - timedelta(minutes=rng.randint(10, 600))
    return service.NewIntake(location=location, description=description, injured_count=injured,
                             accident_at=accident_at, received_at=received_at, simulated=True)


@dataclass
class FillReport:
    intakes: int = 0
    statuses: Counter = field(default_factory=Counter)
    methods: Counter = field(default_factory=Counter)


async def fill(
    connect: Callable, *, start: datetime, horizon: datetime, slots: int,
    decide: service.Decider = service.decide_by_rule, seed: int = 20261113,
    concurrency: int = 1, ramp: timedelta = timedelta(days=3),
    progress: Callable[[FillReport], None] | None = None,
) -> FillReport:
    """start부터 horizon까지 사건 자리 slots개를 가상 접수로 채운다.

    자리마다 첫 접수는 start에서 ramp 사이에 고르게 들어오고, 그 뒤로는 앞 건이 종결되는 시각에
    다음 건이 들어온다. 접수는 시각 순으로 concurrency건씩 묶어 배정한다(묶음 안에서는 서로의
    배정을 못 볼 수 있다 — 실제로 동시에 들어온 접수와 같다).
    """
    rng = random.Random(seed)
    queue: list[tuple[datetime, int]] = [
        (start + ramp * rng.random(), slot) for slot in range(slots)
    ]
    heapq.heapify(queue)
    report, lock = FillReport(), asyncio.Lock()

    async def one(received_at: datetime, slot: int, intake_seed: int) -> None:
        local = random.Random(intake_seed)
        new = synthetic_intake(local, received_at)
        async with connect() as conn:
            row = await service.register(conn, new, now=received_at, decide=decide, rng=local)
            cur = await conn.execute(
                "select method from public.case_assignments where intake_id = %s"
                " and coverage = 'property'", (row["id"],))
            method = await cur.fetchone()
        async with lock:
            report.intakes += 1
            report.statuses[row["status"]] += 1
            report.methods[method["method"] if method else "none"] += 1
            if row["closes_at"] <= horizon:
                heapq.heappush(queue, (row["closes_at"], slot))

    while queue:
        batch = [heapq.heappop(queue) for _ in range(min(concurrency, len(queue)))]
        await asyncio.gather(*(one(at, slot, rng.getrandbits(48)) for at, slot in batch))
        if progress is not None:
            progress(report)
    return report
