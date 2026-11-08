# 비전 평가 — 사실 카드 칸별 정확도

키프레임 + VLM이 뽑은 사실 카드가 정답과 얼마나 맞는지 칸별로 잰다. 정답은
[AI Hub 교통사고 영상 데이터(597)](https://aihub.or.kr/aihubdata/data/view.do?currMenu=115&topMenu=100&dataSetSn=597)의
Validation 라벨이다. 영상·라벨·설명서·정답셋은 전부 무시 경로 `data/private/aihub/`에 두고 결과는 `evals/results/`에 둔다.
`docs/contracts/mvp-chart-scope.md`의 "자동 정확도(10건)"을 이 평가로 대체하는 것이 목표다.

## 정답 라벨의 구조

라벨 JSON의 `traffic_accident_type`(0~433)이 설명서 코드표의 한 행을 가리키고, 그 행이 장소·장소 특징·A/B 진행·과실비율을
모두 정한다. 코드표는 과실비율 인정기준 도표와 같은 구성이라 도표 선택 평가(B 담당)에도 쓸 수 있다.
라벨 키는 파일마다 조금 다르다(`traffic_accident_type`/`accident_type`, `accident_negligence_rateA`/`accident_negligence_rate`).
`evals/vision/aihub.py`가 흡수한다.

| 카드 칸 | 코드표에서 유도 | 비고 |
| --- | --- | --- |
| `accident_type` | `accident_object == 0` → `car_to_car` | 차대차만 MVP 범위 |
| `location_type` | 장소 열 | T자형 교차로는 `unsignalized_intersection`으로 둔다(카드에 T자형 칸이 없음) |
| `road_priority` | 장소 특징 열 | 동일폭·대로소로·일시정지만. 그 외 특징(교차로 내 진로변경 등)은 `null` |
| `movement_A/B` | 진행 열에서 괄호·신호 표기 제거 후 동작어 | 추월·회전·추돌은 `null` |
| `signal_A/B` | 진행 열의 첫 `[..]` 표기 | 신호 없는 교차로는 `no_signal`. 신호 교차로인데 표기 없으면 `null` |
| `entry_order` | 진행 열의 (선진입)/(후진입) | A 기준. 표기 없는 유형은 `null` |
| `night` | 라벨에 없음 | 항상 `null`. 채점 제외. 별도 라벨링 필요 |

`null`인 정답 칸은 채점에서 뺀다. 1인칭(블랙박스) 영상은 촬영 차량이 B로 고정되어 A/B 순서가 정해지고,
3인칭(CCTV) 영상은 모델이 A/B를 정할 수 없으므로 `movement`·`signal` 쌍을 맞바꿔도 맞은 것으로 센다.

## 준비 (한 번)

1. AI Hub 가입 → 데이터셋 페이지에서 다운로드 승인 신청 → API key 발급 → `.env`에 `AIHUB_API_KEY=`.
2. 데이터셋 페이지의 "데이터 설명서" PDF를 `data/private/aihub/`에 둔다.
3. `pip install -e ".[dev,vision-eval]"` (pdfplumber·OpenCV·anthropic).
4. 코드표 추출: `python scripts/aihub_codebook.py "data/private/aihub/<설명서>.pdf" data/private/aihub/codebook.json`
5. 라벨·영상 다운로드. 공식 `aihubshell`의 분할 파일 병합이 macOS bash 3.2에서 깨져 빈 파일을 남기므로,
   `curl -H "apikey:$AIHUB_API_KEY" "https://api.aihub.or.kr/down/0.6/597.do?fileSn=<filekey,...>"`로 받은 tar를 풀고
   `*.partN`을 번호순으로 이어 붙인다. filekey는 `aihubshell -mode l -datasetkey 597`로 본다.
   차대차 교차로 Validation: 라벨 `509415,509417,509418,509422`, 영상 `509462(신호등 없음 1GB),509463(신호등 있음 2GB)`.
6. 정답셋 생성:
   ```
   python -m evals.vision.dataset --labels data/private/aihub/raw/labels \
       --codebook data/private/aihub/codebook.json --videos data/private/aihub/raw/videos \
       --out data/private/aihub/vision_gt.jsonl
   ```

## 실행

```
# 예측 (--provider openai는 OPENAI_API_KEY, anthropic은 ANTHROPIC_API_KEY). 재실행하면 끝난 사건은 건너뛴다
python -m evals.vision.run_vlm --gt data/private/aihub/vision_gt.jsonl --provider openai --model gpt-5.5 \
    --strategy adaptive --frames 10 --limit 50 --out evals/results/vision_pred_gpt55_adaptive.jsonl
python -m evals.vision.run_vlm --gt data/private/aihub/vision_gt.jsonl --provider openai --model gpt-5.5 \
    --strategy uniform --frames 10 --limit 50 --out evals/results/vision_pred_gpt55_uniform.jsonl
# 10건 참고값과 같은 모델로 비교하려면 --model gpt-4o
# 채점
python -m evals.vision.evaluate --gt data/private/aihub/vision_gt.jsonl \
    --pred evals/results/vision_pred_adaptive.jsonl --out evals/results/vision_report_adaptive.json
```

`adaptive`는 프레임 차분 누적치를 등분해 움직임이 큰 구간(충돌 전후)에 프레임을 몰아 뽑고, `uniform`은 같은 장수를
균등하게 뽑는 기준선이다. 두 전략을 같은 사건·같은 장수로 돌려야 비교가 된다.

채점 결과는 칸별 정확도·예측 `null` 비율·혼동 행렬·확신도 구간별 정확도를 담는다. 확신도 구간별 정확도는
카드 확인 임계값(0.7 제안값)을 정하는 근거다. 결과를 문서에 옮길 때는 모델·전략·장수·사건 수·비용을 같이 적는다.

## 결과 2026-11-06 — gpt-5.4-mini, 10프레임, MVP 범위 438건(영상 있는 건)

| 칸 | n | adaptive | uniform | 예측 null(adaptive) |
| --- | --- | --- | --- | --- |
| location_type | 438 | 0.63 | 0.60 | 0.00 |
| movement_A | 421 | 0.77 | 0.78 | 0.05 |
| movement_B | 431 | 0.67 | 0.64 | 0.04 |
| signal_A | 389 | 0.42 | 0.35 | 0.46 |
| signal_B | 363 | 0.40 | 0.34 | 0.37 |
| road_priority | 168 | 0.21 | 0.15 | 0.73 |
| entry_order | 62 | 0.06 | 0.02 | 0.74 |

전략당 입력 227만·출력 23만 토큰, 약 $2.8, 건당 3.8초(4병렬). 원본 보고서는 `evals/results/vision_report_gpt54mini_*.json`.

- **A/B 쌍은 맞바꿔도 맞은 것으로 센 수치다.** 설명서의 "1인칭은 본인=B" 규칙과 달리 실제 라벨은 도표 A/B를 따르고
  촬영 차량이 A인 건이 많았다(육안 2건 확인, movement_A 오답 96건이 맞바꾸면 정답). 촬영 차량이 A/B 중 누구인지는
  라벨에 없으므로 카드의 `video_ego_party_id`↔도표 A/B 매핑 정확도는 이 데이터로 잴 수 없다.
- `location_type`은 신호 있는 사거리 0.84, 신호 없는 사거리 0.31. 신호 없는 교차로를 `other`(71건)나
  `signalized`(51건)로 본다. 골목 교차로를 교차로로 인식하지 못하는 것이 주된 오답이다.
- `movement_B` 오답은 거의 전부 좌회전을 직진으로 본 것(120/130). 10장으로는 회전을 읽기 어렵다.
- `signal`은 절반 가까이 `null`이고 `<0.5` 확신도 구간 정확도가 0.1~0.2, `>=0.7` 구간이 0.55~0.7이다.
  신호 칸은 확신도 0.7 임계값이 유효하다. `movement`는 확신도와 정확도가 거의 무관해 임계값으로 거를 수 없다.
- adaptive가 uniform보다 신호·장소·도로관계에서 3~7%p 높고 비용은 같다. 진행 방향은 차이 없다.

## 한계

- Validation 라벨은 유형 분포가 치우쳐 있다(직진 대 직진이 다수). 칸별 정확도와 함께 혼동 행렬을 봐야 한다.
- 라벨 자체의 오류가 있다(코드표 과실비율과 라벨 과실비율이 다른 건이 약 1%). 정답을 절대 기준으로 삼지 않는다.
- `night`·`insured_party_id`는 정답이 없다.

### 후속 실험 2026-11-07: 장소 입력 가정 + 프레임 수·해상도

같은 100건(adaptive, gpt-5.4-mini)에 `--known-fields location_type`으로 장소를 정답으로 넣은 뒤
프레임 수와 해상도를 바꿨다. 평균은 입력으로 준 칸과 accident_type을 뺀 `field_mean`이다.

| 설정 | movement_A | movement_B | signal_A | signal_B | road_priority | micro 평균 | 건당 비용 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 원래(장소 미입력, 10f/768) | 0.75 | 0.62 | 0.39 | 0.32 | 0.24 | - | $0.006 |
| 장소 입력, 10f/768 | 0.76 | 0.62 | 0.63 | 0.59 | 0.19 | 0.596 | $0.006 |
| 장소 입력, 24f/768 | 0.76 | 0.65 | 0.65 | 0.63 | 0.22 | 0.613 | $0.011 |
| 장소 입력, 10f/1280 | 0.75 | 0.61 | 0.63 | 0.58 | 0.19 | 0.586 | $0.012 |

- 장소를 알려주면 신호 칸이 0.3대에서 0.6대로 오른다. 신호 없는 교차로의 `no_signal`이 결정되기 때문이다.
- 24프레임은 진행·신호에서 2~4%p 오르고 비용은 1.7배. 1280px은 효과가 없다(신호 null이 오히려 늘었다).
- 진행 방향은 어떤 설정에서도 0.6~0.76에 머문다. 프레임 샘플링이 아니라 판정 방식의 문제이므로 궤적(YOLO 추적·광류) 접근이 필요하다.

### 후속 실험 2026-11-07: 궤적 특징(광류·YOLO) 결합

`evals/vision/trajectory.py`가 150프레임 전체에서 촬영 차량 회전각(광류)과 상대 차량 궤적(YOLO11n + ByteTrack)을
뽑아 텍스트로 프롬프트에 붙인다(`--trajectory-cache`). 영상 1건당 약 5초(M4 Pro), 결과는 JSON 캐시.
같은 100건, 장소 입력 + 24프레임 공통.

| 설정 | movement_A | movement_B | signal_A | signal_B | road_priority | micro 평균 | 진행 A/B 둘 다 맞음 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 궤적 없음 | 0.76 | 0.65 | 0.65 | 0.63 | 0.22 | 0.613 | 39 |
| 궤적 | 0.81 | 0.65 | 0.60 | 0.57 | 0.14 | 0.596 | 39 |
| 궤적 + 진행 판정 기준 프롬프트 | 0.79 | 0.69 | 0.56 | 0.58 | 0.41 | 0.618 | 43 |

- VLM은 광류의 촬영 차량 회전 판정을 97/100건에서 그대로 따른다. 광류 부호·크기는 일관된다(좌회전 +53~99도, 우회전 -85도).
- 그래도 진행 칸이 크게 오르지 않는 이유: 라벨의 "좌회전"은 차체 회전이 아니라 의도(좌회전 차로에서 진입 중 충돌 등)이고,
  상대 차의 "가로지름"을 같은 방향 차의 좌회전인지 옆 도로 차의 직진인지 구분하려면 원래 진행 방향을 읽어야 한다.
  궤적은 가로지름 자체는 보여주지만 해석은 여전히 VLM 몫이다.
- 100건에서 ±5%p는 잡음 범위다(실행 간 맞고 틀림이 뒤바뀐 건 19+19).

### 품질 게이트와 단순 범위 2026-11-08

`evals/vision/quality.py`가 영상마다 선명도(라플라시안 분산 중앙값)와 비트레이트를 `video_quality`로 정답셋에 넣는다.
채점기와 라벨 도구는 `--min-sharpness`(게이트)와 `--simple-scope`(유턴·우회전끼리 제외)를 받는다.
측정에서 몰래 빼는 것이 아니라 제품의 "화질 낮음 → 신호 칸은 질문" 게이트를 그대로 재는 것이며, 전체 수치와 함께 보고한다.

| 실행 | 필터 | n | 진행(A/B 평균) | 신호 | micro 평균 |
| --- | --- | --- | --- | --- | --- |
| 438건 adaptive, 장소 미입력 | 없음 | 438 | 0.72 | 0.41 | 0.543 |
| 438건 adaptive, 장소 미입력 | sharpness ≥ 70 | 254 | 0.73 | 0.46 | 0.566 |
| 100건 장소 입력+24f+궤적 | 없음 | 100 | 0.74 | 0.57 | 0.618 |
| 100건 장소 입력+24f+궤적 | sharpness ≥ 70 | 46 | 0.77 | 0.71 | 0.677 |
| 100건 장소 입력+24f+궤적 | ≥ 70 + 단순 범위 | 42 | 0.84 | 0.71 | 0.702 |

- 게이트는 장소를 알 때만 신호 칸을 크게 올린다(0.57→0.71). 장소를 모르면 신호 오답의 주원인이 장소 혼동이라 화질을 걸러도 0.41→0.46에 그친다.
- 전체 449건 sharpness 분위(10/25/50/75/90%): 24.6 / 45.7 / 87.7 / 174.2 / 296.6. 기준 70이면 약 57%가 통과한다.

### 중복 영상 2026-11-08

`evals/vision/dedupe.py`: 449건 중 **44그룹 96건이 같은 영상**이었다(최대 6벌). 이 중 11그룹은 사본끼리 사고 유형·과실이
다르다(예: 같은 영상이 60번/70번 유형, 과실 0:100 / 80:20). 사본은 색감·잘림이 다르고 **시작 시점이 1~2초 어긋난 것도 있어**
파일 해시나 같은 프레임 번호 비교로는 잡히지 않는다.

- 후보: 프레임 12장을 가운데 70%만 잘라 dHash, ±3장 밀어 가며 겹치는 프레임의 평균 해밍 거리 ≤ 17.
- 확정: 기준 프레임 2곳(30, 75)을 상대 영상의 ±3초 범위에서 5프레임 간격으로 ORB+RANSAC 정합. 두 곳 모두 ≥ 60이거나
  한 곳이 ≥ 150이면 중복. 평균 거리 ≤ 6이면 ORB 없이 확정.
- 사람이 라벨 도구에서 `D`로 신고한 쌍(`data/private/aihub/manual_duplicates.jsonl`)은 무조건 같은 그룹으로 합친다.
- 자동으로 잡힌 60여 쌍을 눈으로 확인했고 오탐은 없었다. 기준은 그 확인 결과로 정했다.

정답셋에 `duplicate_conflict`(충돌 그룹 전부)와 `duplicate_of`(같은 라벨 그룹의 비대표)를 기록하고,
`select_rows`가 기본으로 둘 다 뺀다(채점기·라벨 도구 공통). 제외 후 사용 가능 영상은 449 − 28 − 35 = 386건.
