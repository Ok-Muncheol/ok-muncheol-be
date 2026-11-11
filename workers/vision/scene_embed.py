"""A: 장면 숫자화(CLIP 이미지 임베딩) + 작은 분류기로 장소 등 장면 칸을 맞힌다(2026-11-09 시험).

왜: VLM이 장소를 '느낌으로' 자신 있게 틀린다(T자→사거리, 차도와비차도→사거리). 장소 라벨은 데이터 폴더 기준이라 정확하므로
라벨 1,256건으로 '이 장면 숫자면 어떤 장소'를 배우게 한다(선형 프로브, 장면 분류에 흔히 쓰는 방법). CLIP은 학습하지 않는다.
- embed_video: 영상에서 FRAMES 프레임을 CLIP으로 숫자(512개)로 바꿔 저장해 두고 다시 쓴다
- predict_scene: 학습한 분류기(data/private/models/scene_<칸>.joblib)로 예측
학습: evals/vision/train_scene.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = "openai/clip-vit-base-patch16"
FRAMES = (15, 40, 60, 75)  # 장면을 대표하는 프레임(충돌 전, 충돌 무렵)
CACHE = ROOT / "evals/results/vision/clip_cache"
MODEL_DIR = ROOT / "data/private/models"
_CLIP = None


def _clip():
    global _CLIP
    if _CLIP is None:
        import os

        os.environ.setdefault("HF_HOME", str(ROOT / "data/private/models/hf"))
        import torch
        from transformers import CLIPModel, CLIPProcessor

        model = CLIPModel.from_pretrained(MODEL).to("cuda" if torch.cuda.is_available() else "cpu").eval()
        _CLIP = (model, CLIPProcessor.from_pretrained(MODEL))
    return _CLIP


def embed_video(video_path: str, name: str | None = None):
    """(len(FRAMES), 512) 배열. name이 있으면 CACHE/<name>.npy에 저장해 두고 다시 쓴다."""
    import cv2
    import numpy as np
    import torch
    from PIL import Image

    path = CACHE / f"{name}.npy" if name else None
    if path and path.exists():
        return np.load(path)
    model, proc = _clip()
    cap = cv2.VideoCapture(video_path)
    imgs = []
    for f in FRAMES:
        cap.set(cv2.CAP_PROP_POS_FRAMES, f - 1)
        ok, img = cap.read()
        imgs.append(Image.fromarray(img[:, :, ::-1]) if ok else Image.new("RGB", (224, 224)))
    with torch.no_grad():
        inputs = proc(images=imgs, return_tensors="pt").to(model.device)
        emb = model.get_image_features(**inputs)
        if not torch.is_tensor(emb):  # transformers 5: 출력 객체의 pooler_output이 이미지 임베딩
            emb = emb.pooler_output
        emb = emb / emb.norm(dim=-1, keepdim=True)
    out = emb.cpu().numpy().astype("float32")
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.save(path, out)
    return out


def scene_vector(emb):
    """분류기 입력: 프레임 평균 + 첫·마지막 프레임 차이(장면이 바뀐 정도)."""
    import numpy as np

    return np.concatenate([emb.mean(0), emb[-1] - emb[0]])


def predict_scene(field: str, emb) -> dict | None:
    import joblib

    path = MODEL_DIR / f"scene_{field}.joblib"
    if not path.exists():
        return None
    bundle = joblib.load(path)
    probs = bundle["model"].predict_proba([scene_vector(emb)])[0]
    classes = bundle["model"].classes_
    order = probs.argsort()[::-1]
    return {"value": str(classes[order[0]]), "confidence": round(float(probs[order[0]]), 2),
            "probs": {str(classes[i]): round(float(probs[i]), 2) for i in order[:3]}}
