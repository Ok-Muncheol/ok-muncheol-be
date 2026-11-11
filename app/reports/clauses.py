"""D: 검토 보고서 8·9번 약관 근거 조항. 계산은 바꾸지 않고 근거만 붙인다. LLM 호출 없음.

- 어떤 조항을 붙일지는 `select_report_clauses` 하나에서 정한다. 조항을 바꾸거나 더할 때는 이 함수만
  고친다.
- `fetch_report_clauses`가 조항마다 `fetch_by_parent`로 꺼내 C의 `validate_hits`로 검증한다.
  통과한 조항만 붙인다. 실패는 조항 단위로 건너뛰고(경고 로그) 보고서는 그대로 만든다.
  코퍼스·색인·EvidenceStore가 없으면(운영 포함) "연결 안 됨"으로 표시한다.
- 잡는 예외는 `ValueError`·`RuntimeError`(검증 실패·`RetrievalNotConfigured`·EvidenceStore
  미등록)뿐이다. 그 밖의 예외(코드 버그)는 감싸지 않는다.
- 청구서(구상 청구 패키지) 근거서에는 조항 이름만 넣는다(`claim_clause_names`).
"""

import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date

from app.schemas.reports import ReviewReport
from app.schemas.retrieval import EvidenceHit
from app.tools._evidence import validate_hits

logger = logging.getLogger(__name__)

FetchFn = Callable[..., Awaitable[list[EvidenceHit]]]

TERMS = "samsung-personal-auto-2026-11-11"
NOT_CONNECTED = "연결 안 됨"
TEXT_ONLY_SOURCE = "약관 코퍼스에 없음 — 법령명만 표기"
# 예시 규칙(rule_version이 example-)일 때 표시. 근거가 붙어도 금액 규칙·조항은 실무 검수 전이다
EXAMPLE_NOTICE = "예시 규칙 기준 — 약관 검수 전 초안"
EXAMPLE_NOTICE_WITH_CLAUSES = "예시 규칙 기준 — 약관 조항 원문 연결(실무 검수 전) 초안"
_EXCERPT = 120


@dataclass(frozen=True)
class ClauseRef:
    name: str
    parent_id: str | None = None  # None이면 코퍼스에 없는 법령: 이름만 표기
    # 발췌를 이 문구가 있는 청크·위치에서 시작(공백 무시). 못 찾으면 그 항목은 붙이지 않는다
    excerpt_from: str | None = None
    in_claim: bool = False  # 청구서 근거서에 이름을 넣는다


@dataclass(frozen=True)
class ClauseSelection:
    payout: tuple[ClauseRef, ...]  # 8번 자차 보험금
    recovery_main: tuple[ClauseRef, ...]  # 9번 구상 주 근거
    recovery_reference: tuple[ClauseRef, ...]  # 9번 구상 참고 근거

    def all(self) -> tuple[ClauseRef, ...]:
        return self.payout + self.recovery_main + self.recovery_reference


def select_report_clauses(accident_date: str | None) -> ClauseSelection:
    """8·9번에 붙일 조항. 조항을 바꾸거나 더할 때는 이 함수만 고친다.

    - 제10조(대인배상Ⅱ·대물배상 지급보험금 계산)는 자차 산정과 무관해 쓰지 않는다.
    - 별표3 과실상계는 사고일로 판 하나를 고른다(2022-12-31 이전 / 2023-01-01 이후).
      사고일이 없거나 날짜가 아니면 넣지 않는다.
    - 제24조의 호(號)는 같은 parent 안 문구로 발췌 위치를 고른다(① 제4호 권리 취득 문장, ① 제5호).
      제24조 ④항은 전부손해 시 피해물 인수라 구상 근거가 아니다.
    """
    article_24 = f"{TERMS}:general_article:24:p68"
    reference = []
    day = _parse_date(accident_date)
    if day is not None:
        if day < date(2023, 1, 1):
            reference.append(ClauseRef(
                "별표3 1. 과실상계(2022년 12월 31일 이전 사고)",
                f"{TERMS}:payment_standard:별표3:1:until-2022-12-31",
            ))
        else:
            reference.append(ClauseRef(
                "별표3 1. 과실상계(2023년 1월 1일 이후 사고)",
                f"{TERMS}:payment_standard:별표3:1:from-2023-01-01",
            ))
    reference.append(ClauseRef("별표2 1. 수리비용", f"{TERMS}:payment_standard:별표2:1"))
    reference.append(ClauseRef(
        "보통약관 제24조 ① 제5호(구상금을 받으면 자기부담금 차액을 피보험자에게 지급)",
        article_24,
        excerpt_from="5. 「자기차량손해」 보험금을 지급한 보험회사가",
    ))
    return ClauseSelection(
        payout=(ClauseRef("보통약관 제24조(지급보험금의 계산)", article_24),),
        recovery_main=(
            ClauseRef(
                "보통약관 제34조(보험회사의 대위)",
                f"{TERMS}:general_article:34:p76",
                in_claim=True,
            ),
            ClauseRef(
                "보통약관 제24조 ① 제4호"
                "(자기부담금 확정 부담 조건 선지급 후 상대방에 대한 권리 취득)",
                article_24,
                excerpt_from="이 경우 「자기차량손해」 보험금을 지급한 보험회사는",
            ),
            ClauseRef("상법 제682조(제3자에 대한 보험대위)", in_claim=True),
        ),
        recovery_reference=tuple(reference),
    )


def _parse_date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


@dataclass(frozen=True)
class ReportClauses:
    """검증을 통과한 조항(parent_id → 청크). 비어 있으면 아무것도 연결되지 않은 것."""

    selection: ClauseSelection
    hits: Mapping[str, list[EvidenceHit]]
    edition: str | None = None

    @property
    def connected(self) -> bool:
        return bool(self.hits)


async def fetch_report_clauses(
    selection: ClauseSelection,
    *,
    fetch: FetchFn,
    edition: str | None,
    accident_date: str | None = None,
) -> ReportClauses:
    """조항마다 꺼내 검증한다. 실패한 조항만 빠지고 나머지는 붙는다.

    사고일을 검증 요청에 넣어 C의 사고일 범위 검사(별표3)도 함께 거친다."""
    hits: dict[str, list[EvidenceHit]] = {}
    day = _parse_date(accident_date)
    for parent_id in dict.fromkeys(r.parent_id for r in selection.all() if r.parent_id):
        try:
            found = await fetch("policy_terms", [parent_id], terms_edition=edition)
            request = {
                "corpus": "policy_terms",
                "text": parent_id,
                "accident_date": day.isoformat() if day else "",
                "filters": {} if edition is None else {"edition": edition},
                "top_k": max(len(found), 1),
            }
            valid = await validate_hits(request, found)  # type: ignore[arg-type]
        except (ValueError, RuntimeError) as exc:  # 검증 실패·색인 미연결·EvidenceStore 미등록
            logger.warning("보고서 근거 조항 미연결: %s (%s)", parent_id, exc)
            continue
        if valid:
            hits[parent_id] = valid
    return ReportClauses(selection, hits, edition)


def _pages(chunks: list[EvidenceHit]) -> str:
    start = min(h["chunk"]["page_start"] for h in chunks)
    end = max(h["chunk"]["page_end"] for h in chunks)
    return f"{start}" + (f"–{end}" if end != start else "")


def _find_loose(text: str, phrase: str) -> int:
    """공백을 무시하고 찾는다. 원문 청크는 줄바꿈 자리에 공백이 끼어 있다(예: '보험 금을')."""
    pattern = r"\s*".join(re.escape(ch) for ch in phrase if not ch.isspace())
    found = re.search(pattern, text)
    return found.start() if found else -1


def _row(ref: ClauseRef, clauses: ReportClauses) -> dict[str, object] | None:
    if ref.parent_id is None:
        return {"조항": ref.name, "출처": TEXT_ONLY_SOURCE}
    chunks = clauses.hits.get(ref.parent_id)
    if not chunks:
        return None
    first = chunks[0]["chunk"]
    text, page = first["text"], _pages(chunks)
    if ref.excerpt_from:
        located = next(
            (
                (h, i)
                for h in chunks
                if (i := _find_loose(h["chunk"]["text"], ref.excerpt_from)) >= 0
            ),
            None,
        )
        if located is None:  # 다른 항·호의 문구를 이 이름으로 보여 주지 않는다
            logger.warning("보고서 근거 조항 발췌 위치 없음: %s (%s)", ref.name, ref.parent_id)
            return None
        h, i = located
        text, page = h["chunk"]["text"][i:], _pages([h])
    return {
        "조항": ref.name,
        "ID": ref.parent_id,
        "적용판": first["document"]["edition"],
        "페이지": page,
        "발췌": text[:_EXCERPT],
    }


def _rows(refs: tuple[ClauseRef, ...], clauses: ReportClauses) -> list[dict[str, object]] | str:
    rows = [r for r in (_row(ref, clauses) for ref in refs) if r is not None]
    has_clause = any("ID" in r for r in rows)
    return rows if has_clause else NOT_CONNECTED


def clause_notice(is_example: bool, connected: bool) -> str:
    """보고서 8번 '근거 상태'와 청구서 rule_notice가 같은 문구를 쓴다."""
    if is_example:
        return EXAMPLE_NOTICE_WITH_CLAUSES if connected else EXAMPLE_NOTICE
    return "약관 조항 원문 연결" if connected else f"약관 조항 {NOT_CONNECTED}"


def payout_basis(clauses: ReportClauses | None) -> object:
    """8번 '근거 조항'."""
    return NOT_CONNECTED if clauses is None else _rows(clauses.selection.payout, clauses)


def recovery_basis(clauses: ReportClauses | None) -> dict[str, object]:
    """9번 본문에 더할 칸. 하나도 연결되지 않으면 '연결 안 됨' + 법령(문구) 근거만."""
    if clauses is None or not clauses.connected:
        text_only = select_report_clauses(None).recovery_main
        return {
            "근거 조항": NOT_CONNECTED,
            "법령 근거": [_row(r, _EMPTY) for r in text_only if r.parent_id is None],
        }
    sel = clauses.selection
    main = [r for r in (_row(ref, clauses) for ref in sel.recovery_main) if r is not None]
    return {
        "근거 조항": {
            "주 근거": main if any("ID" in r for r in main) else [NOT_CONNECTED, *main],
            "참고 근거": _rows(sel.recovery_reference, clauses),
        }
    }


def clause_ids(refs: tuple[ClauseRef, ...], clauses: ReportClauses | None) -> list[str]:
    """그 항목의 evidence_ids에 더할 검증된 조항 ID."""
    if clauses is None:
        return []
    return list(dict.fromkeys(r.parent_id for r in refs if r.parent_id in clauses.hits))


def _section(report: ReviewReport, no: int) -> dict[str, object]:
    return next((s for s in report["sections"] if s["no"] == no), {})  # type: ignore[return-value]


def report_clauses_connected(report: ReviewReport) -> bool:
    """보고서 8·9번에 검증된 조항이 하나라도 붙었는지(evidence_ids 기준)."""
    known = {r.parent_id for r in select_report_clauses("2023-01-01").all() if r.parent_id}
    known |= {r.parent_id for r in select_report_clauses("2022-12-31").all() if r.parent_id}
    ids = {i for no in (8, 9) for i in _section(report, no).get("evidence_ids", [])}  # type: ignore[union-attr]
    return bool(known & ids)


def claim_clause_names(report: ReviewReport) -> list[str]:
    """청구서 근거서 '근거 조항': 이름만. 코퍼스 조항은 9번에 검증돼 붙은 것만, 법령은 항상."""
    ids = set(_section(report, 9).get("evidence_ids", []))  # type: ignore[arg-type]
    return [
        r.name
        for r in select_report_clauses(None).recovery_main
        if r.in_claim and (r.parent_id is None or r.parent_id in ids)
    ]


_EMPTY = ReportClauses(select_report_clauses(None), {})
