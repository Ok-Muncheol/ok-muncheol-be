import argparse
import csv
import json
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 표에는 안내문 이름으로 저장하고, 실제 라벨은 오른쪽 이름으로 읽는다
ALIASES = {
    "accidental_negligence_rateA": "accident_negligence_rateA",
    "accidental_negligence_rateB": "accident_negligence_rateB",
    "accident_type": "traffic_accident_type",
}
MAP_FIELDS = [
    "사고대상",
    "사고장소",
    "장소특징",
    "A진행",
    "B진행",
    "A신호",
    "B신호",
    "기본과실A",
    "기본과실B",
]

FIELDS = [
    "video_name",
    "video_date",
    "filming_way",
    "video_point_of_view",
    "accidental_negligence_rateA",
    "accidental_negligence_rateB",
    "accident_type",
    "accident_object",
    "accident_place",
    "accident_place_feature",
    "vehicle_a_progress_info",
    "vehicle_b_progress_info",
    "damage_location",
    "related_laws",
    "violation_of_law",
    "additional_elements",
    "violation_factor",
    "weather",
]


def load_json(path):
    with path.open(encoding="utf-8-sig") as f:
        return json.load(f)


def find_frames(data):
    """이미지 라벨 파일 하나에서 프레임 목록을 꺼낸다."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    if "objects" in data or "video_file_name" in data:
        return [data]
    for value in data.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            if "objects" in value[0] or "video_file_name" in value[0]:
                return value
    return []


def iter_image_jsons(root):
    """(경로, 읽기 함수)를 낸다. 풀어 둔 JSON과 zip 안의 JSON을 모두 읽는다.
    zip에서 바로 읽는 쪽이 작은 파일 수십만 개를 여는 것보다 훨씬 빠르다."""
    for path in root.rglob("*.json"):
        yield path, lambda p=path: load_json(p)
    for zpath in root.rglob("*.zip"):
        if "_이미지_" not in zpath.name:
            continue
        with zipfile.ZipFile(zpath) as zf:
            for name in zf.namelist():
                if name.endswith(".json"):
                    yield Path(name), lambda n=name: json.loads(zf.read(n).decode("utf-8-sig"))


def iter_video_labels(root):
    """영상 라벨 [(표시 이름, 파일 이름, 읽기 함수)]. 풀어 둔 JSON과 zip 안의 JSON(_영상_ zip)을
    모두 읽는다.
    Training 라벨은 zip으로만 있어서 필요하다(2026-11-10)."""
    items = [(str(p), p, lambda p=p: load_json(p)) for p in sorted(root.rglob("*.json"))]
    for zpath in sorted(root.rglob("*.zip")):
        if "_영상_" not in zpath.name:
            continue
        zf = zipfile.ZipFile(zpath)  # 다 읽을 때까지 열어 둔다
        for name in sorted(zf.namelist()):
            if name.endswith(".json"):
                items.append(
                    (
                        f"{zpath}::{name}",
                        Path(name),
                        lambda n=name, zf=zf: json.loads(zf.read(n).decode("utf-8-sig")),
                    )
                )
    return items


def scan_images(root):
    stats = defaultdict(lambda: {"frames": 0, "a_boxes": 0, "b_boxes": 0, "ab_frames": set()})
    files = 0
    unread = 0
    for path, read in iter_image_jsons(root):
        files += 1
        try:
            frames = find_frames(read())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            unread += 1
            continue
        for frame in frames:
            name = frame.get("video_file_name") or path.parent.name
            stem = Path(str(name)).stem
            stats[stem]["frames"] += 1
            objects = frame.get("objects") or []
            if isinstance(objects, dict):
                objects = list(objects.values())
            for obj in objects:
                if isinstance(obj, dict):
                    stats[stem]["a_boxes"] += bool(obj.get("isObjectA"))
                    stats[stem]["b_boxes"] += bool(obj.get("isObjectB"))
                    if obj.get("isObjectA") or obj.get("isObjectB"):
                        stats[stem]["ab_frames"].add(frame.get("sequence_frame_number"))
    return stats, files, unread


def scan_videos(root):
    """mp4 이름 → 위치. 풀어 둔 mp4와 zip 안의 mp4를 모두 찾는다(zip은 풀지 않음)."""
    found = {}
    for p in root.rglob("*.mp4"):
        found[p.stem] = str(p)
    for z in root.rglob("*.zip"):
        with zipfile.ZipFile(z) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".mp4"):
                    found.setdefault(Path(name).stem, f"{z}::{name}")
    return found


def load_type_map(path):
    with path.open(encoding="utf-8-sig") as f:
        return {row["accident_type"]: row for row in csv.DictReader(f)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--video-labels", required=True, type=Path)
    parser.add_argument("--image-labels", required=True, type=Path)
    parser.add_argument("--videos", required=True, type=Path)
    parser.add_argument("--type-map", default=HERE / "accident_type_map.csv", type=Path)
    parser.add_argument("--out", default=Path("evals/results/vision/labels.csv"), type=Path)
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    type_map = load_type_map(args.type_map)

    video_labels = iter_video_labels(args.video_labels)
    if not video_labels:
        raise SystemExit(f"영상 라벨 JSON을 찾지 못했습니다: {args.video_labels}")

    first = video_labels[0][2]()
    print("첫 영상 라벨:", video_labels[0][0])
    print(json.dumps(first, ensure_ascii=False, indent=2)[:1500])
    first_video = first.get("video", first)
    missing = [k for k in FIELDS if ALIASES.get(k, k) not in first_video]
    if missing:
        print("주의: 첫 라벨에 없는 칸:", missing)

    mp4_paths = scan_videos(args.videos)
    mp4_stems = set(mp4_paths)
    image_stats, image_files, unread = scan_images(args.image_labels)

    rows = []
    for label_name, path, read in video_labels:
        try:
            data = read()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            print("읽기 실패:", label_name)
            continue
        video = data.get("video", data) if isinstance(data, dict) else {}
        row = {key: video.get(ALIASES.get(key, key), video.get(key, "")) for key in FIELDS}
        stem = Path(str(row["video_name"]) or path.stem).stem
        stat = image_stats.get(stem, {"frames": 0, "a_boxes": 0, "b_boxes": 0, "ab_frames": set()})
        mapped = type_map.get(str(row["accident_type"]), {})
        row.update({f"map_{k}": mapped.get(k, "") for k in MAP_FIELDS})
        row.update(
            {
                "label_file": label_name,
                "has_mp4": stem in mp4_stems,
                "mp4_path": mp4_paths.get(stem, ""),
                "frames": stat["frames"],
                "a_boxes": stat["a_boxes"],
                "b_boxes": stat["b_boxes"],
                # A나 B 박스가 있는 프레임 번호 (세미콜론으로 구분)
                "ab_frames": ";".join(
                    str(n) for n in sorted(x for x in stat["ab_frames"] if x is not None)
                ),
            }
        )
        #  122건은 과실이 accident_negligence_rate 한 칸뿐이다. 값은 A의 과실(유형표 기본과실 A와
        # 121/121 일치)
        if row["accidental_negligence_rateA"] == "" and isinstance(
            video.get("accident_negligence_rate"), int
        ):
            row["accidental_negligence_rateA"] = video["accident_negligence_rate"]
            row["accidental_negligence_rateB"] = 100 - video["accident_negligence_rate"]
        rows.append(row)

    with args.out.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    print("\n영상 라벨:", len(rows))
    print("mp4 파일:", len(mp4_stems))
    print("이미지 라벨 파일:", image_files, "| 읽기 실패:", unread)
    print("라벨과 mp4 연결:", sum(r["has_mp4"] for r in rows))

    # 1인칭은 내 차(B)가 화면에 없을 수 있어 A나 B 중 하나만 있어도 인정한다
    def usable(r):
        if str(r["video_point_of_view"]) == "1":
            return r["a_boxes"] > 0 or r["b_boxes"] > 0
        return r["a_boxes"] > 0 and r["b_boxes"] > 0

    print("정답 박스까지 있음:", sum(r["has_mp4"] and usable(r) for r in rows))
    for key in [
        "filming_way",
        "video_point_of_view",
        "accident_place",
        "weather",
        "map_사고대상",
        "map_사고장소",
    ]:
        print(key, Counter(str(r[key]) for r in rows).most_common(10))
    types = Counter(str(r["accident_type"]) for r in rows)
    print("사고 유형 종류:", len(types), "| 상위:", types.most_common(10))
    unmapped = sorted({t for t in types if t not in type_map}, key=lambda t: (len(t), t))
    print("유형표에 없는 유형:", unmapped or "없음")
    print("과실비율 빈칸:", sum(str(r["accidental_negligence_rateA"]) == "" for r in rows))
    print("\n저장:", args.out)


if __name__ == "__main__":
    main()
