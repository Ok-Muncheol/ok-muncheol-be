"""가상 견적서 PDF 3개 생성 — data/forms/car_estimate_form.pdf 원본 위에 가상 값 레이어를 겹친다.

실행: py -3.12 tests/fixtures/make_estimate_pdfs.py
- 개발용 의존성: reportlab, pypdf (이 스크립트에서만 사용). 만든 PDF는 커밋하므로 테스트에는 필요
  없다.
- 한글 글꼴은 subset으로 포함: Noto Sans KR(정적 TTF)이 있으면 그것, 없으면 맑은 고딕.
  가변 글꼴(NotoSansKR-VF.ttf)은 reportlab이 글리프를 제대로 그리지 못해 쓰지 않는다.
- 값은 전부 synthetic(가상). 실제 차량·정비업체·사건 아님.
"""

from io import BytesIO
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parents[2]
FORM = ROOT / "data" / "forms" / "car_estimate_form.pdf"
OUT = Path(__file__).resolve().parent
PAGE_H = 841  # 서식 A4 높이(pt). 좌표는 pdfplumber 기준 top(위에서부터)으로 적고 여기서 뒤집는다.

FONT_CANDIDATES = [
    ("NotoSansKR", ["NotoSansKR-Regular.ttf"]),
    ("MalgunGothic", ["malgun.ttf"]),
]
FONT_DIRS = [
    Path("C:/Windows/Fonts"),
    Path.home() / "AppData/Local/Microsoft/Windows/Fonts",
    Path("/usr/share/fonts"),
    Path("/Library/Fonts"),
]


def register_font() -> str:
    for name, files in FONT_CANDIDATES:
        for p in (d / f for f in files for d in FONT_DIRS):
            if p.exists():
                try:
                    pdfmetrics.registerFont(TTFont(name, str(p)))  # TTFont는 subset으로 포함된다
                    return name
                except Exception:  # noqa: BLE001 - 가변/OTF 글꼴 등 reportlab 미지원이면 다음 후보
                    continue
    raise SystemExit("한글 글꼴(Noto Sans KR 또는 맑은 고딕)을 찾지 못했습니다")


# 품목: (견적내용, 코드, 수량, 단가, 공임). 부품 계 = 수량 × 단가, 합계 = 부품 계 + 공임
ITEMS = [
    ("앞 범퍼 교환", "A", 1, 1_200_000, 350_000),
    ("헤드램프(좌) 교환", "A", 1, 4_700_000, 240_909),
    ("앞 범퍼·펜더 도장", "", 1, 300_000, 2_300_000),
]
PARTS = sum(q * p for _, _, q, p, _ in ITEMS)  # 6,200,000
LABOR = sum(lab for *_, lab in ITEMS)  # 2,890,909
VAT = 909_091
TOTAL = PARTS + LABOR + VAT  # 10,000,000

SAMPLES = {
    "estimate_sample_10m.pdf": {"serial": "SYN-2026-0001", "total": TOTAL},
    "estimate_sample_mismatch.pdf": {"serial": "SYN-2026-0002", "total": 10_500_000},  # 합계 불일치
    "estimate_sample_no_total.pdf": {"serial": "SYN-2026-0003", "total": None},  # 총액 빈칸
}

# 칸 좌표(서식에서 측정, pt). x는 칸 오른쪽 끝(숫자 오른쪽 정렬) 또는 왼쪽 시작.
ITEM_ROWS = [353.1, 371.6, 390.1, 408.5, 427.0]  # 품목표 각 행 위쪽 선
SUMMARY_ROW = 461.0  # 합계표 값 행 위쪽 선 (행 높이 약 15.7)
SUMMARY_COLS = {
    "구분": (57.9, 152.4),
    "부품": (152.4, 226.2),
    "공임": (226.2, 299.0),
    "계": (299.0, 379.2),
    "부가가치세": (379.2, 459.5),
    "총액": (459.5, 538.0),
}


def won(n: int) -> str:
    return f"{n:,}"


def overlay(font: str, serial: str, total: int | None) -> bytes:
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, PAGE_H), invariant=1)

    def left(x: float, top: float, s: str, size: float = 9) -> None:
        c.setFont(font, size)
        c.drawString(x, PAGE_H - top, s)

    def right(x: float, top: float, s: str, size: float = 9) -> None:
        c.setFont(font, size)
        c.drawRightString(x, PAGE_H - top, s)

    def center(x0: float, x1: float, top: float, s: str, size: float = 9) -> None:
        c.setFont(font, size)
        c.drawCentredString((x0 + x1) / 2, PAGE_H - top, s)

    left(480, 103, serial)  # 일련번호
    left(156, 131, "00가0000 (가상)", 8.5)  # 등록번호
    left(326, 131, "가상 중형 세단", 8.5)  # 차명
    right(518, 132, "12,345", 8.5)  # 주행거리
    left(156, 175, "2022.03.15", 8.5)  # 등록년월일
    left(326, 175, "SYNTHETICVIN000001", 8.5)  # 차대번호
    left(156, 214, "000-00-00000", 8.5)  # 사업자등록번호
    left(366, 214, "가상-0000", 8.5)  # 정비업 등록번호
    left(156, 244, "가상정비공업사 / 홍가상", 8.5)  # 업체명 및 대표자
    left(156, 275, "서울특별시 가상구 가상로 1", 8.5)  # 주소
    left(460, 278, "02-000-0000", 8)  # 전화번호
    center(158.9, 173.9, 301, "V", 9)  # 견적구분 [V] 보험

    for (name, code, qty, price, lab), top in zip(ITEMS, ITEM_ROWS, strict=False):
        y = top + 12.5
        part = qty * price
        left(62, y, name, 8.5)
        center(226.2, 276.9, y, code, 8.5)
        center(276.9, 322.1, y, str(qty), 8.5)
        right(366, y, won(price), 8.5)
        right(412, y, won(part), 8.5)
        right(470, y, won(lab), 8.5)
        right(535, y, won(part + lab), 8.5)

    y = SUMMARY_ROW + 11.5
    center(*SUMMARY_COLS["구분"], y, "보험", 8.5)
    for key, val in (
        ("부품", PARTS),
        ("공임", LABOR),
        ("계", PARTS + LABOR),
        ("부가가치세", VAT),
        ("총액", total),
    ):
        if val is not None:
            right(SUMMARY_COLS[key][1] - 4, y, won(val), 8.5)

    right(457, 512, "2026")  # 발급 년월일
    right(494, 512, "11")
    right(525, 512, "3")
    left(345, 540, "김가상", 9)  # 작성자
    left(345, 555, "홍가상", 9)  # 대표이사
    c.save()
    return buf.getvalue()


def main() -> None:
    font = register_font()
    for name, s in SAMPLES.items():
        page = PdfReader(FORM).pages[0]
        page.merge_page(PdfReader(BytesIO(overlay(font, s["serial"], s["total"]))).pages[0])
        writer = PdfWriter()
        writer.add_page(page)
        writer.add_metadata(
            {"/Title": f"synthetic estimate {s['serial']}", "/Producer": "make_estimate_pdfs.py"}
        )
        with open(OUT / name, "wb") as f:
            writer.write(f)
        print(f"{name}: 글꼴 {font}, 총액 {s['total']}")


if __name__ == "__main__":
    main()
