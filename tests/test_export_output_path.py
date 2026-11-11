"""DB 적재 행 내보내기 경로가 D 로더가 읽는 경로와 같은지."""

import importlib.util
from pathlib import Path

from app.retrieval.loader import DB_IMPORT

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "export_corpus_index_rows.py"


def test_export_default_output_is_loader_path():
    spec = importlib.util.spec_from_file_location("export_corpus_index_rows", _PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # 다르면 새로 내보낸 파일을 로더가 못 읽고 실데이터 테스트가 조용히 skip된다
    assert module.DB_IMPORT.resolve() == DB_IMPORT.resolve()
