"""A: 사실 카드 칸 정의(초안 v0). VLM 질문·정답 입력표·채점이 이 목록 하나를 같이 쓴다.

원본 설명: docs/contracts/fact-card-v0.md 3절. 칸을 바꾸면 문서와 함께 고친다.
required: "always"(●), "cond"(◐ 조건부), "optional"(○, 채점 안 함)
"""

PLACES = [
    "직선도로",
    "사거리_신호있음",
    "사거리_신호없음",
    "T자형교차로",
    "회전교차로",
    "고속도로",
    "주차장",
    "차도와비차도",
    "불명",
]
SIGNALS = ["녹색", "황색", "적색", "녹색좌회전", "녹색직진좌회전", "신호없음", "불명"]
MOVES = [
    "직진",
    "좌회전",
    "우회전",
    "유턴",
    "진로변경",
    "합류",
    "추월",
    "회전교차로진입",
    "회전교차로회전",
    "정차후출발",
    "주정차",
    "후진",
    "출차",
    "불명",
]
YES_NO = ["있음", "없음", "불명"]
TRENDS = ["가속", "일정", "감속", "정지", "불명"]
PARTS = ["앞", "뒤", "왼쪽", "오른쪽", "앞왼쪽", "앞오른쪽", "뒤왼쪽", "뒤오른쪽", "불명"]

# (칸 이름, 값 목록 또는 "int"/"text", 필수 구분, 설명)
FIELDS = [
    ("accident_object", ["차대차", "차대이륜차", "기타", "불명"], "always", "사고 대상"),
    ("party_count", "int", "always", "사고에 얽힌 차량 수 (3 이상이면 범위 밖)"),
    ("location_type", PLACES, "always", "사고 장소"),
    (
        "road_priority",
        [
            "동일폭",
            "A대로",
            "B대로",
            "A쪽_일시정지",
            "B쪽_일시정지",
            "A쪽만_신호등",
            "B쪽만_신호등",
            "해당없음",
            "불명",
        ],
        "cond",
        "교차로에서 도로 관계. 교차로가 아니면 해당없음",
    ),
    ("signal_A", SIGNALS, "cond", "A 진행 방향 신호. 안 보이면 불명"),
    ("signal_B", SIGNALS, "cond", "B 진행 방향 신호. 신호등 없는 곳이면 신호없음"),
    ("movement_A", MOVES, "always", "A의 충돌 직전 움직임"),
    ("movement_B", MOVES, "always", "B의 충돌 직전 움직임"),
    (
        "relative_position",
        ["같은방향_앞", "같은방향_뒤", "맞은편", "왼쪽", "오른쪽", "불명"],
        "always",
        "B 기준 A가 있던 쪽(충돌 직전)",
    ),
    (
        "entry_order",
        ["A먼저", "B먼저", "동시", "해당없음", "불명"],
        "cond",
        "교차로·합류 지점에 누가 먼저 들어왔나. 해당 없으면 해당없음",
    ),
    ("lane_change_A", YES_NO, "always", "A가 충돌 직전 차로를 바꿨나"),
    ("lane_change_B", YES_NO, "always", "B가 충돌 직전 차로를 바꿨나"),
    ("centerline_cross_A", YES_NO, "always", "A가 중앙선을 넘었나"),
    ("centerline_cross_B", YES_NO, "always", "B가 중앙선을 넘었나"),
    ("motion_trend_A", TRENDS, "optional", "A 속도 변화 추세(수치 아님)"),
    ("motion_trend_B", TRENDS, "optional", "B 속도 변화 추세(수치 아님)"),
    ("collision_part_A", PARTS, "always", "A 차의 부딪힌 부위"),
    ("collision_part_B", PARTS, "always", "B 차의 부딪힌 부위"),
    (
        "collision_time_ms",
        "int",
        "always",
        "충돌 순간(영상 시작부터 ms). 입력표에서는 프레임 번호로 받음",
    ),
    ("time_condition", ["주간", "야간", "새벽황혼", "불명"], "optional", "시간대"),
    ("weather", ["맑음", "흐림", "비", "눈", "불명"], "optional", "날씨"),
    ("narrative", "text", "optional", "자유 서술 (채점 안 함)"),
]
FIELD_NAMES = [f[0] for f in FIELDS]
SCORED = [f[0] for f in FIELDS if f[2] != "optional"]
#  핵심 칸: 교차로 사고 유형(과실 도표)을 가르는 칸. 유형표 211개 후보에서 이 8칸이면 12칸 전부와
# 같은 만큼 갈린다
# (2026-11-06 계산). 칸을 지우는 게 아니라 채점·개선의 우선 기준이다.
CORE = [
    "location_type",
    "road_priority",
    "signal_A",
    "signal_B",
    "movement_A",
    "movement_B",
    "relative_position",
    "entry_order",
]
# 핵심 칸을 맡는 쪽으로 나눔(2026-11-09 합의):
# - 비전이 책임지는 칸: 영상에서 판단
#  - 담당자 칸: 장소는 담당자가 확인, 도로 관계·진입 순서는 신호 없는 교차로 등 필요할 때 담당자가
# 입력
#   (영상으로 판단이 어렵고 사람도 '불명'이 많음. 해당없음은 장소 정리 규칙이 자동으로 채움)
VISION_CORE = ["signal_A", "signal_B", "movement_A", "movement_B", "relative_position"]
STAFF_FIELDS = ["location_type", "road_priority", "entry_order"]
FPS = 15


REVIEW_THRESHOLD = 0.7  # 임시값. 개선용 10건 평가 후 다시 정한다 (fact-card-v0.md 6절)
SIGNAL_PLACES = {"사거리_신호있음"}
INTERSECTIONS = {"사거리_신호있음", "사거리_신호없음", "T자형교차로", "회전교차로"}
TIME_TOLERANCE_MS = 200  # collision_time_ms가 이만큼 이내면 같은 값으로 본다


def frame_to_ms(frame: int) -> int:
    return round(frame / FPS * 1000)


def is_required(name: str, values: dict) -> bool:
    """칸이 이 사건에서 필수인지. values는 같은 카드의 다른 칸 값(장소 등)."""
    spec = {f[0]: f[2] for f in FIELDS}[name]
    place = values.get("location_type")
    if spec == "always":
        return True
    if spec == "optional":
        return False
    if name in ("road_priority", "entry_order"):
        return place in INTERSECTIONS
    if name in ("signal_A", "signal_B"):
        return place in SIGNAL_PLACES
    return False


#  신호가 들어간 사고 유형은 사거리_신호있음에만 있다(유형표 46개 중 35개, 다른 장소는 0개,
# 2026-11-09 확인).
#  그래서 그 밖의 장소에서는 신호 칸을 '신호없음'으로 정리하고(VLM이 지어낸 색 제거), 채점도 하지
# 않는다.
NO_ENTRY_PLACES = {"직선도로", "주차장", "고속도로"}  # 진입 순서가 없는 곳(고속도로 합류는 예외)
PLACE_FIELDS = ("road_priority", "entry_order", "signal_A", "signal_B")  # 장소에 딸린 칸


def tidy_by_location(values: dict) -> dict:
    """장소에 맞지 않는 칸 값을 정리한다. 바꿀 칸만 {칸: (전 값, 새 값)}으로 돌려준다(values는 안
    바꿈).

    카드를 만들 때(VLM이 답한 장소 기준)와 담당자가 장소를 고쳤을 때(고친 장소 기준) 같은 함수를
    쓴다.
    규칙은 정답 카드의 기준을 따른다. 맞는 값을 모르면 '불명'으로 바꿔 확인 표시가 붙게 한다.
    """
    place = values.get("location_type")
    if place in (None, "불명"):
        return {}
    want = {}
    rp, eo = values.get("road_priority"), values.get("entry_order")
    if place not in INTERSECTIONS or place == "회전교차로":
        want["road_priority"] = "해당없음"
    elif place in SIGNAL_PLACES:
        if rp not in ("해당없음", "A쪽만_신호등", "B쪽만_신호등"):
            want["road_priority"] = "해당없음"
    elif rp == "해당없음":
        want["road_priority"] = "불명"
    merging = "합류" in (values.get("movement_A"), values.get("movement_B"))
    if place in NO_ENTRY_PLACES and not merging:
        want["entry_order"] = "해당없음"
    elif place in INTERSECTIONS and eo == "해당없음":
        want["entry_order"] = "불명"
    if place not in SIGNAL_PLACES:
        want["signal_A"] = want["signal_B"] = "신호없음"
    return {k: (values.get(k), v) for k, v in want.items() if values.get(k) != v}


# 충돌 부위 A를 B의 부위와 상대 위치로 정한다(물리적으로 정해지는 경우만).
# 추돌: 두 차가 같은 방향을 보므로 왼쪽·오른쪽이 거울처럼 맞물린다(B 앞오른쪽 ↔ A 뒤왼쪽).
# 정면: 마주 보므로 같은 쪽끼리 맞물린다(B 앞오른쪽 ↔ A 앞오른쪽).
_FRONT_TO_REAR = {"앞": "뒤", "앞왼쪽": "뒤오른쪽", "앞오른쪽": "뒤왼쪽"}
_REAR_TO_FRONT = {"뒤": "앞", "뒤왼쪽": "앞오른쪽", "뒤오른쪽": "앞왼쪽"}
ROUNDABOUT_MOVES = {"회전교차로진입": "회전교차로회전", "회전교차로회전": "회전교차로진입"}


def derive_fields(values: dict) -> dict:
    """다른 칸에서 논리적으로 정해지는 값. 바꿀 칸만 {칸: (전 값, 새 값)} (values는 안 바꿈).

    - 충돌 부위 A: 같은방향_앞(B가 A를 추돌)·같은방향_뒤(A가 B를 추돌)·맞은편(정면)일 때만.
      옆에서 부딪힌 경우(왼쪽·오른쪽)는 누가 누구를 받았는지에 따라 달라 정하지 않는다.
    - 회전교차로: 차의 움직임은 진입 아니면 회전뿐이다. 한쪽이 진입·회전이고 다른 쪽이 그 밖의
      값이면 반대 값으로.
    """
    want = {}
    rel, part_b = values.get("relative_position"), values.get("collision_part_B")
    part_a = None
    if rel == "같은방향_앞" and part_b in _FRONT_TO_REAR:
        part_a = _FRONT_TO_REAR[part_b]
    elif rel == "같은방향_뒤" and part_b in _REAR_TO_FRONT:
        part_a = _REAR_TO_FRONT[part_b]
    elif rel == "맞은편" and part_b in ("앞", "앞왼쪽", "앞오른쪽"):
        part_a = part_b
    if part_a:
        want["collision_part_A"] = part_a
    if values.get("location_type") == "회전교차로":
        ma, mb = values.get("movement_A"), values.get("movement_B")
        if mb in ROUNDABOUT_MOVES and ma not in ROUNDABOUT_MOVES:
            want["movement_A"] = ROUNDABOUT_MOVES[mb]
        elif ma in ROUNDABOUT_MOVES and mb not in ROUNDABOUT_MOVES:
            want["movement_B"] = ROUNDABOUT_MOVES[ma]
    return {k: (values.get(k), v) for k, v in want.items() if values.get(k) != v}


def same_value(name: str, a, b) -> bool:
    if a is None or b is None:
        return a is b
    if name == "collision_time_ms":
        return abs(int(a) - int(b)) <= TIME_TOLERANCE_MS
    return a == b
