"""V-JEPA 2(Meta, 2025)로 영상 특징을 뽑는다. 영상 모델(video_model.py, VideoMAE)의 '눈'을 바꿔 보는 시험(2026-11-10).

왜: VideoMAE(Kinetics)는 '무엇이 보이나'에 강하고, 우리가 틀리는 칸(움직임 A, 진로변경·추월)은 '어떻게 움직이나' 문제다.
V-JEPA 2는 움직임 시험(SSv2, 본체 고정 + 판단부만 학습)에서 77.3%(이전 최고 69.7%).
- 모델: facebook/vjepa2-vitl-fpc16-256-ssv2 (ViT-L, 16장, 256×256, MIT 라이선스). 본체는 고정(학습 안 함)
- 입력: video_model.load_clip과 같은 16장(224×224, 저장분 clip16 재사용) → 256으로 키움
- 출력: grid  (8, 4, 4, 1024) float16 = 시간 8칸 × 화면 4×4칸 평균(좌우·앞뒤 위치를 남기려고)
        ssv2  (1024,) float32 = SSv2 움직임 판단부(어텐션 풀링)의 요약
노트북 RTX 4070(8GB)에서 영상 1건 약 0.09초, GPU 메모리 0.8GB(2026-11-10 측정)
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MODEL_ID = "facebook/vjepa2-vitl-fpc16-256-ssv2"
FEAT_DIR = ROOT / "evals/results/vision/vjepa_feat"
SIZE = 256
GRID = 4
MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)

_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        os.environ.setdefault("HF_HOME", str(ROOT / "data/private/models/hf"))
        import torch
        from transformers import VJEPA2ForVideoClassification

        _MODEL = VJEPA2ForVideoClassification.from_pretrained(MODEL_ID, torch_dtype=torch.float16).cuda().eval()
    return _MODEL


def features(clips):
    """clips: (N, 16, 224, 224, 3) uint8 → (grid (N,8,4,4,1024) float16, ssv2 (N,1024) float32) numpy."""
    import torch
    import torch.nn.functional as F

    m = _model()
    x = torch.from_numpy(clips).cuda().float().div_(255)
    x = (x - torch.tensor(MEAN, device="cuda")) / torch.tensor(STD, device="cuda")
    n, t = x.shape[:2]
    x = x.permute(0, 1, 4, 2, 3).reshape(n * t, 3, *x.shape[2:4])
    x = F.interpolate(x, size=(SIZE, SIZE), mode="bilinear", align_corners=False).reshape(n, t, 3, SIZE, SIZE)
    with torch.no_grad():
        h = m.vjepa2(pixel_values_videos=x.half(), skip_predictor=True).last_hidden_state  # (N, 8*16*16, 1024)
        pooled = m.pooler(h).float()
        side = SIZE // 16
        g = h.reshape(n, t // 2, side, side, -1).permute(0, 1, 4, 2, 3).reshape(n * (t // 2), -1, side, side)
        g = F.adaptive_avg_pool2d(g.float(), GRID).reshape(n, t // 2, -1, GRID, GRID).permute(0, 1, 3, 4, 2)
    return g.half().cpu().numpy(), pooled.cpu().numpy()


def load_feat(name: str):
    """저장된 특징 (grid, ssv2). 없으면 None."""
    import numpy as np

    p = FEAT_DIR / f"{name}.npz"
    if not p.exists():
        return None
    d = np.load(p)
    return d["grid"], d["ssv2"]
