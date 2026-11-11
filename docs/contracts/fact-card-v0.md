# 사고 사실 카드 칸 구성 초안 v0 (A 제안, 합의 전)

작성: A(비전) · 2026-11-05 · 상태: **팀 검토용 초안**. 합의 후 `app/schemas/facts.py`와 견본에 반영한다.
예시: [fact-card-v0.example.json](fact-card-v0.example.json) (AI Hub 개선용 영상 1건의 라벨 값으로 만든 예시, 탐지·VLM 값은 가상)

## 1. 정할 것 요약

| # | 질문 | A 제안 | 확인할 사람 |
| --- | --- | --- | --- |
| 1 | 칸 목록과 값 목록 (아래 3절) | 22개 칸 | B(도표 고르기에 충분한가), FE |
| 2 | 탐지와 VLM 결과를 어디에 남기나 | `source`는 그대로, `vision_detail` 칸 추가 (4절) | B, D, FE |
| 3 | 카드의 A/B는 누구인가 | 임시로 AI Hub 관례(1인칭은 촬영 차량=B). 담당자 확정 때 매핑 확인 (5절) | B, 전원 |
| 4 | 담당자 확인 표시 규칙 | 확신도 0.7 미만, 탐지·VLM 불일치, 필수 칸 불명 (6절) | B, FE |

## 2. 원칙

- 칸 하나에 사실 하나만 담는다. AI Hub 진행 문장(예: "[녹색신호] 대로에서 좌회전(선진입)")은 신호·움직임·도로 관계·진입 순서 칸으로 나눈다.
- 값은 정해진 목록에서만 고른다. 모르면 `불명`, 그 상황에 없는 칸이면 `해당없음`.
- 속도 수치는 칸에 넣지 않는다. 움직임 추세(가속·감속 등)만 둔다.
- 상대 차 신호가 화면에 안 보이면 `불명`으로 둔다. 추측하지 않는다.
- 칸마다 기존 계약대로 `value`, `model_value`, `source`, `model_confidence`, `verification_status`, `evidence_frame_ids`를 갖는다.

## 3. 칸 목록

필수: ● 항상 필수 · ◐ 조건부 필수 · ○ 선택. 정답 출처는 평가용(AI Hub 라벨로 알 수 있는지)이다.

| 묶음 | 칸 | 값 목록 | 필수 | AI Hub 정답 |
| --- | --- | --- | --- | --- |
| 범위 | `accident_object` | 차대차 · 차대이륜차 · 기타 · 불명 | ● | 있음 |
| 범위 | `party_count` | 정수 (3 이상이면 범위 밖) | ● | 없음(2로 가정) |
| 장소 | `location_type` | 직선도로 · 사거리_신호있음 · 사거리_신호없음 · T자형교차로 · 회전교차로 · 고속도로 · 주차장 · 차도와비차도 · 불명 | ● | 있음 |
| 장소 | `road_priority` | 동일폭 · A대로 · B대로 · A쪽_일시정지 · B쪽_일시정지 · A쪽만_신호등 · B쪽만_신호등 · 해당없음 · 불명 | ◐ 교차로 | 일부 |
| 신호 | `signal_A`, `signal_B` | 녹색 · 황색 · 적색 · 녹색좌회전 · 녹색직진좌회전 · 신호없음 · 불명 | ◐ 신호 있는 장소 | 35종만 |
| 움직임 | `movement_A`, `movement_B` | 직진 · 좌회전 · 우회전 · 유턴 · 진로변경 · 합류 · 추월 · 회전교차로진입 · 회전교차로회전 · 정차후출발 · 주정차 · 후진 · 출차 · 불명 | ● | 있음 |
| 움직임 | `relative_position` | **B 기준 A가 있던 쪽**(충돌 직전): 같은방향_앞 · 같은방향_뒤 · 맞은편 · 왼쪽 · 오른쪽 · 불명 | ● | 일부 |
| 움직임 | `entry_order` | A먼저 · B먼저 · 동시 · 해당없음 · 불명 | ◐ 교차로 | 일부 |
| 움직임 | `lane_change_A`, `lane_change_B` | 있음 · 없음 · 불명 | ● | 일부 |
| 움직임 | `centerline_cross_A`, `centerline_cross_B` | 있음 · 없음 · 불명 | ● | 일부 |
| 움직임 | `motion_trend_A`, `motion_trend_B` | 가속 · 일정 · 감속 · 정지 · 불명 | ○ | 없음 |
| 충돌 | `collision_part_A`, `collision_part_B` | 앞 · 뒤 · 왼쪽 · 오른쪽 · 앞왼쪽 · 앞오른쪽 · 뒤왼쪽 · 뒤오른쪽 · 불명 | ● | 없음 |
| 충돌 | `collision_time_ms` | 정수(영상 시작부터 ms) · null | ● | 약 5000(라벨 기준) |
| 환경 | `time_condition` | 주간 · 야간 · 새벽황혼 · 불명 | ○ | 없음 |
| 환경 | `weather` | 맑음 · 흐림 · 비 · 눈 · 불명 | ○ | 빈칸 |
| 설명 | `narrative` | 자유 서술 (채점하지 않음) | ○ | 없음 |

`_A`, `_B` 쌍을 따로 세면 모두 22칸이다(채점 대상은 ○ 선택 칸과 `narrative`를 뺀 18칸).
README의 `priority_road`·`movement`·`motion_trend`·`collision_point`·`time_condition`은 위 이름으로 나눴다.
B가 도표를 고르는 데 더 필요한 칸(예: 노면 표시, 긴급자동차, 일시정지 위반)이 있으면 추가한다.

## 4. 탐지·VLM 결과 기록: `vision_detail`

`source`는 지금처럼 `vision` · `manual` · `statement`로 둔다. 판단·보고서·화면은 "기계가 정했나, 사람이 정했나"만 알면 된다.
영상 분석 칸에는 근거로 `vision_detail`을 붙인다. 수동 입력 칸에는 없다.

```json
"lane_change_B": {
  "value": "있음", "model_value": "있음", "source": "vision",
  "model_confidence": 0.55, "verification_status": "needs_review",
  "evidence_frame_ids": ["f071", "f075"], "source_document_ids": [],
  "vision_detail": {
    "detection": {"value": "있음", "confidence": 0.8},
    "vlm": {"value": "없음", "confidence": 0.55},
    "agree": false
  }
}
```

- `detection`이나 `vlm` 중 그 칸을 내지 않은 쪽은 `null`, 이때 `agree`도 `null`이다.
- 같다고 보는 기준: 목록 값은 똑같을 때, `collision_time_ms`는 차이가 200ms(3프레임) 이하일 때.
- `model_confidence`: 하나만 있으면 그 값, 둘이 같으면 높은 값, 다르면 낮은 값.
- `value`: 둘이 다르면 VLM 값을 두고 확인 대상으로 표시한다(실험 결과에 따라 칸별로 바꿀 수 있음).

계약 변경안 (`app/schemas/facts.py`, 합의 후 반영):

```python
class ModelOpinion(TypedDict):
    value: FactValue
    confidence: float | None

class VisionDetail(TypedDict):
    detection: ModelOpinion | None
    vlm: ModelOpinion | None
    agree: bool | None

class FactField(TypedDict):
    ...  # 기존 칸 그대로
    vision_detail: NotRequired[VisionDetail]

class Party(TypedDict):          # 5절
    party_id: str                # P1, P2
    role: Literal["촬영 차량", "상대 차량"]
    track_id: int | None         # YOLO 추적 번호. 화면에 없으면 None

class FactCard(TypedDict):
    ...  # 기존 칸 그대로
    parties: list[Party]
```

## 5. 당사자와 A/B

- 카드의 `_A`, `_B`는 `party_mapping.chart_party_A_id`, `chart_party_B_id`가 가리키는 당사자다.
- 비전은 당사자를 `P1`(촬영 차량), `P2`(상대 차량)로 두고, **임시로** AI Hub 관례대로 1인칭이면 촬영 차량을 B로 둔다. 3인칭이면 `video_ego_party_id`는 `null`이다.
- 도표 기준의 A/B와 보험가입자는 비전이 정하지 않는다. 담당자 확정 때 매핑을 확인하고, 바뀌면 `_A`·`_B` 칸을 함께 맞바꾼다.
- **미정:** 최종 A/B 기준(도표 정의 vs 보험가입자)과 맞바꿀 때 누가(A 화면, B 판단) 하는지.

## 6. 담당자 확인 표시 규칙 (임시)

다음 중 하나면 `needs_review`, 아니면 `unverified`로 두고, 담당자가 확정하면 `human_confirmed`가 된다.

1. `model_confidence` < 0.7 (임시값. 개선용 10건 평가 후 다시 정함)
2. `vision_detail.agree == false`
3. 필수(●·조건 충족 ◐) 칸이 `불명`. 단, **상대 차 신호의 `불명`은 표시하지 않는다**(안 보이는 게 정상인 경우가 많음).

담당자가 고쳐도 `model_value`, `model_confidence`, `vision_detail`은 바꾸지 않는다.

## 7. 평가에서의 쓰임

- 정답 카드는 같은 칸에 `value`만 적는다. 사람이 영상을 보고 채우고, AI Hub 라벨로 알 수 있는 칸은 미리 채운다.
- 채점은 칸별 일치율. `불명`도 하나의 답으로 본다. ○ 선택 칸과 `narrative`는 채점하지 않는다.
- 확인 표시 규칙도 함께 본다: 틀린 칸 중 `needs_review`로 잡힌 비율, 맞은 칸 중 괜히 잡힌 비율.
