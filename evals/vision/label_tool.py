"""사람 라벨링용 로컬 웹 도구.

영상을 보며 촬영 차량이 도표의 A인지 B인지, 두 차의 진행(의도)을 고른다.

    python -m evals.vision.label_tool --gt data/private/aihub/vision_gt.jsonl \
        --ids-from evals/results/exp_loc_f10_w768.jsonl \
        --out data/private/aihub/human_labels.jsonl

브라우저에서 http://127.0.0.1:8765 를 연다. 표준 라이브러리만 쓴다(추가 의존성 없음).
저장 형식(JSONL, example_id로 덮어씀):
    {"example_id", "ego_party": "A"|"B"|"unknown", "movement_ego", "movement_other",
     "note", "labeled_at"}
"""

import argparse
import json
import os
import sys
import threading
from datetime import UTC, datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

MOVEMENTS = ("straight", "left_turn", "right_turn", "u_turn")
WEB_WIDTH = 1280
_transcode_lock = threading.Lock()


def web_video(src: Path, cache_dir: Path) -> Path:
    """AI Hub 원본은 MPEG-4 Part 2(FMP4)라 브라우저가 못 튼다. 첫 요청 때 H.264로 바꿔 캐시한다."""
    dst = cache_dir / src.name
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    import cv2

    with _transcode_lock:
        if dst.exists() and dst.stat().st_size > 0:
            return dst
        cache_dir.mkdir(parents=True, exist_ok=True)
        cap = cv2.VideoCapture(str(src))
        fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
        ok, frame = cap.read()
        if not ok:
            raise RuntimeError(f"영상을 열 수 없음: {src}")
        scale = min(1.0, WEB_WIDTH / frame.shape[1])
        size = (int(frame.shape[1] * scale), int(frame.shape[0] * scale))
        tmp = dst.with_suffix(".tmp.mp4")
        writer = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"avc1"), fps, size)
        if not writer.isOpened():
            raise RuntimeError("OpenCV에 H.264(avc1) 인코더가 없음")
        while ok:
            writer.write(cv2.resize(frame, size) if scale < 1.0 else frame)
            ok, frame = cap.read()
        writer.release()
        cap.release()
        os.replace(tmp, dst)
    return dst

PAGE = (Path(__file__).with_suffix(".html")).read_text()


def load_labels(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return {row["example_id"]: row for row in rows}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, examples: list[dict], out: Path, web_cache: Path, **kwargs):
        self.examples = examples
        self.by_id = {e["example_id"]: e for e in examples}
        self.out = out
        self.web_cache = web_cache
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):  # noqa: D102 - 영상 range 요청 로그가 너무 많다
        return

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - http.server 규약
        path = urlparse(self.path).path
        if path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/examples":
            self._json(self.examples)
        elif path == "/api/labels":
            self._json(load_labels(self.out))
        elif path.startswith("/video/"):
            self._video(path.removeprefix("/video/"))
        else:
            self.send_error(404)

    def _video(self, example_id: str) -> None:
        example = self.by_id.get(example_id)
        if not example:
            self.send_error(404)
            return
        try:
            file = web_video(Path(example["video_path"]), self.web_cache)
        except RuntimeError as exc:
            self.send_error(500, str(exc))
            return
        size = file.stat().st_size
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else max(0, size - int(b))
            end = int(b) if b and a else size - 1
            end = min(end, size - 1)
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with file.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return  # 브라우저가 다른 구간을 요청하며 끊은 것
                remaining -= len(chunk)

    def _report_duplicate(self, row: dict) -> None:
        a, b = row.get("example_id"), row.get("duplicate_of")
        if a not in self.by_id or b not in self.by_id or a == b:
            self._json({"error": "bad pair"}, 400)
            return
        _append_manual_duplicate(a, b)
        self._json({"ok": True})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        row = json.loads(self.rfile.read(length))
        if path == "/api/duplicate":
            self._report_duplicate(row)
            return
        if path != "/api/label":
            self.send_error(404)
            return
        if row.get("example_id") not in self.by_id or row.get("ego_party") not in ("A", "B",
                                                                                     "unknown"):
            self._json({"error": "bad label"}, 400)
            return
        for key in ("movement_ego", "movement_other"):
            if row.get(key) is not None and row[key] not in MOVEMENTS:
                self._json({"error": f"bad {key}"}, 400)
                return
        saved = {
            "example_id": row["example_id"], "ego_party": row["ego_party"],
            "movement_ego": row.get("movement_ego"), "movement_other": row.get("movement_other"),
            "note": (row.get("note") or "").strip(),
            "labeled_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        labels = load_labels(self.out)
        labels[saved["example_id"]] = saved
        self.out.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.out.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in labels.values()))
        os.replace(tmp, self.out)
        self._json({"ok": True, "count": len(labels)})


MANUAL_DUPLICATES = Path("data/private/aihub/manual_duplicates.jsonl")


def _append_manual_duplicate(a: str, b: str, path: Path = MANUAL_DUPLICATES) -> None:
    """사람이 신고한 중복 쌍. dedupe.py가 읽어 그룹에 합친다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"ids": sorted([a, b]),
                            "reported_at": datetime.now(UTC).isoformat(timespec="seconds")},
                           ensure_ascii=False) + "\n")


def select_examples(gt: Path, ids_from: Path | None, limit: int,
                    min_sharpness: float | None = None, simple_scope: bool = False) -> list[dict]:
    from evals.vision.evaluate import select_rows

    rows = [json.loads(line) for line in gt.read_text().splitlines() if line.strip()]
    rows = [r for r in rows if r["video_path"]]
    rows = select_rows(rows, mvp_only=True, min_sharpness=min_sharpness, simple_scope=simple_scope)
    if ids_from:
        order = [json.loads(line)["example_id"] for line in ids_from.read_text().splitlines()
                 if line.strip()]
        by_id = {r["example_id"]: r for r in rows}
        rows = [by_id[i] for i in order if i in by_id]
    return rows[:limit]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt", type=Path, default=Path("data/private/aihub/vision_gt.jsonl"))
    parser.add_argument("--ids-from", type=Path, default=None,
                        help="이 JSONL의 example_id 순서대로 보여준다(실험 100건과 맞추기)")
    parser.add_argument("--out", type=Path, default=Path("data/private/aihub/human_labels.jsonl"))
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--web-cache", type=Path, default=Path("data/private/aihub/web"),
                        help="브라우저용 H.264 변환본 캐시 디렉터리")
    parser.add_argument("--min-sharpness", type=float, default=None, help="화질 게이트 통과분만")
    parser.add_argument("--simple-scope", action="store_true", help="유턴·우회전끼리 유형 제외")
    args = parser.parse_args()
    examples = select_examples(args.gt, args.ids_from, args.limit, args.min_sharpness,
                               args.simple_scope)
    handler = partial(Handler, examples=examples, out=args.out, web_cache=args.web_cache)

    def pretranscode() -> None:  # 보는 동안 뒤에서 미리 변환해 둔다
        for e in examples:
            try:
                web_video(Path(e["video_path"]), args.web_cache)
            except RuntimeError as exc:
                print(exc, file=sys.stderr)

    threading.Thread(target=pretranscode, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"{len(examples)}건 → http://127.0.0.1:{args.port}  (저장: {args.out})", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
