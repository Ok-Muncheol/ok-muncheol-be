"""중복 영상 탐지.

AI Hub에는 같은 영상이 다른 이름·색감·잘림으로, 때로는 다른 라벨(사고 유형·과실)로 들어 있다.
449건을 눈으로 확인한 결과 같은 영상이 최대 6벌까지 있었다.

1단계(후보): 프레임 12장을 가운데 70%만 잘라 dHash(64비트)로 만들고, 시작 시점이 어긋난
   사본을 잡기 위해 ±3장까지 밀어 가며 겹치는 프레임의 평균 해밍 거리가 HASH_CANDIDATE 이하인 쌍.
2단계(확정): 후보 쌍을 ORB 특징점 매칭 + RANSAC 호모그래피로 비교해 정합점(inlier)이
   ORB_MIN 이상이면 중복. 해밍 거리 ≤ HASH_SURE 이면 ORB 없이 확정.
   (dHash만으로는 잘림·색보정 때문에 거리가 벌어져 놓친다.)
같은 그룹에서 사고 유형 코드가 다르면 정답을 믿을 수 없으므로 전부 `duplicate_conflict`,
같으면 하나만 남기고 나머지를 `duplicate_of`로 표시한다. evaluate.select_rows가 둘 다 뺀다.

    python -m evals.vision.dedupe --gt data/private/aihub/vision_gt.jsonl
"""

import argparse
import json
import sys
from pathlib import Path

HASH_FRAMES = 12
HASH_MAX_SHIFT = 3    # 12장 간격(약 0.9초)으로 ±3장 ≈ ±2.7초까지 시작 시점 차이를 허용
HASH_SURE = 6.0       # 프레임당 평균 다른 비트 수(0~64). 확인한 중복은 이 아래이거나 ORB로 잡힌다
HASH_CANDIDATE = 17.0
ORB_MIN = 60          # 두 기준 프레임 모두 이 이상이면 중복. 서로 다른 영상은 보통 50 미만
ORB_HIGH = 150        # 한 기준 프레임이라도 이 이상이면 중복(충돌 장면은 흔들려 한쪽이 안 맞는다)
ORB_ANCHORS = (30, 75)  # 클립 끝은 충돌로 화면이 깨지므로 앞쪽을 기준으로 삼는다
ORB_SEARCH = range(-45, 46, 5)  # 상대 영상에서 앞뒤 3초를 5프레임 간격으로 훑는다


def frame_hashes(video_path: Path) -> list[int]:
    import cv2

    cap = cv2.VideoCapture(str(video_path))
    frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    hashes = []
    for k in range(HASH_FRAMES):
        idx = round(k * (frames - 1) / (HASH_FRAMES - 1)) if frames else 0
        cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, idx))
        ok, frame = cap.read()
        if not ok:
            hashes.append(0)
            continue
        h, w = frame.shape[:2]
        crop = frame[int(h * 0.15):int(h * 0.85), int(w * 0.15):int(w * 0.85)]  # 테두리·자막 제거
        gray = cv2.cvtColor(cv2.resize(crop, (9, 8), interpolation=cv2.INTER_AREA),
                            cv2.COLOR_BGR2GRAY)
        bits = 0
        for row in range(8):
            for col in range(8):
                bits = (bits << 1) | int(gray[row, col] > gray[row, col + 1])
        hashes.append(bits)
    cap.release()
    return hashes


def distance(a: list[int], b: list[int], max_shift: int = HASH_MAX_SHIFT) -> float:
    """프레임당 평균 해밍 거리. 시작 시점 차이를 ±max_shift 프레임까지 밀어 보고 최소를 쓴다."""
    best = 64.0
    for shift in range(-max_shift, max_shift + 1):
        pairs = [(a[i], b[i + shift]) for i in range(len(a)) if 0 <= i + shift < len(b)]
        if len(pairs) < max(2, len(a) - max_shift):
            continue
        best = min(best, sum(bin(x ^ y).count("1") for x, y in pairs) / len(pairs))
    return best


class _OrbMatcher:
    def __init__(self) -> None:
        import cv2

        self.cv2 = cv2
        self.orb = cv2.ORB_create(1500)
        self.bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.cache: dict[tuple[str, int], tuple] = {}

    def features(self, path: str, idx: int) -> tuple:
        key = (path, idx)
        if key not in self.cache:
            cap = self.cv2.VideoCapture(path)
            cap.set(self.cv2.CAP_PROP_POS_FRAMES, idx)
            ok, frame = cap.read()
            cap.release()
            if not ok:
                self.cache[key] = (None, None)
            else:
                width = 640
                height = int(frame.shape[0] * width / frame.shape[1])
                frame = self.cv2.resize(frame, (width, height))
                gray = self.cv2.cvtColor(frame, self.cv2.COLOR_BGR2GRAY)
                self.cache[key] = self.orb.detectAndCompute(gray, None)
        return self.cache[key]

    def _match(self, fa: tuple, fb: tuple) -> int:
        import numpy as np

        k1, d1 = fa
        k2, d2 = fb
        if d1 is None or d2 is None:
            return 0
        pairs = [m for m in self.bf.knnMatch(d1, d2, k=2) if len(m) == 2]
        good = [m for m, n in pairs if m.distance < 0.75 * n.distance]
        if len(good) < 8:
            return len(good)
        src = np.float32([k1[g.queryIdx].pt for g in good])
        dst = np.float32([k2[g.trainIdx].pt for g in good])
        _, mask = self.cv2.findHomography(src, dst, self.cv2.RANSAC, 5.0)
        return int(mask.sum()) if mask is not None else 0

    def inliers(self, path_a: str, path_b: str) -> tuple[int, int]:
        """기준 프레임 2곳 각각에서 상대 영상의 앞뒤 프레임 중 가장 잘 맞는 정합점. (최소, 최대)."""
        per_anchor = []
        for idx in ORB_ANCHORS:
            fa = self.features(path_a, idx)
            per_anchor.append(max(self._match(fa, self.features(path_b, idx + off))
                                  for off in ORB_SEARCH if idx + off >= 0))
        return min(per_anchor), max(per_anchor)


def is_duplicate(inliers: tuple[int, int], orb_min: int = ORB_MIN,
                 orb_high: int = ORB_HIGH) -> bool:
    low, high = inliers
    return low >= orb_min or high >= orb_high


def group_duplicates(hashes: dict[str, list[int]], paths: dict[str, str] | None = None,
                     hash_sure: int = HASH_SURE, hash_candidate: int = HASH_CANDIDATE,
                     orb_min: int = ORB_MIN,
                     manual_pairs: list[tuple[str, str]] = ()) -> list[list[str]]:
    """중복 그룹(2건 이상)을 돌려준다. paths가 없으면 해시만으로(hash_sure) 판정한다.
    manual_pairs(사람이 신고한 쌍)는 무조건 같은 그룹으로 합친다."""
    ids = sorted(hashes)
    parent = {i: i for i in ids}
    matcher = _OrbMatcher() if paths else None

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in manual_pairs:
        if a in parent and b in parent:
            parent[find(a)] = find(b)

    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            d = distance(hashes[a], hashes[b])
            same = d <= hash_sure
            if not same and matcher and d <= hash_candidate:
                same = is_duplicate(matcher.inliers(paths[a], paths[b]), orb_min)
            if same:
                parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)
    return [sorted(g) for g in groups.values() if len(g) > 1]


def annotate(rows: list[dict], groups: list[list[str]]) -> tuple[int, int]:
    """행에 duplicate_group / duplicate_of / duplicate_conflict를 쓴다.

    (충돌 그룹 수, 중복 제거 건수)를 돌려준다."""
    by_id = {r["example_id"]: r for r in rows}
    for r in rows:
        r.pop("duplicate_group", None)
        r.pop("duplicate_of", None)
        r["duplicate_conflict"] = False
    conflicts = removed = 0
    for n, group in enumerate(groups):
        types = {by_id[i]["aihub"]["accident_type"] for i in group}
        for i in group:
            by_id[i]["duplicate_group"] = n
        if len(types) > 1:
            conflicts += 1
            for i in group:
                by_id[i]["duplicate_conflict"] = True
        else:
            for i in group[1:]:
                by_id[i]["duplicate_of"] = group[0]
                removed += 1
    return conflicts, removed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gt", type=Path, default=Path("data/private/aihub/vision_gt.jsonl"))
    parser.add_argument("--report", type=Path, default=Path("data/private/aihub/duplicates.json"))
    parser.add_argument("--manual", type=Path,
                        default=Path("data/private/aihub/manual_duplicates.jsonl"),
                        help="라벨 도구에서 신고한 중복 쌍(JSONL, {\"ids\": [a, b]})")
    args = parser.parse_args()
    manual = []
    if args.manual.exists():
        manual = [tuple(json.loads(line)["ids"]) for line in args.manual.read_text().splitlines()
                  if line.strip()]
    rows = [json.loads(line) for line in args.gt.read_text().splitlines() if line.strip()]
    with_video = [r for r in rows if r.get("video_path")]
    hashes = {r["example_id"]: frame_hashes(Path(r["video_path"])) for r in with_video}
    paths = {r["example_id"]: r["video_path"] for r in with_video}
    groups = group_duplicates(hashes, paths, manual_pairs=manual)
    conflicts, removed = annotate(rows, groups)
    args.gt.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    by_id = {r["example_id"]: r for r in rows}
    report = [{"ids": g, "accident_types": [by_id[i]["aihub"]["accident_type"] for i in g],
               "rates": [(by_id[i]["aihub"]["decoded"].get("rateA"),
                          by_id[i]["aihub"]["decoded"].get("rateB")) for i in g],
               "conflict": by_id[g[0]]["duplicate_conflict"]} for g in groups]
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"{len(hashes)}건 중 중복 그룹 {len(groups)}개(영상 {sum(len(g) for g in groups)}건): "
          f"라벨 충돌 {conflicts}그룹, 동일 라벨 중복 제거 {removed}건 → {args.report}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
