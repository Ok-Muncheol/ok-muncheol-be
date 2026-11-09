"""제공된 삼성화재 2026.11.11 판의 좌우 교대 본문/비고 열 분리."""


def extract_columns(page, page_number):
    """페이지 좌표로 분리하며 작은 글자/각주 번호를 글꼴 크기로 버리지 않는다.

    짝수 PDF 페이지: 왼쪽 비고, 오른쪽 본문. 홀수는 반대.
    경계는 이 원문의 본문/비고 사이 여백에서 확인한 값이다.
    """
    visible = page.within_bbox(page.bbox).dedupe_chars(tolerance=1)
    even = page_number % 2 == 0
    boundary = 80 if even else 232

    def content(obj):
        return (
            obj.get("object_type") == "char"
            and 15 <= obj["top"]
            and obj["bottom"] < 550
            and 15 <= obj["x0"]
            and obj["x1"] < 294
        )

    def main(obj):
        if page_number == 72:
            # 제출 서류 표는 비고 열을 쓰지 않는 전폭 페이지다.
            return content(obj)
        return content(obj) and (
            (obj["x0"] + obj["x1"]) / 2 >= boundary
            if even
            else (obj["x0"] + obj["x1"]) / 2 < boundary
        )

    def note(obj):
        return content(obj) and not main(obj)

    main_page = visible.filter(main)
    note_page = visible.filter(note)
    headings = main_page.filter(lambda obj: 9.4 <= obj.get("size", 0) <= 11).extract_text() or ""
    return {
        "body": main_page.extract_text() or "",
        "side_note": note_page.extract_text() or "",
        "headings": headings,
        "body_bbox": [boundary if even else 20, 15, 294 if even else boundary, 550],
        "side_note_bbox": [20 if even else boundary, 15, boundary if even else 294, 550],
        "version": "samsung-20261111-columns-v1",
    }
