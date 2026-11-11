"""A: VLM에 보낼 사진 고르기·그리기와 질문문(버전별).

- 기본 버전은 PROMPT_VERSION(v0.2). v0.1·v0.3·v0.4는 지난 실험 재현용(evals/vision/run_cards.py
  --prompt)으로 남긴다. 결과는 각 함수 주석과 evals/vision/CARDS.md에 있다.
- 칸 이름·허용값은 내부 한국어 값(workers/vision/fields.py)이다. 팀에 넘길 때는 contract.py가
  영어 v1으로 바꾼다.
"""

from __future__ import annotations

import base64
from typing import Any

from workers.vision.fields import FIELDS, FPS
from workers.vision.tracks import draw_boxes

PROMPT_VERSIONS = ("v0.1", "v0.2", "v0.3", "v0.4")
PROMPT_VERSION = "v0.2"  # 기본값: 지금까지 가장 좋은 버전(개선용 10건 65%)
# v0.1: 충돌(hint=75) 기준 간격. 충돌 뒤 장면은 움직임 판단을 망치므로 앞쪽 위주
FRAME_OFFSETS = [-45, -35, -27, -20, -14, -9, -5, -2, 0, 6]
#  v0.2: 앞쪽 2장(도로 모양 파악용) + 충돌 구간(약 3~6.5초)을 촘촘하게. 충돌 시점을 미리 알려 주지
# 않는다
FRAMES_V02 = [1, 30, 50, 58, 63, 67, 70, 73, 76, 80, 85, 95]

# ---------- 프레임 ----------


def select_frames(
    collision_hint: int = 75, total: int = 150, version: str = PROMPT_VERSION
) -> list[int]:
    if version == "v0.1":
        return sorted({min(total, max(1, collision_hint + d)) for d in FRAME_OFFSETS})
    return [f for f in FRAMES_V02 if f <= total]


def render_frames(
    video_path: str,
    frames: list[int],
    hint: int,
    size=(768, 432),
    tracks: dict | None = None,
    version: str = PROMPT_VERSION,
) -> list[str]:
    """프레임을 JPG(base64)로. 왼쪽 위에 프레임 번호와 시간을 적어 시간 순서를 분명히 한다.

    v0.1은 충돌 추정 시점 기준(+/-초), v0.2는 영상 시작 기준 초(충돌 시점 힌트를 주지 않기 위해).
    tracks가 있으면 그 프레임(±2) 근처의 A·B 박스를 그린다.
    """
    import cv2

    cap = cv2.VideoCapture(video_path)
    out = []
    for n in frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, n - 1)
        ok, img = cap.read()
        if not ok:
            raise RuntimeError(f"프레임 {n} 읽기 실패: {video_path}")
        if tracks:
            draw_boxes(img, tracks, n)
        img = cv2.resize(img, size)
        label = (
            f"#{n} ({(n - hint) / FPS:+.1f}s)" if version == "v0.1" else f"#{n} ({n / FPS:.1f}s)"
        )
        cv2.rectangle(img, (0, 0), (190, 30), (0, 0, 0), -1)
        cv2.putText(img, label, (6, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 255), 2)
        out.append(
            base64.b64encode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1]).decode()
        )
    return out


# ---------- 질문 ----------

ASKED = [f for f in FIELDS if f[0] != "collision_time_ms"]


def response_schema(version: str = PROMPT_VERSION) -> dict:
    props: dict[str, Any] = {}
    if version != "v0.1":
        # 답하기 전에 관찰을 먼저 적게 한다(순서가 앞이어야 효과가 있음). 채점하지 않는다
        props["scene_notes"] = {"type": "string"}
    for name, values, _, _ in ASKED:
        if isinstance(values, list):
            props[name] = {
                "type": "object",
                "required": ["value", "confidence"],
                "properties": {"value": {"enum": values}, "confidence": {"type": "number"}},
            }
        elif values == "int":
            props[name] = {
                "type": "object",
                "required": ["value", "confidence"],
                "properties": {
                    "value": {"type": ["integer", "null"]},
                    "confidence": {"type": "number"},
                },
            }
        else:
            props[name] = {"type": "string"}
    props["collision_frame"] = {
        "type": "object",
        "required": ["value", "confidence"],
        "properties": {"value": {"type": ["integer", "null"]}, "confidence": {"type": "number"}},
    }
    return {"type": "object", "properties": props, "required": list(props)}


def build_prompt(
    viewpoint: int,
    frames: list[int],
    hint: int,
    context: str | None = None,
    version: str = PROMPT_VERSION,
) -> str:
    if version == "v0.1":
        return _build_prompt_v01(viewpoint, frames, hint, context)
    if version == "v0.4":
        #  v0.3 결과: A 움직임 규칙이 과하게 먹혀 차로 변경·중앙선을 7/10 '있음'으로 답함(정답
        # 2·1건) → 뺌.
        # 신호 규칙(신호등 위치를 적어야 색을 고름, 가로등·브레이크등 제외)만 남긴다.
        return _build_prompt_v02(viewpoint, frames, context, notes={"2"}, rules=V03_RULES[:2])
    if version == "v0.3":
        return _build_prompt_v02(viewpoint, frames, context, notes={"2", "4"}, rules=V03_RULES)
    return _build_prompt_v02(viewpoint, frames, context)


# v0.3에서 바뀌는 문장(v0.2 오답 분석, gpt-4o 개선용 10건)
#  - 신호 B를 '적색'으로 지어냄(7개 중 6개), 신호 없는 야간 사거리에서도 '신호등 보임, 적색' →
# 가로등·브레이크등 착각
# - 그 때문에 장소를 '사거리_신호있음'으로 틀림
# - 충돌 부위 A의 좌우가 반대(앞모습이 보이면 거울처럼 반대)
# - A 움직임을 '직진'으로만 답함(진로변경·우회전·후진·추월)
V03_NOTES = {
    "2": "2) 신호등: 신호등이 보이면 몇 번 사진의 어디(예: #30 화면 위 가운데)에 있는지와 색을 "
         "적는다. "
    "보이지 않으면 '신호등 안 보임'이라고 적는다",
    "4": "4) A: 처음 보인 사진·위치, A의 앞모습이 보이는지 뒷모습이 보이는지, 차선을 넘었는지, "
    "방향을 틀었는지, 충돌 직전 위치",
}
V03_RULES = [
    "- 신호등 판단: 위에 매달리거나 기둥에 달린 신호등 몸체(3~4개 등이 나란한 상자)가 보여야 "
    "신호등이다. "
    "가로등, 앞차 브레이크등·후미등, 간판 불빛은 신호등이 아니다. scene_notes 2)에 신호등 위치를 "
    "적지 못했으면 "
    "신호 색을 고르지 말고 '불명'(신호등이 있을 법하지만 안 보임) 또는 '신호없음'(신호등이 없는 "
    "곳)을 고른다.",
    "- location_type의 '사거리_신호있음'은 신호등 몸체가 실제로 보일 때만 고른다. 교차로인데 "
    "신호등이 안 보이면 "
    "'사거리_신호없음' 또는 'T자형교차로'.",
    "- 충돌 부위 좌우: 각 차 운전석에 앉은 사람 기준이다. 차의 앞모습이 화면에 보이면 그 차의 "
    "왼쪽은 화면 오른쪽, "
    "오른쪽은 화면 왼쪽이다(거울처럼 반대). 뒷모습이 보이면 화면과 같은 쪽이다.",
    "- A의 움직임: '직진'은 A가 자기 차선을 따라 곧게 왔을 때만 고른다. 차선을 넘어 들어오면 "
    "진로변경(앞질러 가면 추월), "
    "옆 도로에서 방향을 틀며 들어오면 좌회전·우회전, 뒷모습이 B 쪽으로 다가오면 후진.",
]


def _build_prompt_v02(
    viewpoint: int,
    frames: list[int],
    context: str | None,
    notes: set[str] = frozenset(),
    rules: list[str] = (),
) -> str:
    """notes: V03_NOTES에서 바꿔 끼울 메모 항목 번호, rules: 덧붙일 판단 규칙."""
    """v0.1 오답 분석(gpt-4o, 개선용 10건)으로 고친 질문.

    - 장소를 거의 다 '직선도로'로 답함 → 앞쪽 사진으로 도로 모양을 먼저 보게 함
    - 신호를 '불명'으로 답함 → '신호등이 아예 없음'(신호없음)과 '색이 안 보임'(불명)을 구분
    - 충돌 순간을 힌트(75번) 그대로 답함 → 힌트를 빼고 사진에서 직접 찾게 함
    - 충돌 부위를 '앞'으로만 답함, 1인칭 B 움직임을 '직진'으로만 답함 → 판단 기준 추가
    """
    if viewpoint == 1:
        who = (
            "1인칭 블랙박스 영상이다. 블랙박스가 달린 촬영 차량이 B, 부딪힌 상대 차량이 A다. "
            "B는 화면에 거의 보이지 않으므로 B의 움직임은 사진 사이 풍경 변화로 판단한다."
        )
    else:
        who = (
            "사고와 관계없는 제3의 카메라 영상이다. 서로 부딪힌 두 차 중 하나를 A, 다른 하나를 B로 "
            "정하고 "
            "scene_notes 첫 줄에 A와 B가 각각 어떤 차인지(색·위치) 적어라."
        )
    lines = [
        f"교통사고 영상(15fps, 150프레임 = 10초)에서 시간 순서대로 뽑은 사진 {len(frames)}장이다.",
        "각 사진 왼쪽 위에 프레임 번호와 영상 시작부터의 시간이 적혀 있다.",
        "앞쪽 사진은 사고 전 도로 모양을 보여 주고, 나머지는 충돌 전후를 촘촘하게 보여 준다. "
        "충돌 시점은 영상마다 다르니 사진을 보고 직접 찾아라.",
        who,
        "",
        "먼저 scene_notes에 아래 순서로 짧게 관찰을 적고, 그 관찰과 맞게 각 칸을 채워라.",
        "1) 도로 모양: 앞쪽 사진에서 가로지르는 도로·횡단보도·정지선이 있는지, "
        "회전교차로·주차장·고속도로인지",
        V03_NOTES["2"]
        if "2" in notes
        else "2) 신호등: 보이는지, 보이면 어느 방향 신호이고 무슨 색인지",
        "3) B의 움직임",
        V03_NOTES["4"] if "4" in notes else "4) A가 처음 보인 위치와 충돌 직전까지의 경로",
        "5) 두 차가 처음 닿는 사진 번호와 닿는 부위",
        "",
        "칸별 판단 기준:",
        "- location_type: 충돌 지점만 보지 말고 앞쪽 사진의 도로 모양으로 정한다. 다른 도로가 "
        "가로지르거나 "
        "합쳐지면 교차로 종류를 고르고, '직선도로'는 교차로가 전혀 없을 때만 고른다.",
        "- road_priority, entry_order: 교차로(사거리·T자형·회전교차로)이면 반드시 판단한다. "
        "교차로가 아닐 때만 '해당없음'.",
        "- signal_A, signal_B: 사고 장소에 신호등이 아예 없으면 '신호없음'. 신호등은 있는데 그 차 "
        "방향의 색을 "
        "알 수 없으면 '불명'. 1인칭에서 B의 신호는 화면 앞쪽에 보이는 신호등이다.",
        "- 1인칭 B의 움직임: 풍경이 사진마다 오른쪽으로 흘러가면 좌회전, 왼쪽으로 흘러가면 우회전, "
        "차선을 비스듬히 넘어가면 진로변경, 풍경이 전혀 안 바뀌면 주정차, 풍경이 멀어지면 후진, "
        "멈춰 있다가 움직이기 시작하면 정차후출발, 곧게 앞으로 가면 직진.",
        "- A의 움직임: A의 앞모습/뒷모습과 이동 방향으로 판단한다. A의 뒷모습이 B 쪽으로 "
        "가까워지면 후진일 수 있다.",
        "- relative_position(B 기준 A가 충돌 직전 있던 쪽): 1인칭이면 A가 화면 왼쪽에 있거나 "
        "왼쪽에서 다가오면 "
        "'왼쪽', 오른쪽이면 '오른쪽', A의 앞모습이 정면에서 다가오면 '맞은편', A의 뒷모습이 앞에 "
        "있으면 "
        "'같은방향_앞', 뒤에서 오면 '같은방향_뒤'.",
        "- collision_part_A/B: 각 차 자기 기준 부위다. 정면 가운데만 '앞', 모서리 쪽이면 "
        "'앞왼쪽'·'앞오른쪽', "
        "옆면이면 '왼쪽'·'오른쪽'. 1인칭에서 상대 차가 화면 오른쪽 아래에서 닿으면 B의 '앞오른쪽'.",
        "- collision_frame: 두 차가 처음 닿거나 화면이 갑자기 흔들리는 첫 사진의 프레임 번호.",
        "- 그 상황에 없는 칸은 '해당없음', 화면으로 알 수 없으면 '불명'.",
        "- confidence: 사진에서 직접 보이면 0.8 이상, 짐작이면 0.5 이하로 적는다.",
        *rules,
        "",
        "칸 설명:",
    ]
    for name, _, _, desc in ASKED:
        lines.append(f"- {name}: {desc}")
    lines.append("- collision_frame: 두 차가 처음 닿는 프레임 번호(1~150). 모르면 null")
    if context:
        lines += ["", "추가 정보:", context]
    lines += ["", "JSON만 출력한다."]
    return "\n".join(lines)


def _build_prompt_v01(
    viewpoint: int, frames: list[int], hint: int, context: str | None = None
) -> str:
    if viewpoint == 1:
        who = (
            "블랙박스가 달린 촬영 차량이 B, 부딪힌 상대 차량이 A다. B는 화면에 거의 보이지 않는다."
        )
    else:
        who = (
            "사고와 관계없는 제3의 카메라 영상이다. 서로 부딪힌 두 차 중 하나를 A, 다른 하나를 B로 "
            "정하고 "
            "narrative 첫 문장에 A와 B가 각각 어떤 차인지(색·위치) 적어라."
        )
    lines = [
        f"교통사고 영상에서 시간 순서대로 뽑은 프레임 {len(frames)}장이다. 영상은 15fps, "
        f"150프레임(10초)이다.",
        f"각 사진 왼쪽 위에 프레임 번호와 충돌 추정 시점({hint}번) 기준 시간이 적혀 있다. 실제 "
        f"충돌은 조금 앞뒤일 수 있다.",
        who,
        "충돌 뒤에는 차가 돌거나 화면이 흔들린다. 움직임·차로·진입 순서는 충돌 '직전'까지의 "
        "장면으로 판단하라.",
        "각 칸에 정해진 값 중 하나와 확신도(0~1)를 적어라. 화면으로 알 수 없으면 '불명'을 고르고 "
        "확신도를 낮게 적어라.",
        "그 상황에 없는 칸(예: 직선 도로의 진입 순서)은 '해당없음'.",
        "상대 차 신호는 화면에 신호등이 보이지 않으면 추측하지 말고 '불명'.",
        "",
        "칸 설명:",
    ]
    for name, _, _, desc in ASKED:
        lines.append(f"- {name}: {desc}")
    lines.append("- collision_frame: 두 차가 처음 닿는 프레임 번호(1~150). 모르면 null")
    if context:
        lines += ["", "추가 정보:", context]
    lines += ["", "JSON만 출력한다."]
    return "\n".join(lines)
