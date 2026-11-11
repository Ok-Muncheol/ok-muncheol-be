"""A: 상대 차(A) 모습의 시간 변화를 숫자로(CLIP)(2026-11-09 시험). 움직임 분류기의 추가 특징.

왜: 박스(위치·크기)로는 차체가 어느 쪽을 보는지 알 수 없다. 좌·우회전은 차체 방향 변화로 드러난다.
움직임 A 분류기는 상대 차를 맞게 고른 영상(56%)과 잘못 고른 영상(59%)에서 정확도가 같았다 → 박스 숫자에 정보가 부족.
여기서는 고른 상대 차를 잘라 CLIP으로 숫자화하고, '차의 앞면/옆면/뒷면' 글과 닮은 정도도 함께 낸다.
(VLM에 잘라낸 사진을 보여 주는 확대 질문은 실패했다 → 이번엔 라벨로 학습)
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OFFSETS = (-40, -32, -24, -16, -8, 0)  # 충돌 순간 기준
CONTEXT = 1.4  # 박스 크기의 몇 배를 잘라낼지
CACHE = ROOT / "evals/results/vision/crop_cache"
VIEWS = ("a photo of the front of a car", "a photo of the side of a car", "a photo of the back of a car")
_TEXT = None


def _text_emb():
    global _TEXT
    if _TEXT is None:
        import torch

        from workers.vision.scene_embed import _clip

        model, proc = _clip()
        with torch.no_grad():
            t = model.get_text_features(**proc(text=list(VIEWS), return_tensors="pt", padding=True).to(model.device))
            if not torch.is_tensor(t):
                t = t.pooler_output
            _TEXT = (t / t.norm(dim=-1, keepdim=True)).cpu().numpy()
    return _TEXT


def crop_embeddings(video_path: str, track_a: list, anchor: int, name: str | None = None, tag: str = ""):
    """(6, 512) 배열. 상대 차가 없는 프레임은 nan. name이 있으면 CACHE/<name><tag>.npy에 저장해 두고 다시 쓴다."""
    import cv2
    import numpy as np
    import torch
    from PIL import Image

    path = CACHE / f"{name}{tag}.npy" if name else None
    if path and path.exists():
        return np.load(path)
    from workers.vision.scene_embed import _clip

    by_frame = {p[0]: p for p in track_a}
    cap = cv2.VideoCapture(video_path)
    imgs, rows = [], []
    for i, off in enumerate(OFFSETS):
        f = anchor + off
        near = [by_frame[g] for g in (f, f - 1, f + 1, f - 2, f + 2) if g in by_frame]
        if not near:
            continue
        _, x, y, w, h = near[0][:5]
        cap.set(cv2.CAP_PROP_POS_FRAMES, f - 1)
        ok, img = cap.read()
        if not ok:
            continue
        H, W = img.shape[:2]
        side = max(w, h) * CONTEXT
        cx, cy = x + w / 2, y + h / 2
        x0, y0 = int(max(0, cx - side / 2)), int(max(0, cy - side / 2))
        x1, y1 = int(min(W, cx + side / 2)), int(min(H, cy + side / 2))
        if x1 - x0 < 8 or y1 - y0 < 8:
            continue
        imgs.append(Image.fromarray(img[y0:y1, x0:x1, ::-1]))
        rows.append(i)
    out = np.full((len(OFFSETS), 512), np.nan, dtype="float32")
    if imgs:
        model, proc = _clip()
        with torch.no_grad():
            e = model.get_image_features(**proc(images=imgs, return_tensors="pt").to(model.device))
            if not torch.is_tensor(e):
                e = e.pooler_output
            e = (e / e.norm(dim=-1, keepdim=True)).cpu().numpy()
        out[rows] = e
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.save(path, out)
    return out


def view_scores(emb):
    """(6, 3): 프레임마다 앞면·옆면·뒷면 글과 닮은 정도(코사인). 없는 프레임은 nan."""
    return emb @ _text_emb().T
