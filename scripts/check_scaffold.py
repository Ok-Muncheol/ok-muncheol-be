"""문법·import·타입 참조·JSON/견본 동기화 검사. 기능 테스트가 아니다."""

import ast
import importlib
import json
import sys
import typing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True


def main() -> None:
    sources = [p for folder in ("app", "workers", "evals", "scripts")
               for p in (ROOT / folder).rglob("*.py")]
    modules = 0
    for path in sources:
        ast.parse(path.read_text(), filename=str(path))
        if path.parts[-2] == "scripts":
            continue
        parts = list(path.relative_to(ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        module = importlib.import_module(".".join(parts))
        for value in vars(module).values():
            if isinstance(value, type) and typing.is_typeddict(value):
                typing.get_type_hints(value)
        modules += 1
    documents = []
    for folder in ("data/corpora", "evals/retrieval", "docs/contracts"):
        for path in (ROOT / folder).rglob("*"):
            if path.suffix == ".json":
                json.loads(path.read_text())
                documents.append(path)
            elif path.suffix == ".jsonl":
                for line in path.read_text().splitlines():
                    if line.strip():
                        json.loads(line)
                documents.append(path)
    from app.schemas.examples import contract_examples
    expected = contract_examples()
    for path in (ROOT / "docs/contracts/examples.json",):
        if path.exists() and json.loads(path.read_text()) != expected:
            raise SystemExit(
                f"Stale contract fixture: {path}. Run make contracts and refresh the FE fixture."
            )
    print(f"OK: {len(sources)} Python files, {modules} imports, {len(documents)} JSON/JSONL files")
    print("Scaffold checks only; server/models/DB/features are not implemented.")


if __name__ == "__main__":
    main()
