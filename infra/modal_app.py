"""B(인프라): 영상 분석 Modal 앱.

배포는 저장소 루트에서 `modal deploy infra/modal_app.py`(CLI 로그인 뒤).

- `analyze`(웹 엔드포인트, 프록시 인증): BE `app/modal_dispatch.py`가 job을 POST하면 모양만 확인하고
  `run_job`을 spawn한 뒤 `{"call_id"}`를 바로 돌려준다. 주소·프록시 인증 토큰을 Vercel BE에
  `MODAL_ANALYZE_URL`·`MODAL_PROXY_KEY`·`MODAL_PROXY_SECRET`로 넣는다.
- `run_job`(GPU T4): `workers/modal_worker.py`가 영상 받기 → 미리보기 → 분석 → 근거 프레임 → 콜백.
- Modal에는 Supabase·DB 키를 두지 않는다. 시크릿 `ok-muncheol-vision`에는 VLM용 `OPENAI_API_KEY`만
  넣는다(Modal 전용 키, 사용 한도를 건다).
- 모델 파일 8개는 볼륨 `ok-muncheol-models`의 맨 위에 올리고 `/root/data/private/models`에 붙인다
  (`workers/vision/pipeline.py`의 MODELS). Hugging Face 본체 3개는 이미지를 만들 때 받아 둔다.
- 비용: 쓴 초만큼만 과금하고 요청이 없으면 컨테이너가 꺼진다(scale to zero). 대기 60초 뒤 끄고,
  GPU 컨테이너는 하나만 띄운다(동시 작업은 줄을 선다). 자세한 절차는 `infra/README.md`.

이 파일은 `modal` 패키지가 있어야 import된다. 그래서 `scripts/check_scaffold.py`가 보는
app·workers·evals 밖(`infra/`)에 두고, 처리 로직은 `workers/modal_worker.py`(테스트 대상)에 둔다.
"""

from pathlib import Path

import modal

REPO = Path(__file__).resolve().parents[1]
APP_NAME = "ok-muncheol-vision"
MODELS_PATH = "/root/data/private/models"
HF_HOME = "/root/hf"
HF_MODELS = (
    "MCG-NJU/videomae-base-finetuned-kinetics",
    "facebook/vjepa2-vitl-fpc16-256-ssv2",
    "openai/clip-vit-base-patch16",
)

app = modal.App(APP_NAME)
models = modal.Volume.from_name("ok-muncheol-models", create_if_missing=True)
vision_secret = modal.Secret.from_name("ok-muncheol-vision")


def _download_hf_models() -> None:
    from huggingface_hub import snapshot_download

    for repo_id in HF_MODELS:
        snapshot_download(repo_id)


worker_image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "libgl1", "libglib2.0-0")
    .pip_install_from_pyproject(str(REPO / "pyproject.toml"), optional_dependencies=["vision-eval"])
    .env({"HF_HOME": HF_HOME, "PYTHONPATH": "/root"})
    .run_function(_download_hf_models)
    .add_local_dir(REPO / "workers", remote_path="/root/workers", ignore=["**/__pycache__"])
    .add_local_file(
        REPO / "evals/vision/accident_type_map.csv",
        remote_path="/root/evals/vision/accident_type_map.csv",
    )
    # workers/vision/fact_card.py가 BE 계약 스키마(app.schemas.facts, typing만 씀)를 import한다.
    # app/의 나머지(API·DB·에이전트)는 넣지 않는다(tests/test_modal_worker.py가 import를 지킨다)
    .add_local_file(REPO / "app/__init__.py", remote_path="/root/app/__init__.py")
    .add_local_dir(REPO / "app/schemas", remote_path="/root/app/schemas", ignore=["**/__pycache__"])
)

web_image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install("fastapi[standard]", "httpx")
    .env({"PYTHONPATH": "/root"})
    .add_local_dir(REPO / "workers", remote_path="/root/workers", ignore=["**/__pycache__"])
)


@app.function(
    image=worker_image,
    gpu="T4",
    volumes={MODELS_PATH: models},
    secrets=[vision_secret],
    timeout=900,
    scaledown_window=60,
    max_containers=1,
)
def run_job(job: dict) -> dict:
    import logging

    from workers.modal_worker import run_job as process

    logging.basicConfig(level=logging.INFO)
    # httpx는 INFO에서 요청 주소를 그대로 찍는다(서명 URL의 token 포함) → 경고 이상만 남긴다
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return process(job)


@app.function(image=web_image, scaledown_window=60, max_containers=2)
@modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
def analyze(job: dict) -> dict:
    from fastapi import HTTPException

    from workers.modal_worker import JobInvalid, validate_job

    try:
        validate_job(job)
    except JobInvalid as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    call = run_job.spawn(job)
    return {"call_id": call.object_id}
