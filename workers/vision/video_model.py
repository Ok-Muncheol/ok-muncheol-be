"""A: 영상 모델(VideoMAE)로 장소·움직임 A·B를 직접 맞힌다(2026-11-09 시험). 학습:
evals/vision/train_video.py

왜: 사람이 고른 숫자(박스 위치·크기, 카메라 흐름)로 학습한 분류기는 상대 차 움직임을 못 배웠다(사람
정답에서 전부 직진과 같음).
영상 자체(차의 모습, 움직임, 도로 모양)를 보고 배우는 모델을 AI Hub 라벨로 학습한다. 평가는 사람
정답으로만.
- 입력: 프레임 30~110(충돌 앞뒤 약 5초)에서 16장, 224×224(가로를 눌러 화면 전체를 담음). 좌우 뒤집기
  금지(왼쪽·오른쪽 뜻이 바뀜)
- 출력: 장소(8), 움직임 B(4: 직진·좌회전·우회전·기타), 움직임 A(4)
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKBONE = "MCG-NJU/videomae-base-finetuned-kinetics"
FRAMES = [round(30 + i * (110 - 30) / 15) for i in range(16)]
SIZE = 224
CACHE = ROOT / "evals/results/vision/clip16"
#  2026-11-10 Training 6,438건 + Validation으로 재학습(--balance, 3회차). 사람 정답 92건: 장소 74%,
# B 80%, A 62%
# 11/9 모델(video_model.pt, 1,073건: 장소 43%, B 61%, A 53%)은 지우지 않고 남겨 둠
MODEL_PATH = ROOT / "data/private/models/video_model_tr.pt"  # MODEL_SPEC과 함께 바꾼다(MODEL_FILES)
PLACES = [
    "T자형교차로",
    "고속도로",
    "사거리_신호없음",
    "사거리_신호있음",
    "주차장",
    "직선도로",
    "차도와비차도",
    "회전교차로",
]
MOVES = ["직진", "좌회전", "우회전", "기타"]
#  v3(2026-11-10): 움직임에 진로변경·추월·주정차·출차를 따로 가르치고(정답 카드 A의 40%가 직진·좌·우
# 밖),
#  상대 위치·도로 관계를 더한다. 상대 위치 라벨(AI Hub 사고 유형)에는 왼쪽·오른쪽이 없어 빈칸을
# '옆'으로 배운다.
MOVES_V3 = ["직진", "좌회전", "우회전", "진로변경", "추월", "주정차", "출차", "기타"]
RELPOS = ["같은방향_앞", "같은방향_뒤", "맞은편", "옆"]
ROADPRI = ["해당없음", "동일폭", "A대로", "B대로"]
SPECS = {
    "v2": {"location_type": PLACES, "movement_B": MOVES, "movement_A": MOVES},
    "v3": {
        "location_type": PLACES,
        "movement_B": MOVES_V3,
        "movement_A": MOVES_V3,
        "relative_position": RELPOS,
        "road_priority": ROADPRI,
    },
}
#  v4(2026-11-10): v3와 같은 칸, 전체 11,275건(빠졌던 직선도로 4,837건 추가: 진로변경 578→1,140,
# 추월 127→334)
SPECS["v4"] = SPECS["v3"]
#  j3(2026-11-10): v3와 같은 칸·데이터, 눈만 V-JEPA 2(ViT-L, SSv2). 뒤 12개 층 + 요약부만 학습(8GB
# GPU: 4건씩 0.74초,
# 4.8GB)
#  눈 고정 시험(vjepa_feat.py + 판단부)은 모든 칸에서 VideoMAE보다 못함 → 우리 영상으로 눈을 학습해
# 본다
SPECS["j3"] = SPECS["v3"]
VJEPA_ID = "facebook/vjepa2-vitl-fpc16-256-ssv2"
VJEPA_TRAIN_LAYERS = 12
MODEL_FILES = {
    "v2": "video_model_tr.pt",
    "v3": "video_model_v3.pt",
    "v4": "video_model_v4.pt",
    "j3": "video_model_j3.pt",
}
MODEL_SPEC = "v2"  # 카드가 쓰는 모델. v3는 30건 평가 후 바꾼다


def label_class(spec: str, field: str, value) -> str | None:
    """정답 값 → 이 모델이 배우는 답. 배울 수 없으면 None(학습·평가에서 뺌)."""
    classes = SPECS[spec][field]
    if field == "relative_position":
        if value in ("왼쪽", "오른쪽"):
            return "옆"
        return value if value in classes else ("옆" if not value else None)
    if not value:
        return None
    if field.startswith("movement_"):
        return value if value in classes else "기타"
    return value if value in classes else None


MEAN = (0.485, 0.456, 0.406)
STD = (0.229, 0.224, 0.225)


def load_clip(video_path: str, name: str | None = None):
    """(16, 224, 224, 3) uint8. name이 있으면 CACHE/<name>.npy에 저장해 두고 다시 쓴다."""
    import cv2
    import numpy as np

    path = CACHE / f"{name}.npy" if name else None
    if path and path.exists():
        return np.load(path)
    cap = cv2.VideoCapture(video_path)
    out = []
    last = np.zeros((SIZE, SIZE, 3), np.uint8)
    for f in FRAMES:
        cap.set(cv2.CAP_PROP_POS_FRAMES, f - 1)
        ok, img = cap.read()
        if ok:
            last = cv2.resize(img, (SIZE, SIZE))[:, :, ::-1]
        out.append(last)
    arr = np.stack(out).astype(np.uint8)
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        np.save(path, arr)
    return arr


def to_tensor(clips):
    """uint8 (B,16,H,W,3) → float (B,16,3,H,W) 정규화."""
    import torch

    x = torch.as_tensor(clips).float().div_(255)
    mean = torch.tensor(MEAN).view(1, 1, 1, 1, 3)
    std = torch.tensor(STD).view(1, 1, 1, 1, 3)
    return ((x - mean) / std).permute(0, 1, 4, 2, 3).contiguous()


def build_model(spec: str = "v2"):
    import os

    from torch import nn

    os.environ.setdefault("HF_HOME", str(ROOT / "data/private/models/hf"))
    if spec.startswith("j"):
        return _build_vjepa(spec)
    from transformers import VideoMAEModel

    class Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.backbone = VideoMAEModel.from_pretrained(BACKBONE)
            d = self.backbone.config.hidden_size
            self.norm = nn.LayerNorm(d)
            self.heads = nn.ModuleDict({f: nn.Linear(d, len(c)) for f, c in SPECS[spec].items()})
            self.drop = nn.Dropout(0.2)

        def forward(self, x):
            h = self.backbone(pixel_values=x).last_hidden_state.mean(1)
            h = self.drop(self.norm(h))
            return {k: head(h) for k, head in self.heads.items()}

    return Net()


def _build_vjepa(spec: str):
    """V-JEPA 2 눈 + 칸별 판단부. 앞 12개 층은 고정, 뒤 12개 층과 요약부(어텐션 풀링)만 학습한다.
    메모리 아끼기(gradient checkpointing)는 use_reentrant=False여야 한다: True면 앞층이 고정일 때
    뒤층에 학습 신호가 안 간다(2026-11-10 확인: 마지막 층 기울기 None)."""
    from torch import nn
    from transformers import VJEPA2ForVideoClassification

    class JNet(nn.Module):
        def __init__(self):
            super().__init__()
            self.backbone = VJEPA2ForVideoClassification.from_pretrained(VJEPA_ID)
            self.backbone.classifier = nn.Identity()  # SSv2 답(174종)은 안 씀
            for p in self.backbone.parameters():
                p.requires_grad = False
            for layer in self.backbone.vjepa2.encoder.layer[-VJEPA_TRAIN_LAYERS:]:
                for p in layer.parameters():
                    p.requires_grad = True
            for p in self.backbone.pooler.parameters():
                p.requires_grad = True
            self.backbone.gradient_checkpointing_enable(
                gradient_checkpointing_kwargs={"use_reentrant": False}
            )
            d = self.backbone.config.hidden_size
            self.norm = nn.LayerNorm(d)
            self.heads = nn.ModuleDict({f: nn.Linear(d, len(c)) for f, c in SPECS[spec].items()})
            self.drop = nn.Dropout(0.2)

        def forward(
            self, x
        ):  # x (B, 16, 3, 224, 224): 224 그대로 넣는다(위치 정보가 RoPE라 크기를 받아 줌)
            h = self.backbone.vjepa2(pixel_values_videos=x, skip_predictor=True).last_hidden_state
            h = self.drop(self.norm(self.backbone.pooler(h)))
            return {k: head(h) for k, head in self.heads.items()}

    return JNet()


_NETS: dict = {}


def _load(spec: str):
    """학습한 모델(캐시). 파일이 없으면 None."""
    import torch

    path = MODEL_PATH if spec == MODEL_SPEC else MODEL_PATH.parent / MODEL_FILES[spec]
    if not path.exists():
        return None
    if spec not in _NETS:
        net = build_model(spec)
        net.load_state_dict(torch.load(path, map_location="cpu"))
        _NETS[spec] = net.cuda().eval() if torch.cuda.is_available() else net.eval()
    return _NETS[spec]


def embed(video_path: str, name: str | None = None, spec: str = "v2"):
    """VideoMAE 모델의 영상 요약(norm 뒤 768개, numpy). 사고 유형 판단부
    입력(evals/vision/train_type_head.py와 같음)."""
    import torch

    net = _load(spec)
    if net is None or spec.startswith("j"):
        return None
    x = to_tensor(load_clip(video_path, name)[None]).to(next(net.parameters()).device)
    with torch.no_grad(), torch.autocast("cuda", dtype=torch.float16, enabled=x.is_cuda):
        h = net.norm(net.backbone(pixel_values=x).last_hidden_state.mean(1))
    return h.float()[0].cpu().numpy()


def predict(video_path: str, name: str | None = None, spec: str | None = None) -> dict | None:
    """{칸: {"value", "confidence", "probs"}}. 학습한 모델이 없으면 None.

    spec: None이면 MODEL_SPEC(카드 값을 정하는 모델). "v3"는 상대 위치·도로 관계 확인
    표시용(2026-11-10)
    """
    import torch

    spec = spec or MODEL_SPEC
    net = _load(spec)
    if net is None:
        return None
    x = to_tensor(load_clip(video_path, name)[None]).to(next(net.parameters()).device)
    with torch.no_grad(), torch.autocast("cuda", dtype=torch.float16, enabled=x.is_cuda):
        out = {k: v.float().softmax(-1)[0].cpu() for k, v in net(x).items()}
    res = {}
    for k, p in out.items():
        names = SPECS[spec][k]
        order = p.argsort(descending=True)
        res[k] = {
            "value": names[order[0]],
            "confidence": round(float(p[order[0]]), 2),
            "probs": {names[i]: round(float(p[i]), 2) for i in order[:3]},
        }
    return res
