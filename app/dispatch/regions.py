"""B(통합): 사고 장소 글에서 지역(17개 시·도)을 규칙으로 판별한다.

배정 에이전트가 없거나 실패했을 때 쓰는 규칙 쪽 판별이다. 글에 시·도 이름이나 잘 알려진 시 이름이
있을 때만 맞히고, 모르면 None을 돌려준다(그 접수는 미배정으로 남아 관리자가 본다).
낱말의 앞머리로 맞춘다 — "해운대구"가 "대구"로, "서울대입구"가 다른 곳으로 읽히지 않게 한다.
"""

import re
import typing

from app.schemas.admin import Region

REGIONS: tuple[str, ...] = typing.get_args(Region)

# 도 이름이 광역시 이름보다 먼저다 — "경기도 광주시"는 경기다
_PROVINCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("경기", ("경기",)),
    ("강원", ("강원",)),
    ("충북", ("충북", "충청북도")),
    ("충남", ("충남", "충청남도")),
    ("전북", ("전북", "전라북도")),
    ("전남", ("전남", "전라남도")),
    ("경북", ("경북", "경상북도")),
    ("경남", ("경남", "경상남도")),
    ("제주", ("제주",)),
)
_METROS: tuple[str, ...] = ("서울", "부산", "대구", "인천", "대전", "울산", "세종")
# 시·도 이름 없이 시 이름만 적은 경우
_CITIES: dict[str, tuple[str, ...]] = {
    "경기": ("수원", "성남", "고양", "용인", "부천", "안산", "안양", "남양주", "화성", "평택",
           "의정부", "시흥", "파주", "김포", "광명", "하남", "일산", "분당"),
    "강원": ("춘천", "원주", "강릉", "속초"),
    "충북": ("청주", "충주", "제천"),
    "충남": ("천안", "아산", "서산", "당진"),
    "전북": ("전주", "익산", "군산"),
    "전남": ("목포", "여수", "순천", "광양"),
    "경북": ("포항", "경주", "구미", "안동", "경산"),
    "경남": ("창원", "김해", "진주", "양산", "거제", "통영"),
    "제주": ("서귀포",),
}


def detect(location: str) -> str | None:
    """사고 장소 글 → 지역. 판별하지 못하면 None."""
    words = [w for w in re.split(r"[\s,·/()\[\]]+", location or "") if w]

    def has(prefixes: tuple[str, ...]) -> bool:
        return any(word.startswith(prefix) for word in words for prefix in prefixes)

    for region, names in _PROVINCES:
        if has(names):
            return region
    for region in _METROS:
        if has((region,)):
            return region
    # 광주: "광주시"는 경기도 광주시, "광주"·"광주광역시"는 광주광역시로 본다
    for word in words:
        if word.startswith("광주"):
            return "경기" if word.startswith("광주시") else "광주"
    for region, cities in _CITIES.items():
        if has(cities):
            return region
    return None
