"""검색용 텍스트 전처리. 원문/숫자/단서는 보존하고 문서 장식만 제거한다."""

import re

VERSION = "conservative-layout-v1"


def clean_text(text: str, corpus: str, page: int) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200b\ufeff]", "", text)
    text = re.sub(r"(?m)^자동차사고 과실비율(?: 인정기준|분쟁 심의 사례)\s*[│|].*$", "", text)
    if corpus == "fault_standards":
        text = re.sub(
            r"(?m)^제2장\.\s*\n자동차와\s*\n자동차\(이륜차\s*\n포함\)의\s*\n사고\s*\n?", "", text
        )
    if corpus == "dispute_cases":
        for number, lines in [
            (1, "자동차와\\s*\\n자동차의\\s*\\n사고"),
            (2, "자동차와\\s*\\n이륜차의\\s*\\n사고"),
            (3, "고속도로의\\s*\\n사고"),
        ]:
            text = re.sub(rf"(?m)^{number}\.\s*\n{lines}\s*\n?", "", text)
    if corpus == "policy_terms":
        text = text.replace("* 비고", "")
        text = re.sub(r"(?m)^\* 비고\s*(?=\n|$)", "", text)
        text = re.sub(r"(?m)^개인용\s*애니카\s*자동차보험\s+\d+\s*$", "", text)
        navigation = [
            "용어의\n정의\n보상하는\n내용\n보험금,\n손해배상\n청구\n일반사항",
            "운전가능자\n제한\n보상\n확대\n사고처리시\n비용\n긴급출동\n서비스\n보험료\n납입\n친환경\n상품\n기타",
        ]
        for block in navigation:
            text = text.replace(block, "")
        # 책의 인쇄 쪽수는 이 원문에서 PDF 실제 페이지-2. 다른 숫자 행은 유지.
        lines = text.splitlines()
        while lines and (not lines[-1].strip() or lines[-1].strip() == str(page - 2)):
            lines.pop()
        text = "\n".join(lines)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    result = []
    boundary = re.compile(
        r"^(?:[•●⊙※①-⑳]|\(?\d+[.)]|[가-힣][.)]|[+-]\d|제\d+[조장절]|\[|\{|차\d+-|심의번호)"
    )
    for line in lines:
        if not line:
            if result and result[-1] != "":
                result.append("")
            continue
        # 순수 서술 줄만 합친다. 표/목록/숫자 행은 그대로 둔다.
        if (
            result
            and result[-1]
            and not boundary.match(line)
            and len(result[-1]) >= 35
            and re.search(r"[가-힣,]$", result[-1])
            and re.match(r"^[가-힣]", line)
        ):
            result[-1] += " " + line
        else:
            result.append(line)
    return "\n".join(result).strip()


def preprocess_chunks(chunks):
    output = []
    seen = set()
    for original in chunks:
        chunk = {**original, "metadata": dict(original["metadata"])}
        raw = original.get("raw_text", original["text"])
        text = clean_text(raw, chunk["document"]["corpus"], chunk["page_start"])
        chunk["raw_text"] = raw
        chunk["text"] = text
        chunk["metadata"]["preprocessing_version"] = VERSION
        # 제목만 남은 조항 페이지는 검색 청크에서 제외. 원문은 부모 레코드에 있음.
        content = (
            text.splitlines()[1:]
            if chunk["document"]["corpus"] == "policy_terms"
            else text.splitlines()
        )
        if not any(line.strip() for line in content):
            continue
        identity = (chunk["parent_id"], text)
        if not text or identity in seen:
            continue
        seen.add(identity)
        output.append(chunk)
    return output
