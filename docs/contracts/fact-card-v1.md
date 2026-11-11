# 사고 사실 카드 v1 (A 정지원 → 전원)

작성: 2026-11-11 · 상태: **팀 확정 요청**. v0(11/5 초안)을 대체한다.
예시: [fact-card-v1.example.json](fact-card-v1.example.json) — 지금 코드로 만든 실제 카드 1장(AI Hub 신호 없는 사거리 영상)

비전 파이프라인이 내는 카드와 사람이 붙이는 정답 카드(`evals/vision/answers/answer_cards.xlsx`)는 **같은 칸 이름, 같은 값**을 쓴다.
판단(B)·도표 레코드(C)·검색 필터(D)·화면(FE)은 이 문서의 값을 그대로 쓴다. `app/schemas/chart_scope.py`의 영어 값(11/6 임시안)은
이 문서 값으로 바꾼다(8절 대응표).

## 1. 카드 모양

`app/schemas/facts.py`의 `FactCard` 그대로 + 두 가지를 더한다.

| 키 | 내용 |
| --- | --- |
| `case_id`, `version`, `accident_date`, `status`, `confirmed_by`, `confirmed_at` | 기존 계약 그대로 |
| `fields` | 칸 이름 → `FactField` (2절 칸 목록) |
| `party_mapping` | 기존 그대로. 1인칭 영상이면 `video_ego_party_id = "P1"`, 기본값 `chart_party_A_id = "P2"`, `chart_party_B_id = "P1"` |
| `evidence_frames` | 기존 그대로 |
| **`parties`** (추가) | `[{"party_id": "P1", "role": "촬영 차량", "track_id": null}, {"party_id": "P2", "role": "상대 차량", ...}]` |
| **`model_info`** (추가) | 쓴 모델·질문 버전·걸린 시간. 판단에는 쓰지 않음 |

`FactField`는 기존 7개 키(`value`, `model_value`, `source`, `model_confidence`, `verification_status`, `evidence_frame_ids`,
`source_document_ids`) + **`vision_detail`**(영상 분석 칸만, 6절).

## 2. A/B 규칙

- **카드의 B = 블랙박스(촬영) 차량, A = 상대 차량.** 1인칭 영상은 항상 이렇다. `_A`·`_B` 칸과 `A대로`·`A먼저` 같은 값의 A/B도 같은 뜻이다.
- 도표의 A/B가 카드와 반대면(도표가 블랙박스 차를 A로 정의) 담당자 확정 때 `party_mapping`을 바꾸고 `_A`↔`_B` 칸, `A대로`↔`B대로`,
  `A먼저`↔`B먼저`, `A쪽_*`↔`B쪽_*`를 함께 맞바꾼다. 비전은 도표 A/B와 보험 가입자를 정하지 않는다.
- `relative_position`은 **B(블랙박스 차) 기준 A가 있던 쪽**(충돌 직전).

## 3. 칸 목록과 값

모르면 `불명`, 그 상황에 없는 칸이면 `해당없음`. 값은 목록에서만 고른다.
정확도: 개발 세트 30건(사람 정답 카드, 개발에 써서 조금 부풀려질 수 있음) / 사람 라벨 92건(장소·움직임만).

| 칸 | 값 | 도표 선택 | 자동 정확도 |
| --- | --- | --- | --- |
| `accident_object` | 차대차 · 차대이륜차 · 기타 · 불명 | 범위 | 30/30 |
| `party_count` | 정수 (3 이상이면 범위 밖) | 범위 | 30/30 |
| `location_type` | 직선도로 · 사거리_신호있음 · 사거리_신호없음 · T자형교차로 · 회전교차로 · 고속도로 · 주차장 · 차도와비차도 · 불명 | ✅ | 20/30 · 사람 68/92 |
| `road_priority` | 동일폭 · A대로 · B대로 · A쪽_일시정지 · B쪽_일시정지 · A쪽만_신호등 · B쪽만_신호등 · 해당없음 · 불명 | ✅ 신호 없는 교차로 | 21/30 |
| `signal_A`, `signal_B` | 녹색 · 황색 · 적색 · 녹색좌회전 · 녹색직진좌회전 · 신호없음 · 불명 | ✅ 신호 교차로 | 0/4 → **당분간 늘 확인 표시** |
| `movement_A`, `movement_B` | 직진 · 좌회전 · 우회전 · 유턴 · 진로변경 · 합류 · 추월 · 회전교차로진입 · 회전교차로회전 · 정차후출발 · 주정차 · 후진 · 출차 · 불명 | ✅ | A 19/30 · 사람 59/92, B 21/30 · 사람 69/92 |
| `relative_position` | 같은방향_앞 · 같은방향_뒤 · 맞은편 · 왼쪽 · 오른쪽 · 불명 | 직선도로 도표용 | 19/30 |
| `entry_order` | A먼저 · B먼저 · 동시 · 해당없음 · 불명 | ✅ 신호 없는 교차로 | 14/30 |
| `lane_change_A`, `lane_change_B` | 있음 · 없음 · 불명 | 진로변경 도표용 | 23/30, 26/30 |
| `centerline_cross_A`, `centerline_cross_B` | 있음 · 없음 · 불명 | 중앙선 도표용 | 24/30, 29/30 |
| `collision_part_A`, `collision_part_B` | 앞 · 뒤 · 왼쪽 · 오른쪽 · 앞왼쪽 · 앞오른쪽 · 뒤왼쪽 · 뒤오른쪽 · 불명 | 참고 | 6/30, 22/30 |
| `collision_time_ms` | 정수(영상 시작부터 ms) · null | 근거 프레임 | 17/30(±200ms) |
| `motion_trend_A`, `motion_trend_B` | 가속 · 일정 · 감속 · 정지 · 불명 | 참고(채점 안 함) | — |
| `time_condition` | 주간 · 야간 · 새벽황혼 · 불명 | 참고(채점 안 함) | — |
| `weather` | 맑음 · 흐림 · 비 · 눈 · 불명 | 참고(채점 안 함) | — |
| `narrative` | 자유 서술 | 보고서 참고 | — |

## 4. 칸 값 뜻 (헷갈리는 것)

- `T자형교차로`는 신호 유무와 상관없이 T자 모양 교차로. 도표가 "신호 없는 교차로"로 묶으면 B가 `사거리_신호없음`과 같이 다룬다.
- `A대로` = A가 대로, B가 소로. `A쪽_일시정지` = A 쪽에 일시정지 표지. `A쪽만_신호등` = A 쪽에만 신호등.
- `A먼저` = A가 교차로에 먼저 진입(선진입). 직선도로 등 교차로가 아니면 `해당없음`.
- `진로변경`(차로를 바꾸며 들어옴)과 `추월`(앞지르려다 부딪힘)은 화면에서 비슷하다. 비전은 둘 다 맞힐 때가 있지만 서로 헷갈린다.
- `정차후출발`은 서 있다가 출발, `주정차`는 서 있던 중 부딪힘, `출차`는 주차 자리에서 나옴.

## 5. 확인 표시(`verification_status`) 규칙 — 평가로 정함

`needs_review`면 담당자가 봐야 하는 칸, `unverified`면 모델 값 그대로(담당자가 안 봐도 되는 칸), 담당자가 확정하면 `human_confirmed`.
개발 세트 30건: 카드당 확인할 핵심 칸 약 3.9개, 표시 없는 핵심 칸 정확도 75%(11/9 60%), 틀린 핵심 칸의 76%를 표시로 잡음.

| 칸 | 표시 조건 |
| --- | --- |
| `location_type`, `movement_A` | 항상 |
| `signal_A`, `signal_B` | 신호 교차로(`사거리_신호있음`)에서 항상 — 신호 판단이 아직 약함(30건 0/4) |
| `road_priority`, `entry_order` | 신호 없는 교차로(`사거리_신호없음`, `T자형교차로`)에서 항상. 영상 모델(j3)과 다르면 도로 관계 표시 |
| `movement_B` | 분류기 ≠ 영상 모델, 또는 사고 유형 자신감 < 0.9 |
| `relative_position` | VLM ≠ 영상 모델(v3, 왼쪽·오른쪽은 '옆'으로 묶어 비교) |
| `collision_part_B` | YOLO 박스 판단이 없을 때 |
| 그 밖 | VLM 자신감 < 0.7, 탐지·VLM 불일치, 필수 칸이 `불명`(1인칭 상대 차 신호 `불명`은 제외) |

B는 `human_confirmed` 칸만 도표 선택에 쓴다(기존 규칙 그대로). `model_confidence`는 참고값이다(VLM 자신감은 0.8~0.9로 거의 일정).

## 6. `vision_detail` (영상 분석 칸만)

```json
"movement_B": {
  "value": "직진", "model_value": "직진", "source": "vision", "model_confidence": 0.97,
  "verification_status": "unverified", "evidence_frame_ids": ["f071"], "source_document_ids": [],
  "vision_detail": {
    "detection": {"value": "직진", "confidence": 0.97, "source": "분류기", "rule": "직진",
                  "video_model": "직진", "type_confidence": 1.0},
    "vlm": {"value": "직진", "confidence": 0.9},
    "agree": true
  }
}
```

- `detection`: 코드·학습 모델의 의견(`source`: 분류기 / 영상 모델 / 영상 모델 j3 / YOLO 박스 등). 없으면 `null`.
- `vlm`: VLM(gpt-4o) 의견. `agree`: 둘이 같은지(한쪽이 없으면 `null`).
- 칸에 따라 더 붙는 키: `video_v3`·`video_j3`(확인 표시용 영상 모델 값), `tidy`(장소 규칙으로 바꾼 전후), `by_place`.
- 담당자가 고쳐도 `model_value`, `model_confidence`, `vision_detail`은 바꾸지 않는다.

## 7. 정답 카드 형식

사람이 붙이는 정답 카드는 같은 칸 이름의 `value`만 적는다(엑셀 한 줄 = 영상 하나). 채점은 칸별 일치, `불명`도 하나의 답.
도표 선택 평가(B·C)도 같은 형식의 카드를 입력으로 쓰면 된다.

## 8. 기존 `chart_scope.py` 값과 대응 (바꿀 때 참고)

| 칸 | 11/6 임시 값 | v1 값 |
| --- | --- | --- |
| 칸 이름 | `accident_type` | `accident_object` (`car_to_car` → `차대차`) |
| 칸 이름 | `night` (true/false) | `time_condition` (야간 → true) — 도표 선택에 안 씀 |
| `location_type` | `signalized_intersection` | `사거리_신호있음` |
| | `unsignalized_intersection` | `사거리_신호없음`, `T자형교차로` |
| | `roundabout` | `회전교차로` |
| | `other` | `직선도로`, `고속도로`, `주차장`, `차도와비차도` (이제 각각 구분됨) |
| `road_priority` | `same_width` / `major_minor` / `stop_sign` | `동일폭` / `A대로`·`B대로`(대로 쪽 구분) / `A쪽_일시정지`·`B쪽_일시정지` |
| `movement_*` | `straight` / `left_turn` / `right_turn` / `u_turn` | `직진` / `좌회전` / `우회전` / `유턴` + 진로변경 등 9개 |
| `signal_*` | `green` / `yellow` / `red` / `green_left_arrow` / `no_signal` | `녹색` / `황색` / `적색` / `녹색좌회전` / `신호없음` + `녹색직진좌회전` |
| `entry_order` | `first` / `second` / `simultaneous` (도표 A 기준) | `A먼저` / `B먼저` / `동시` (카드 A 기준) + `해당없음` |
| (없음) | — | `relative_position`, `lane_change_*`, `centerline_cross_*`, `collision_part_*` 등 |

## 9. 확인 부탁

- B(구준휘): 도표 A/B를 정하는 방법(특히 대로·소로, 진입 순서)과 2절 맞바꾸기 규칙이 맞는지. 범위 게이트를 직선도로 등으로 넓힐 때 쓸 칸이 충분한지.
- C(김지은): 도표 레코드 속성 값을 이 값으로 적을 수 있는지(`*` 무관 표기는 그대로).
- D(옥석현)·FE: 화면에 보여 줄 칸과 값 이름.
