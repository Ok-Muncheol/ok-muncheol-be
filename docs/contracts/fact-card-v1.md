# 사고 사실 카드 v1 (A 정지원 → 전원)

작성: 2026-11-11 · 상태: **확정** (영어 값 17칸). v0(11/5 초안)과 11/11 오전 한국어 22칸안을 대체한다.
11/13: 남은 도표(차11-x, 1-2·2-3·2-5·3-5, 1-5·3-7·3-8, 2-2)를 가르려고 **담당자 입력 칸 5개**(`collision_signal_A/B`, `lane_marking_A/B`, `unprotected_left_sign`)와 **점멸 신호 값**(`flashing_yellow`, `flashing_red`)을 더했다(B 요청).
예시: [fact-card-v1.example.json](fact-card-v1.example.json) — 지금 코드로 만든 실제 카드 1장(AI Hub 신호 없는 사거리 영상)

판단(B)·도표 레코드(C)·검색 필터(D)·화면(FE)은 이 문서의 칸 이름과 값을 쓴다.
비전 안에서는 학습·평가에 쓰는 한국어 세분 값을 쓰고, 밖으로 낼 때만 `workers/vision/contract.py`가 이 값으로 바꾼다(7절).

## 1. 카드 모양

`app/schemas/facts.py`의 `FactCard` 그대로 + 두 키를 더한다.

| 키 | 내용 |
| --- | --- |
| `case_id`, `version`, `accident_date`, `status`, `confirmed_by`, `confirmed_at` | 기존 계약 그대로 |
| `fields` | 칸 이름 → `FactField` (3절 17칸) |
| `party_mapping` | 기존 그대로. 1인칭 영상이면 `video_ego_party_id = "P1"`, 기본값 `chart_party_A_id = "P2"`, `chart_party_B_id = "P1"` |
| `evidence_frames` | 기존 그대로. `timestamp_ms`는 **올린 원본 영상** 기준(긴 영상을 잘라 분석해도 되돌려 준다) |
| **`parties`** (추가) | `[{"party_id": "P1", "role": "촬영 차량", "track_id": null}, {"party_id": "P2", "role": "상대 차량", "track_id": null}]` |
| **`model_info`** (추가) | 쓴 모델·질문 버전·걸린 시간·영상 자르기 정보(`pipeline`). 판단에는 쓰지 않음 |

`FactField`는 기존 7개 키(`value`, `model_value`, `source`, `model_confidence`, `verification_status`, `evidence_frame_ids`,
`source_document_ids`) + **`vision_detail`**(영상 분석 칸만, 6절).
`app/schemas/facts.py`에 `parties`·`model_info`·`vision_detail`을 넣을지는 팀에서 정한다(지금은 문서로만 약속).

## 2. A/B 규칙

- **카드의 B = 블랙박스(촬영) 차량, A = 상대 차량.** 1인칭 영상은 항상 이렇다. `_A`·`_B` 칸과 `major_A`·`stop_B` 같은 값의 A/B도 같은 뜻이다.
- 비전은 도표 A/B와 보험 가입자를 정하지 않는다. 도표 A가 누구인지는 담당자가 `party_mapping.chart_party_A_id`로 확정한다.
- **도표 A가 카드 B면(맞바뀌면)** 판단 쪽은 아래처럼 값을 뒤집어 읽는다. 도표 레코드(C)는 **도표 A 기준**으로 적는다.

| 칸 | 맞바뀔 때 |
| --- | --- |
| `movement_A` ↔ `movement_B`, `signal_A` ↔ `signal_B`, `collision_signal_A` ↔ `collision_signal_B`, `lane_marking_A` ↔ `lane_marking_B` | 칸끼리 맞바꿈(값은 그대로) |
| `road_priority` | `major_A` ↔ `major_B`, `stop_A` ↔ `stop_B` (나머지 그대로) |
| `entry_order` | `first` ↔ `second` (나머지 그대로) |
| `relative_position` | `left` ↔ `right`, `same_direction_front` ↔ `same_direction_rear` (`opposite` 그대로) |

## 3. 칸 목록과 값

모르면 `value: null` + `verification_status: "needs_review"`. 그 상황에 없는 칸이면 `none`(도로 관계·진입 순서).
정확도: 사람 정답 92건(장소·움직임) / 개발 세트 30건(나머지, 개발에 써서 조금 부풀려질 수 있음). 같은 값이 카드 `vision_detail.eval`에도 들어간다.

| 칸 | 값 | 도표 선택 | 자동 정확도 |
| --- | --- | --- | --- |
| `accident_type` | `car_to_car` · `other` (3대 이상·차대차 아님) | 범위 | 30/30 |
| `location_type` | `signalized_intersection` · `unsignalized_intersection` · `roundabout` · `straight_road` · `highway` · `other` | ✅ | 74/92 (80%) |
| `road_priority` | `same_width` · `major_A` · `major_B` · `stop_A` · `stop_B` · `other` · `none` | ✅ 신호 없는 교차로 | 21/30 |
| `signal_A`, `signal_B` | `green` · `yellow` · `red` · `green_left_arrow` · `flashing_yellow` · `flashing_red` · `no_signal` | ✅ 신호 교차로(**진입 시점** 신호) | 0/4 → **신호 교차로에서 늘 확인 표시** |
| `collision_signal_A`, `collision_signal_B` | `signal_*`와 같은 값 | 진입 시와 충돌 시 신호가 다른 도표(차1-2, 2-3, 2-5, 3-5) | 담당자 입력 |
| `movement_A`, `movement_B` | `straight` · `left_turn` · `right_turn` · `u_turn` · `lane_change` · `other` | ✅ | A 60/92 (65%), B 69/92 (75%) |
| `lane_marking_A`, `lane_marking_B` | `left_only` · `straight_left` · `straight_only` · `straight_right` · `right_only` · `none` | 교차로 노면 표시 위반(차11-1~11-6) | 담당자 입력 |
| `relative_position` | `same_direction_front` · `same_direction_rear` · `opposite` · `left` · `right` | 맞은편·측면·좌우 도표 | 19/30 (좌·우 구분) |
| `entry_order` | `first` · `second` · `simultaneous` · `none` | ✅ 신호 없는 교차로 | 14/30 |
| `night` | `true` · `false` | 참고(수정요소 자동 적용 안 함) | — |
| `unprotected_left_sign` | `true` · `false` | 비보호 좌회전 표지(차2-2 `false`, 차2-6·4-2 `true`) | 담당자 입력 |
| `collision_time_ms` | 정수(원본 영상 시작부터 ms) · `null` | 근거 프레임 | 17/30 (±200ms) |
| `narrative` | 자유 서술(한국어) | 보고서 참고 | — |

## 4. 값 뜻

- `unsignalized_intersection`: 신호 없는 사거리와 T자형 교차로. `other`: 주차장, 차도와 차도 아닌 곳.
- `major_A` = A가 대로(B가 소로). `stop_A` = A 쪽에 일시정지 표지. `other` = 한쪽에만 신호등. `none` = 교차로가 아님.
- `lane_change` = 진로변경·추월·합류(화면에서 거의 구분되지 않아 묶음). `other` = 정차 후 출발, 주정차, 출차, 후진, 회전교차로 진입·회전.
- `green_left_arrow` = 녹색 좌회전, 녹색 직진+좌회전 동시 신호. `flashing_yellow`·`flashing_red` = 황색·적색 점멸.
- `signal_*` = 그 차가 **교차로에 들어갈 때** 신호, `collision_signal_*` = **충돌할 때** 신호. 둘이 같으면 같은 값을 넣는다.
- `lane_marking_*` = 그 차가 있던 **차로의 방향 화살표**. `left_only` 좌회전 전용, `straight_left` 직진·좌회전, `straight_only` 직진 전용, `straight_right` 직진·우회전, `right_only` 우회전 전용, `none` 표시 없음. 예: 차11-1은 A가 `left_only`에서 직진, B가 `straight_left`에서 좌회전.
- `unprotected_left_sign`: 비보호 좌회전 표지가 있으면 `true`.
- `relative_position`: **B의 진행 방향에서 볼 때 A가 충돌 직전 어디 있었나(어디서 왔나).** `left` = A가 B의 왼쪽(왼쪽 도로에서 진입), `opposite` = 마주 오는 차, `same_direction_front` = B 앞에서 같은 방향.
- `entry_order`: A 기준. `first` = A가 교차로에 먼저 진입.
- `night`: 야간이면 `true`. JSON 참거짓 값이다(`FIELD_OPTIONS`의 문자열 `"true"`와 비교할 때 맞춘다).

## 5. 확인 표시(`verification_status`) 규칙 — 평가로 정함

`needs_review`면 담당자가 봐야 하는 칸, `unverified`면 모델 값 그대로, 담당자가 확정하면 `human_confirmed`.
개발 세트 30건: 카드당 확인할 핵심 칸 약 3.9개, 표시 없는 핵심 칸 정확도 75%(54/72, 상대 위치 좌·우 구분 기준).

| 칸 | 표시 조건 |
| --- | --- |
| `location_type`, `movement_A` | 항상 |
| `signal_A`, `signal_B` | `signalized_intersection`에서 항상 |
| `road_priority`, `entry_order` | `unsignalized_intersection`에서 항상 |
| `movement_B` | 움직임 분류기 ≠ 영상 모델, 또는 사고 유형 자신감 < 0.9 |
| `relative_position` | VLM ≠ 영상 모델 v3(좌·우는 측면으로 묶어 비교) |
| `collision_time_ms` | 긴 영상을 잘라 분석했을 때 항상 |
| 그 밖 | 값이 `null`, VLM 자신감 < 0.7, 탐지·VLM 불일치 |
| 담당자 입력 칸(`collision_signal_*`, `lane_marking_*`, `unprotected_left_sign`) | **표시하지 않음.** 영상 카드에서는 `null` + `unverified`(출처 `manual`)로 나가 확정을 막지 않는다. 도표를 가르는 데 필요할 때 판단이 빈 칸 질문으로 묻는다(9절) |

B는 `human_confirmed` 칸만 도표 선택에 쓴다. `model_confidence`는 참고값이다(VLM 자신감은 0.8~0.9로 거의 일정).

## 6. `vision_detail` (영상 분석 칸만)

```json
"relative_position": {
  "value": "left", "model_value": "left", "source": "vision", "model_confidence": 0.9,
  "verification_status": "unverified", "evidence_frame_ids": ["f071"], "source_document_ids": [],
  "vision_detail": {
    "detection": null,
    "vlm": {"value": "왼쪽", "confidence": 0.9},
    "agree": null,
    "video_v3": "옆",
    "internal_value": "왼쪽",
    "eval": {"accuracy": 0.63, "basis": "개발 세트 30건, 좌·우 구분 (19/30)"}
  }
}
```

- `detection`: 코드·학습 모델의 의견(`source`: 분류기 / 영상 모델 / 영상 모델 j3 / YOLO 박스 등). 없으면 `null`.
- `vlm`: VLM(gpt-4o) 의견. `agree`: 둘이 같은지(한쪽이 없으면 `null`). 모델 의견은 내부 한국어 값이다.
- `internal_value`: 비전 내부 값(v1으로 바꾸며 뭉친 값, 예: `추월` → `lane_change`).
- `eval`: 이 칸의 평가 정확도와 근거. 담당자가 어떤 칸부터 볼지 정하는 데 쓴다.
- 칸에 따라 더 붙는 키: `video_v3`·`video_j3`(확인 표시용 영상 모델 값), `type_choice`, `tidy`(장소 규칙으로 바꾼 전후), `by_place`.
- 담당자가 고쳐도 `model_value`, `model_confidence`, `vision_detail`은 바꾸지 않는다.

## 7. 비전 내부 카드와 변환

비전 모델과 정답 카드(`evals/vision/answers/answer_cards.xlsx`)는 한국어 세분 값을 쓴다(`workers/vision/fields.py`).
`workers/vision/contract.py`의 `to_contract_card`가 이 문서의 값으로 바꾸고, 채점(`evals/vision/score_v1.py`)도 같은 표를 쓴다.
표를 바꾸면 이 문서도 함께 고친다.

| v1 칸 | 내부 칸 | 내부 값 → v1 값 |
| --- | --- | --- |
| `accident_type` | `accident_object` (+ `party_count` 3 이상이면 `other`) | 차대차 → `car_to_car`, 나머지 → `other` |
| `location_type` | `location_type` | 사거리_신호있음 → `signalized_intersection`, 사거리_신호없음·T자형교차로 → `unsignalized_intersection`, 회전교차로 → `roundabout`, 직선도로 → `straight_road`, 고속도로 → `highway`, 주차장·차도와비차도 → `other` |
| `road_priority` | `road_priority` | 동일폭 → `same_width`, A대로/B대로 → `major_A`/`major_B`, A쪽_일시정지/B쪽_일시정지 → `stop_A`/`stop_B`, 한쪽만_신호등 → `other`, 해당없음 → `none` |
| `signal_*` | `signal_*` | 녹색·황색·적색 → `green`·`yellow`·`red`, 녹색좌회전·녹색직진좌회전 → `green_left_arrow`, 신호없음 → `no_signal` |
| `movement_*` | `movement_*` | 직진·좌회전·우회전·유턴 → 그대로 영어, 진로변경·추월·합류 → `lane_change`, 나머지 → `other` |
| `relative_position` | `relative_position` | 같은방향_앞/뒤 → `same_direction_front`/`rear`, 맞은편 → `opposite`, 왼쪽/오른쪽 → `left`/`right` |
| `entry_order` | `entry_order` | A먼저/B먼저/동시/해당없음 → `first`/`second`/`simultaneous`/`none` |
| `night` | `time_condition` | 야간 → `true`, 나머지 → `false` |

담당자 입력 칸 5개는 내부 칸이 없다(`V1_FIELDS`의 내부 칸 `None`, `MANUAL_FIELDS`).
`불명`은 모두 `null` + `needs_review`. 내부에만 있고 v1에 내지 않는 칸: `party_count`, `lane_change_*`, `centerline_cross_*`,
`collision_part_*`, `motion_trend_*`, `weather`.

## 8. `app/schemas/chart_scope.py`(11/6 임시안)와 다른 점

| 칸 | 11/6 임시 값 | v1 |
| --- | --- | --- |
| `location_type` | `other`에 직선도로·고속도로 포함 | `straight_road`·`highway` 따로. 범위 밖 보류는 그대로 |
| `road_priority` | `major_minor` · `stop_sign` | `major_A`·`major_B` · `stop_A`·`stop_B` (도표가 대로·일시정지 쪽으로 갈림) + `other` · `none` |
| `movement_*` | 직진·좌·우·유턴 | + `lane_change` · `other` |
| `entry_order` | 3개 | + `none` |
| `relative_position` | (없음) | 새 칸. 맞은편·측면·좌우로 갈리는 도표용(mvp-chart-scope.md 「지금 카드 칸으로 가를 수 없는 도표」) |
| `night` | 문자열 `"true"`·`"false"` | JSON 참거짓 |

## 9. 저장·수정·확정 API

`public.fact_cards`에 카드 한 버전을 한 행으로 쌓는다(`app/api/facts.py`). 비전 카드는 `save_vision_card`로 저장한다.
- `PATCH /cases/{id}/fact-card`: 고칠 칸의 값만 보낸다(`{"fields": {"movement_A": {"value": "left_turn"}}, "expected_version": 1, "reason": "..."}`).
  고친 칸은 `human_confirmed`, `model_value`·`vision_detail`은 그대로, 새 버전은 draft. 모름은 `null`로 확인한다.
- `POST /cases/{id}/fact-card/confirm`: `needs_review` 칸이 없고 도표 A/B가 정해져야 확정된다.
  **확정하면 값이 있는 칸은 모두 `human_confirmed`**(담당자가 카드 전체를 확인했다는 뜻, 2026-11-11 결정). 화면에 "표시 없는 칸도
  확정하면 확인한 것으로 처리된다"고 안내한다. 판단 중 구분 질문은 비어 있는(모름) 칸에서만 나온다.

## 10. 확인 부탁

- B(구준휘): `chart_scope.py`에 8절 값 반영, 2절 맞바꾸기 규칙을 도표 선택에 넣기.
- C(김지은): 도표 레코드 속성을 도표 A 기준 v1 값으로 적기(`*` 무관 표기는 그대로).
- D(옥석현)·FE: 화면에 보여 줄 칸과 값 이름, `vision_detail.eval`(칸별 정확도) 표시.
