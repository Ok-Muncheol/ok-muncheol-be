# workers/vision — 블랙박스 영상 → 사실 카드 (A 정지원)

영상 파일 하나를 받아 팀 계약 사실 카드 v1(영어 17칸, [docs/contracts/fact-card-v1.md](../../docs/contracts/fact-card-v1.md))을 만든다.
학습·평가 스크립트는 [evals/vision/README.md](../../evals/vision/README.md).

## 흐름

```
analyze_video(case_id, 로컬 영상 경로)                          pipeline.py
 ├ 1. 영상 맞추기: 15fps로, 20초가 넘으면 충돌 앞뒤 10초만        pipeline.normalize
 ├ 2. 비전 분석 (학습한 모델·계산, 무료)                         pipeline.build_detections
 │    ├ 카메라 움직임·충돌 순간 (광학 흐름)                      ego_motion.py
 │    ├ 차 찾기·추적·상대 차 고르기 (YOLO11s)                    detection.py, tracks.py, box_features.py
 │    ├ 장면 분류기 (CLIP + 작은 분류기)                         scene_embed.py
 │    ├ 움직임 분류기 (궤적 + 흐름)                              motion_model.py
 │    ├ 영상 모델 v2·v3 (VideoMAE), j3 (V-JEPA 2)                video_model.py
 │    └ 사고 유형 × 방향 자신감                                  type_model.py, accident_types.py
 ├ 3. VLM 질문 (gpt-4o, 영상 1건 약 37원)                        prompts.py → vlm.py
 ├ 4. 의견 모으기·값 고르기·확인 표시 → 내부 카드(한국어)        fact_card.py (칸·값: fields.py)
 └ 5. 내부 카드 → v1 카드(영어 17칸, 담당자 입력 5칸 포함) + 칸별 평가 정확도          contract.py
```

## 한국어 값과 영어 값

- **비전 안(1~4단계)은 한국어 세분 값**(`A대로`, `추월`, `왼쪽` …)이다. 모델이 이 값으로 학습됐고 정답 카드·채점도 이 값을 쓴다. 정의는 `fields.py`.
- **밖으로 나가는 카드는 영어 v1 값만**(`major_A`, `lane_change`, `left` …). 바꾸는 곳은 `contract.py` 한 곳뿐이다.
- 변환표를 바꾸면 `docs/contracts/fact-card-v1.md` 7절과 `contract.FIELD_ACCURACY`(칸별 정확도)를 함께 고친다. 정확도는 `evals/vision/score_v1.py`로 다시 잰다.

## 파일

| 파일 | 하는 일 |
| --- | --- |
| `pipeline.py` | **진입점.** `analyze_video` / `analyze_video_sync`. 실패는 `VisionPipelineError(stage, code, retryable)` |
| `contract.py` | 내부 카드 → v1 카드 변환, 칸별 평가 정확도(`vision_detail.eval`) |
| `fact_card.py` | 모델 의견과 VLM 답 모으기, 확인 표시 규칙, 카드 조립 |
| `prompts.py` | VLM에 보낼 사진 고르기·그리기, 질문문(기본 v0.2) |
| `vlm.py` | VLM 연결: `OpenAIVLM`(서비스), `ClaudeVLM`·`OllamaVLM`(비교 실험) |
| `fields.py` | 내부 칸 목록과 한국어 값, 장소에 따른 정리 규칙 |
| `ego_motion.py` | 광학 흐름으로 블랙박스 차(B) 움직임과 충돌 순간 계산 |
| `detection.py` | YOLO 차·신호등 탐지와 추적, 상대 차(A) 고르기 |
| `tracks.py` | 궤적을 VLM이 읽을 글로 요약, 사진에 박스 그리기 |
| `box_features.py` | 상대 차 박스 위치로 칸 판단(충돌 부위 B 등) |
| `opponent_pick.py` | 상대 차 고르기 학습 모델(시험, 파이프라인에서는 안 씀) |
| `scene_embed.py` | CLIP 장면 특징 + 장소·도로 관계 분류기 |
| `motion_model.py` | 움직임 분류기(B 움직임 값) |
| `video_model.py` | 영상 모델 v2(장소·A·B), v3·j3(진로변경·상대 위치·도로 관계 확인용) |
| `type_model.py`, `accident_types.py` | 사고 유형표, 유형 × 방향 자신감(B 확인 표시) |
| `env.py` | `workers/vision/.env` 읽기(키 값은 출력 안 함) |

## 실행에 필요한 것

- 패키지: `pip install -e ".[vision-eval]"`(torch·transformers·ultralytics·OpenCV·scikit-learn·openai 등, `pyproject.toml`). 팀 CI에는 없고, 테스트는 없으면 건너뛴다
- 모델 파일(`data/private/models/`, git 제외): `video_model_tr.pt`, `video_model_v3.pt`, `video_model_j3.pt`, `type_head.pt`,
  `motion_movement_B.joblib`, `scene_location_type.joblib`, `scene_road_priority.joblib`, `yolo11s.pt`
- Hugging Face 본체(처음 실행 때 내려받음): `MCG-NJU/videomae-base-finetuned-kinetics`, `facebook/vjepa2-vitl-fpc16-256-ssv2`, `openai/clip-vit-base-patch16`
- 키: `OPENAI_API_KEY`
- 속도(노트북 GPU 8GB): 첫 영상 약 30초(모델 불러오기), 이후 영상당 약 6초 + VLM

## 확인 방법 (무료)

```powershell
.venv/Scripts/python -m pytest tests/vision                 # 변환·영상 맞추기·단계·실패 처리
.venv/Scripts/python evals/vision/check_pipeline.py          # 저장된 VLM 답으로 30건: 평가 결과와 같은 카드인지
```
