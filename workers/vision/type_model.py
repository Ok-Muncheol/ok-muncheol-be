"""사고 유형 × 방향 판단부(evals/vision/train_type_head.py로 학습)로 칸별 '진짜 자신감'을 낸다. 무료(GPU/CPU, 영상당 수십 ms).

입력: 영상 모델 v2·v3의 영상 요약(video_model.embed, 768 + 768) → 판단부 → 유형×방향 83개 확률
칸별 자신감 = 그 값을 가진 유형들의 확률 합 / 그 칸을 정하는 유형들의 확률 합(train_type_head.marginal과 같음)
쓰는 곳(2026-11-10): 움직임 B 확인 표시 — 자신감 < 0.9면 표시를 더한다(eval_b_typeflag.py, 사람 92건에서
표시 없이 틀림 12→7, 표시 16→24, 표시 없는 칸 정확도 84→90%). 칸 값으로는 안 쓴다(유형 값이 지금 값보다 못함).
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HEAD_PATH = ROOT / "data/private/models/type_head.pt"
FIELDS = ("location_type", "road_priority", "signal_A", "signal_B", "movement_A", "movement_B",
          "relative_position", "entry_order")

_HEAD = None


def _head():
    global _HEAD
    if _HEAD is None:
        import torch
        from torch import nn

        ck = torch.load(HEAD_PATH, map_location="cpu")
        k = len(ck["classes"])
        net = nn.Sequential(nn.Dropout(0.2), nn.Linear(1536, 512), nn.GELU(), nn.Dropout(0.3), nn.Linear(512, k))
        net.load_state_dict(ck["state"])
        _HEAD = (net.eval(), ck["classes"], ck["fields_of"])
    return _HEAD


def marginal(probs, classes, fields_of, field):
    mass, total = Counter(), 0.0
    for p, c in zip(probs, classes):
        v = fields_of.get(c, {}).get(field)
        if v:
            mass[v] += float(p)
            total += float(p)
    if not mass:
        return None, 0.0
    v, m = mass.most_common(1)[0]
    return v, m / total


def type_opinions(video_path: str, name: str | None = None) -> dict | None:
    """{"top5": [(유형|방향, 확률)], "fields": {칸: {"value", "confidence"}}}. 판단부·영상 모델이 없으면 None."""
    if not HEAD_PATH.exists():
        return None
    import numpy as np
    import torch

    from workers.vision.video_model import embed

    e2, e3 = embed(video_path, name, "v2"), embed(video_path, name, "v3")
    if e2 is None or e3 is None:
        return None
    net, classes, fields_of = _head()
    with torch.no_grad():
        probs = net(torch.tensor(np.concatenate([e2, e3]))[None]).softmax(-1)[0].numpy()
    fields = {}
    for f in FIELDS:
        v, c = marginal(probs, classes, fields_of, f)
        if v:
            fields[f] = {"value": v, "confidence": round(c, 3)}
    top = np.argsort(-probs)[:5]
    return {"top5": [(classes[i], round(float(probs[i]), 3)) for i in top], "fields": fields}
