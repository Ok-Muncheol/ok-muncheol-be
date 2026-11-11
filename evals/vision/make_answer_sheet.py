"""정답 카드 입력표(엑셀)와 A/B 참고 사진을 만든다.

사용:
    .venv/Scripts/python evals/vision/make_answer_sheet.py              # 30건 →
    answers/answer_cards.xlsx
    .venv/Scripts/python evals/vision/make_answer_sheet.py --split dev  # 개선용만

- AI Hub 라벨로 알 수 있는 칸은 미리 채우고 노란색으로 칠한다(영상과 다르면 고친다).
- 참고 사진: 충돌 직전 정답 박스 프레임에 A(빨강)·B(파랑)를 그린다. 3인칭에서 A/B를 구분할 때 쓴다.
- 이미 있는 입력표는 덮어쓰지 않는다(사람이 채운 내용을 지키기 위해). 다시 만들려면 --force.
"""

import argparse
import csv
import json
import sys
import zipfile
from pathlib import Path

#  OpenCV·openpyxl은 비전 환경(.venv)에만 있으므로 함수 안에서 불러온다(레포 검사가 모든 모듈을
# import함)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# 유형 → 칸 변환 규칙은 서비스 코드와 같이 쓴다
from workers.vision.accident_types import (  # noqa: E402,F401
    INTERSECTIONS,
    MOVE_WORDS,
    PLACE_MAP,
    SIGNAL_MAP,
    movement,
    signal,
    type_fields,
)
from workers.vision.fields import FIELDS  # noqa: E402

REF_COLS = [
    "video_name",
    "split",
    "시점",
    "영상",
    "참고사진",
    "라벨_장소특징",
    "라벨_A진행",
    "라벨_B진행",
    "라벨_과실A:B",
]
SHEET_FIELDS = [(n, v, r, d) for n, v, r, d in FIELDS if n != "narrative"]
STRIP_FRAMES = list(range(51, 99))  # 참고 사진 아래쪽에 붙일 충돌 근처 프레임


def label_swapped(r) -> bool:
    """1인칭인데 라벨 박스가 B에만 있으면, 화면의 상대 차가 라벨 B이고 블랙박스 차가 라벨 A다.

    데이터 설명서는 '1인칭은 본인=B'라고 하지만 1인칭 1,595건 중 987건이 이 경우다(2026-11-05 확인).
    입력표·카드는 '블랙박스 차=B'로 통일하므로 이때 라벨의 A/B를 맞바꿔 읽는다.
    """
    return r["video_point_of_view"] == "1" and int(r["b_boxes"]) > 0 and int(r["a_boxes"]) == 0


def label_view(r) -> dict:
    """입력표 기준(블랙박스 차=B)으로 맞춘 라벨 값: 진행·신호·과실."""
    keys = [
        ("map_A진행", "map_B진행"),
        ("map_A신호", "map_B신호"),
        ("accidental_negligence_rateA", "accidental_negligence_rateB"),
        ("map_기본과실A", "map_기본과실B"),
    ]
    out = dict(r)
    if label_swapped(r):
        for ka, kb in keys:
            out[ka], out[kb] = r[kb], r[ka]
    return out


def prefill(r):
    """라벨에서 확실히 알 수 있는 칸만 채운다. 애매하면 비워 둔다."""
    r = label_view(r)
    return type_fields(
        r["map_사고장소"],
        r["map_장소특징"],
        r["map_A진행"],
        r["map_B진행"],
        r["map_A신호"],
        r["map_B신호"],
    )


def image_index(label_zip_dir):
    """영상 이름 → (zip 경로, 그 영상의 이미지 라벨 이름 목록)."""
    index = {}
    for z in Path(label_zip_dir).glob("*_이미지_*.zip"):
        with zipfile.ZipFile(z) as zf:
            for name in zf.namelist():
                if name.endswith(".json"):
                    index.setdefault(name.split("/")[0], (z, []))[1].append(name)
    return index


def make_ref_image(r, index, video_path, out_path):
    """충돌 직전 정답 박스 프레임(75 이하 중 가장 가까운 것)과 그 앞 프레임 하나를 나란히
    그린다."""
    import cv2

    frames = [int(f) for f in r["ab_frames"].split(";") if f]
    near = max([f for f in frames if f <= 75] or [min(frames)])
    early = max([f for f in frames if f <= near - 15] or [near])
    zpath, names = index[r["video_name"]]
    by_frame = {}
    with zipfile.ZipFile(zpath) as zf:
        for n in names:
            d = json.loads(zf.read(n).decode("utf-8-sig"))
            by_frame[d["sequence_frame_number"]] = d["objects"]
    cap = cv2.VideoCapture(str(video_path))
    tiles = []
    # 입력표 기준(블랙박스 차=B)으로 글자를 붙인다. 라벨 A/B가 반대인 영상은 바꿔 그린다.
    tags = (
        (("isObjectA", "B", (255, 0, 0)), ("isObjectB", "A", (0, 0, 255)))
        if label_swapped(r)
        else (("isObjectA", "A", (0, 0, 255)), ("isObjectB", "B", (255, 0, 0)))
    )
    for f in sorted({early, near}):
        cap.set(cv2.CAP_PROP_POS_FRAMES, f - 1)
        ok, img = cap.read()
        if not ok:
            continue
        for o in by_frame.get(f, []):
            for flag, tag, color in tags:
                if o.get(flag):
                    x, y, w, h = map(int, o["bbox"])
                    cv2.rectangle(img, (x, y), (x + w, y + h), color, 5)
                    cv2.putText(img, tag, (x, max(40, y - 10)), 0, 2, color, 5)
        cv2.putText(img, f"frame {f}", (20, 60), 0, 2, (0, 255, 255), 5)
        tiles.append(cv2.resize(img, (960, 540)))
    top = cv2.hconcat(tiles) if len(tiles) > 1 else cv2.resize(tiles[0], (1920, 1080))
    # 아래쪽: 충돌 프레임을 고르기 쉽게 51~98번을 번호와 함께 8×6으로 늘어놓는다
    cells = []
    for f in STRIP_FRAMES:
        cap.set(cv2.CAP_PROP_POS_FRAMES, f - 1)
        ok, img = cap.read()
        img = cv2.resize(img, (240, 135)) if ok else top[:135, :240] * 0
        cv2.rectangle(img, (0, 0), (62, 30), (0, 0, 0), -1)
        cv2.putText(img, str(f), (4, 24), 0, 0.8, (0, 255, 255), 2)
        cells.append(img)
    grid = cv2.vconcat([cv2.hconcat(cells[i : i + 8]) for i in range(0, len(cells), 8)])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out_path), cv2.vconcat([top, grid]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="all", choices=["all", "dev", "test"])
    parser.add_argument("--eval-set", default=Path("evals/results/vision/eval_set.csv"), type=Path)
    parser.add_argument("--labels", default=Path("evals/results/vision/labels.csv"), type=Path)
    parser.add_argument("--videos", default=Path("data/private/aihub/videos"), type=Path)
    parser.add_argument("--refs", default=Path("data/private/aihub/answer_refs"), type=Path)
    parser.add_argument(
        "--label-zips",
        default=Path(
            "data/095.교통사고 영상 데이터/01.데이터/2.Validation/라벨링데이터_231108_add"
        ),
        type=Path,
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--refs-only", action="store_true", help="엑셀은 그대로 두고 참고 사진만 다시 만든다"
    )
    args = parser.parse_args()
    name = "answer_cards.xlsx" if args.split == "all" else f"answer_cards_{args.split}.xlsx"
    out = args.out or Path("evals/vision/answers") / name
    if out.exists() and not (args.force or args.refs_only):
        raise SystemExit(f"이미 있습니다(사람이 채운 내용 보호): {out}  다시 만들려면 --force")

    with args.eval_set.open(encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if args.split in ("all", r["split"])]
    rows.sort(key=lambda r: r["split"])  # 개선용(dev) 먼저
    with args.labels.open(encoding="utf-8-sig") as f:
        full = {r["video_name"]: r for r in csv.DictReader(f)}
    index = image_index(args.label_zips)

    if args.refs_only:
        for r in rows:
            name = r["video_name"]
            make_ref_image(
                {**full[name], **r}, index, args.videos / f"{name}.mp4", args.refs / f"{name}.jpg"
            )
        print(f"참고 사진 {len(rows)}장 → {args.refs} (엑셀은 그대로)")
        return

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    PREFILL = PatternFill("solid", fgColor="FFF2CC")
    HEADER = PatternFill("solid", fgColor="D9E1F2")
    wb = Workbook()
    ws = wb.active
    ws.title = "정답"
    headers = (
        REF_COLS
        + [n if n != "collision_time_ms" else "collision_frame" for n, *_ in SHEET_FIELDS]
        + ["메모"]
    )
    ws.append(headers)
    ws.append(
        [""] * len(REF_COLS)
        + [
            d if n != "collision_time_ms" else "충돌 순간 프레임 번호(1~150)"
            for n, _, _, d in SHEET_FIELDS
        ]
        + ["애매한 점, 판단 근거"]
    )
    for c in ws[1] + ws[2]:
        c.fill, c.alignment = HEADER, Alignment(wrap_text=True, vertical="top")
        c.font = Font(bold=c.row == 1)

    for r in rows:
        name = r["video_name"]
        video = (args.videos / f"{name}.mp4").resolve()
        ref = (args.refs / f"{name}.jpg").resolve()
        make_ref_image({**full[name], **r}, index, video, ref)
        pre = prefill(r)
        lv = label_view(r)
        a = lv["accidental_negligence_rateA"] or lv["map_기본과실A"]
        b = lv["accidental_negligence_rateB"] or lv["map_기본과실B"]
        note = (
            "라벨 원본은 A/B 반대(상대 차가 라벨 B). 표에는 블랙박스 차=B로 바꿔 적음"
            if label_swapped(r)
            else ""
        )
        ws.append(
            [
                name,
                r["split"],
                f"{r['video_point_of_view']}인칭",
                "열기",
                "열기",
                r["map_장소특징"],
                lv["map_A진행"],
                lv["map_B진행"],
                f"{a}:{b}",
            ]
            + [pre.get(n, "") for n, *_ in SHEET_FIELDS]
            + [note]
        )
        row = ws.max_row
        ws.cell(row, 4).hyperlink, ws.cell(row, 5).hyperlink = video.as_uri(), ref.as_uri()
        for i, (n, *_) in enumerate(SHEET_FIELDS):
            if n in pre:
                ws.cell(row, len(REF_COLS) + 1 + i).fill = PREFILL

    first, last = 3, ws.max_row
    for i, (n, values, _, _) in enumerate(SHEET_FIELDS):
        col = ws.cell(1, len(REF_COLS) + 1 + i).column_letter
        if isinstance(values, list):
            dv = DataValidation(
                type="list", formula1='"' + ",".join(values) + '"', allow_blank=True
            )
        else:
            dv = DataValidation(
                type="whole",
                operator="between",
                formula1="1",
                formula2="150" if n == "collision_time_ms" else "10",
                allow_blank=True,
            )
        dv.error, dv.showErrorMessage = "목록의 값(또는 범위 안의 숫자)만 넣을 수 있습니다.", True
        ws.add_data_validation(dv)
        dv.add(f"{col}{first}:{col}{last}")
        ws.column_dimensions[col].width = 14
    for col, width in zip("ABCDEFGHI", [32, 6, 7, 6, 8, 20, 26, 26, 9], strict=False):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "B3"

    help_ws = wb.create_sheet("설명")
    help_lines = [
        "정답 카드 입력 방법",
        "1. '영상' 열기를 눌러 영상을 본다(10초, 15fps, 충돌은 보통 5초 근처). 프레임 번호 = 초 × "
        "15.",
        "2. '참고사진'은 라벨 박스로 A(빨강)·B(파랑)를 표시한 사진이다. 1인칭은 촬영 차량이 B다.",
        "3. 노란 칸은 AI Hub 라벨로 미리 채운 값이다. 영상과 다르면 고치고 메모에 이유를 적는다.",
        "4. 빈 칸은 드롭다운에서 고른다. 영상으로 알 수 없으면 '불명', 그 상황에 없는 칸이면 "
        "'해당없음'.",
        "5. 상대 차 신호가 화면에 안 보이면 추측하지 말고 '불명'.",
        "6. relative_position은 B(1인칭이면 내 차) 기준으로 A가 충돌 직전 어느 쪽에 있었는지.",
        "7. motion_trend·time_condition·weather는 채점하지 않는다. 쉬우면 채운다.",
        "",
        "칸 설명 (비전 내부 칸, workers/vision/fields.py)",
    ] + [
        f"- {n}: {d}  [{ {'always': '필수', 'cond': '조건부 필수', 'optional': '선택'}[req] }]"
        + (f"  값: {' / '.join(v)}" if isinstance(v, list) else "")
        for n, v, req, d in SHEET_FIELDS
    ]
    for line in help_lines:
        help_ws.append([line])
    help_ws.column_dimensions["A"].width = 140

    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    print(f"{len(rows)}건 → {out}")
    print(f"참고 사진 → {args.refs}")


if __name__ == "__main__":
    main()
