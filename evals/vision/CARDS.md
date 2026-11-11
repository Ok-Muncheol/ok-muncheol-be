# 비전 평가·학습 (정지원, A 담당)

블랙박스 1인칭 영상 → 사실 카드(JSON) 자동 생성의 학습·평가 스크립트. 서비스 코드와 파일 지도는 `workers/vision/README.md`.
데이터·모델·결과는 저장소에 없다(`data/private/`, `evals/results/`는 .gitignore). AI Hub 영상은 외부로 보내지 않는다.

## 지금 카드 설정 (2026-11-11)

| 칸 | 값을 정하는 것 | 확인 표시(needs_review) |
| --- | --- | --- |
| 장소 | 영상 모델 v2 (VideoMAE) | 항상 |
| 움직임 A (상대 차) | 영상 모델 v2. 진로변경·추월만 j3(V-JEPA 2 학습) | 항상 |
| 움직임 B (블랙박스 차) | 움직임 분류기 (YOLO 궤적 + 광학 흐름) | 분류기 ≠ v2, 또는 사고 유형 자신감 < 0.9 |
| 충돌 부위 B | YOLO 박스 + 규칙 | 규칙 |
| 상대 위치 | VLM (gpt-4o) | v3와 다르면 |
| 도로 관계 | VLM (신호 없는 교차로는 장면 분류기) | 항상 + j3와 다르면 |
| 그 밖 | VLM + 유형표·장소 정리 규칙 | 규칙 |

점수(개발 세트 30건 = 개선용 10 + 최종용 20, 개발에 써서 조금 부풀려질 수 있음):
핵심 칸 52%(11/9) → **61%**, 표시 없는 핵심 칸 정확도 60% → **75%**.
사람 라벨 92건: 움직임 B 분류기 69/92, 움직임 A 59/92(VLM 51).

## 평가 원칙

- 판정은 **사람 정답**으로만 한다. AI Hub 라벨 기준 점수는 부풀려지거나 뒤집힌 적이 있다(v4).
  - 사람 92건 = 구준휘님 라벨 64건(움직임 A·B) + 정답 카드 28건(겹치면 구준휘님). 다시 본 라벨은 `evals/results/vision/recheck_a_1110.csv`
  - 정답 카드 30건(`answers/answer_cards.xlsx`): 모든 칸
- 판정 기준은 **결과를 보기 전에** 스크립트 맨 위 설명에 적는다. 두 방법 비교는 정확 맥니마 검정(p < 0.10).

## 스크립트

| 하는 일 | 스크립트 |
| --- | --- |
| 라벨 정리 | `scan_labels.py` → `select_eval_set.py`(평가 30건) → `make_answer_sheet.py`(정답 입력표) |
| 영상 꺼내기 | `extract_videos.py`(평가 영상), `prepare_train.py`, `prepare_train_tr.py`(Training → 16장 저장) |
| 카드 만들기 (💰 VLM 유료) | `run_cards.py --ego --yolo --motion --video` |
| 무료 재조립 | `replay_cards.py <실행 폴더> --ego --yolo --motion --video` (저장된 VLM 답 + 지금 코드) |
| 채점 | `score.py <폴더>`(내부 한국어 값), `score_v1.py <폴더>`(팀 계약 v1 값), `combine_runs.py <폴더1> <폴더2> --out dev30_<이름>` (30건 합산) |
| 파이프라인 확인 | `check_pipeline.py` (저장된 VLM 답으로 `workers/vision/pipeline.py`가 평가와 같은 카드를 내는지, 무료) |
| 영상 모델 학습 | `train_video.py --spec v2 / v3 / j3 --answers train_answers.csv train_answers_tr.csv --epochs 3 --balance` |
| 사고 유형 판단부 | `train_type_head.py` → `workers/vision/type_model.py` |
| 움직임 분류기 | `train_motion.py`, `train_motion_b.py`(사람 라벨 섞음), `train_pick.py`(상대 차 고르기), `train_scene.py` |
| 사람 정답 비교 | `eval_video_human.py`, `eval_spec_human.py --spec j3`, `eval_hybrid_a.py --special j3`, `eval_b_typeflag.py` |
| 그 밖 | `eval_boxes.py`, `eval_ego.py`, `make_silver_set.py`, `silver.py`, `gt_tracks.py`, `exp_human_mix.py`, `build_type_priors.py` |

`experiments/`: 시험했지만 쓰지 않는 것(기준 미달)과 한 번 쓰고 끝난 스크립트. 기록용으로 남긴다.

## 시도 기록 (2026-11-10)

| 시도 | 결과 | 판정 |
| --- | --- | --- |
| VideoMAE v2 재학습(Training 6,438건) | 장소 74%, B 80%, A 62%(사람 정답) | 사용 |
| 장소 = 영상 모델 | 30건 20 vs VLM 15 | 채택 |
| 사람 라벨 4묶음 보정 | B 74 → 65 | 안 씀 |
| 움직임 A = 영상 모델 | 92건 57 vs VLM 51 | 채택 |
| v3(진로변경·추월·상대 위치·도로 관계) | A 52, 진로변경 3/3 | 확인 표시에만 |
| v2+v3 섞기(진로변경·추월만 v3) | 58 vs 56, 맞은 비율 55% | 채택 → j3로 교체 |
| v4(11,275건, 직선도로 추가) | 장소 57, 섞기 56 | 안 씀 |
| 자신 없는 칸 비워 두기 | VLM 자신감이 늘 0.8~0.9 | 불가 |
| V-JEPA 2 본체 고정 + 판단부 | 전 칸 패 | 안 씀 (`experiments/`) |
| j3 = V-JEPA 2 뒤 12층 학습 | 섞기 59 vs 58(1건, 사실상 비김) | 진로변경·추월에 채택 |
| 사고 유형 × 방향 판단부 | 값은 지금보다 낮음, 자신감은 정답률과 맞음 | 자신감만 B 확인 표시에 |
| B 표시 + 유형 자신감 < 0.9 | 92건 표시 없이 틀림 12 → 7 | 채택 |

## 남은 후보

- 사고 유형 상위 5개 중 VLM이 고르기(💰, 상위 5개 안에 정답 81%)
- Qwen3-VL LoRA 미세조정(arXiv 2607.03591: 1인칭 과실 비율, LoRA 4B가 GPT-5 zero-shot을 크게 이김)
- B가 VLM 특수 움직임(주정차 등)일 때도 유형 자신감으로 확인 표시
- 영상 길이 가정: 영상 모델은 프레임 30~110만 본다(AI Hub 잘린 영상 기준). 파이프라인은 20초 넘는 영상을 충돌 앞뒤 10초로 자른다(충돌 시각은 늘 확인 표시)
