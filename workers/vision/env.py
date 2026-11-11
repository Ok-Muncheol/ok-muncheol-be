"""A: 비전 워커용 .env 읽기. 키 값은 출력하지 않는다.

찾는 순서: workers/vision/.env → 저장소 루트 .env. 이미 환경 변수에 있는 값은 덮어쓰지 않는다.
.env는 .gitignore에 있어 커밋되지 않는다.
"""

import os
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_PATHS = (_HERE / ".env", _HERE.parents[1] / ".env")


def load_env() -> None:
    for path in _PATHS:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip().removeprefix("export ").strip()
            value = value.strip().strip('"').strip("'")
            if key and value and key not in os.environ:
                os.environ[key] = value
