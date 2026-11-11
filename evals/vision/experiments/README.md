# 쓰지 않는 시험 (기록용)

지금 카드 경로에서 쓰지 않는 모듈과 스크립트. 결과와 이유는 각 파일 맨 위 설명과 `../README.md`의 시도 기록에 있다.
저장소 맨 위에서 실행한다(예: `.venv/Scripts/python evals/vision/experiments/eval_depth_a.py`).

| 파일 | 무엇 | 왜 안 쓰나 |
| --- | --- | --- |
| `crop_embed.py`, `exp_crop_motion.py` | 상대 차 모습(CLIP)을 움직임 분류기 특징에 추가 | 개선 없음 (11/9) |
| `crop_question.py`, `eval_crop_a.py` | 상대 차만 확대해 VLM에 A 움직임 질문(💰) | 개선 없음 (11/9) |
| `depth3d.py`, `eval_depth_a.py` | 깊이 추정으로 상대 차 궤적 | 개선 없음 (11/9) |
| `opponent_motion.py` | 상대 차 구간별 움직임 꼬리표(코드 규칙) | 안 씀 |
| `vjepa_feat.py`, `extract_vjepa.py`, `train_vjepa_probe.py` | V-JEPA 2 본체 고정 + 판단부 | 전 칸에서 VideoMAE보다 못함 (11/10). 학습한 j3는 `workers/vision/video_model.py` |
| `eval_human64.py`, `eval_motion_a_92.py` | 예전 사람 정답 비교 | `eval_spec_human.py`, `eval_hybrid_a.py`로 대체 |
| `rescore_confirmed.py`, `rescore_type.py` | 예전 재채점 | `replay_cards.py`로 대체 |
| `replace_third_person.py`, `make_review_sheets.py` | 평가 세트 만들 때 한 번 씀 | 끝남 |
