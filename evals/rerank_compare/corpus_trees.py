"""D: 도표·결정례 목차 트리(①②③ DecisionRAG 방식용). 약관 트리는 decisionrag.build_tree.

도표(TREE fault-toc-v1): L1 묶음 24개 = 원문 목차(제3편 제2장 "4. 세부유형별 과실비율 적용기준"
p.147~488)의 절 23개(가~마 장 아래 (1)~(8)) + "3. 수정요소의 해설"(p.129~147, 공통 수정요소
해설 parent 19개) → L2 도표(parent) → L3 청크. 장은 묶음 경로에만 쓴다(5개 중 몇 개를 남기는
단계를 따로 두지 않음). 도표 번호 범위(차1~차4 등)는 원문 목차 제목 옆 [차N]을 옮긴 것이다
(코퍼스에 장·절 칸이 없음). 차대차만 있어 "차대차" 단계는 없다.

결정례(TREE dispute-chart-v1): L1 묶음 = 결정례가 연결된 도표(chart_id)의 절(도표 목차와 같은 경로)
→ L2 결정례(parent) → L3 청크. 도표 연결은 사람 검토 전이다(chart_link_human_reviewed False).

예산은 약관과 같다(L1 묶음 5, L2 parent 20, 노드 본문 128토큰). 평가 문항을 보기 전에 트리 크기만
보고 정했다(11/12). 결과를 보고 바꾸지 않는다.
"""

import re
from collections.abc import Mapping
from typing import Any

from evals.rerank_compare.common import DISPUTE, FAULT, CorpusParents, TokenEncoder, passage_text

FAULT_TREE_VERSION = "fault-toc-v1"
DISPUTE_TREE_VERSION = "dispute-chart-v1"

FAULT_ROOT = "자동차와 자동차(이륜차 포함)의 사고"
_A = "가. 교차로(+자로, T자로 등) 사고"
_B = "나. 마주보는 방향 진행차량 상호 간의 사고"
_C = "다. 같은 방향 진행차량 상호 간의 사고"
_D = "라. 기타 유형의 사고"
_E = "마. 자동차 대 이륜차 특수유형"
# (첫 도표 번호, 끝 번호, 장, 절) — 원문 p.147~482 제목. 마는 절이 없어 장 이름을 절로 쓴다
FAULT_SECTIONS: tuple[tuple[int, int, str, str], ...] = (
    (1, 4, _A, "(1) 양쪽 신호등 있는 교차로"),
    (5, 6, _A, "(2) 한쪽 신호등 있는 교차로(상대차량이 측면에서 진입)"),
    (7, 10, _A, "(3) 한쪽 지시표지 있는 교차로"),
    (11, 11, _A, "(4) 교차로 노면 표시 위반 사고"),
    (12, 18, _A, "(5) 신호등 없는 교차로"),
    (19, 21, _A, "(6) 교차로 부근 동시 우회전 내지 좌회전 사고"),
    (31, 31, _B, "(1) 중앙선 침범 사고"),
    (32, 32, _B, "(2) 중앙선 없거나 중앙선침범 미적용 도로에서 교행 사고"),
    (33, 33, _B, "(3) 직진(우회전)차와 유턴차 사이의 사고"),
    (41, 41, _C, "(1) 안전거리미확보로 인한 추돌사고"),
    (42, 42, _C, "(2) 주정차 차량 추돌사고"),
    (43, 43, _C, "(3) 진로변경 사고"),
    (44, 44, _C, "(4) 도로로 진입하는 차와 직진차와의 사고"),
    (45, 45, _C, "(5) 앞지르기 금지 장소에서 추월사고"),
    (46, 46, _C, "(6) 선행 유턴 대 후행 유턴 사고"),
    (47, 47, _C, "(7) 정차 후 출발 대 직진(우회전) 사고"),
    (48, 48, _C, "(8) 낙하물 사고"),
    (51, 51, _D, "(1) 주차장 사고"),
    (52, 52, _D, "(2) 문 열림 사고"),
    (53, 53, _D, "(3) 횡단보도 횡단 차량"),
    (54, 54, _D, "(4) 회전교차로 사고"),
    (55, 55, _D, "(5) 긴급자동차 사고"),
    (61, 61, _E, ""),
)  # fmt: skip
MODIFIER_GUIDE_PATH = f"{FAULT_ROOT} > 3. 수정요소(인과관계를 감안한 과실비율 조정)의 해설"
# 원문 p.129~147의 소제목(공통 수정요소 해설 parent는 제목 칸이 없어 묶음 글에 이것을 쓴다)
MODIFIER_GUIDE_ITEMS = (
    "(1) 현저한 과실",
    "(2) 중대한 과실",
    "(3) 사고 상황별 주요 수정요소",
    "(4) 기타 주요 수정요소",
)
_CHART_NUMBER = re.compile(r"^차(\d+)-\d+$")


def chart_section(chart_id: str) -> tuple[int, str]:
    """도표 번호 → (목차 순서, 묶음 경로). 수정요소 해설이 순서 0(원문 앞쪽)."""
    found = _CHART_NUMBER.match(chart_id)
    if not found:
        raise ValueError(f"도표 번호 꼴이 아닙니다: {chart_id}")
    number = int(found.group(1))
    for order, (first, last, chapter, section) in enumerate(FAULT_SECTIONS, 1):
        if first <= number <= last:
            path = f"{FAULT_ROOT} > {chapter}" + (f" > {section}" if section else "")
            return order, path
    raise ValueError(f"목차에 없는 도표: {chart_id}")


def _one_line(text: object) -> str:
    return " ".join(str(text).split())


def chart_titles(corpus: Any) -> dict[str, str]:
    """도표 chart_id → "차1-1 녹색직진 대 적색직진"(read_corpus 결과의 도표 레코드)."""
    return {
        r["body"]["chart_id"]: f"{r['body']['chart_id']} {_one_line(r['body']['title'])}"
        for r in corpus.records
        if r.get("corpus") == FAULT and r.get("is_parent")
    }


def _doc_order(parents: CorpusParents) -> list[str]:
    page = {pid: parents.chunks(pid)[0]["page_start"] for pid in parents.parent_ids()}
    return sorted(page, key=lambda pid: (page[pid], pid))


def _assemble(
    placed: Mapping[str, tuple[int, str]],
    labels: Mapping[str, str],
    node_paths: Mapping[str, str],
    group_items: Mapping[tuple[int, str], list[str]],
    order: list[str],
    parents: CorpusParents,
    encoder: TokenEncoder | None,
    version: str,
):
    from evals.rerank_compare.decisionrag import (
        NODE_BODY_TOKENS,
        GroupNode,
        ParentNode,
        PolicyTree,
    )

    members: dict[tuple[int, str], list[str]] = {}
    for pid in order:
        members.setdefault(placed[pid], []).append(pid)
    groups = []
    for key, pids in sorted(members.items()):
        items = ", ".join(dict.fromkeys(group_items[key]))
        groups.append(GroupNode(key[1], passage_text(key[1], items, encoder=encoder), tuple(pids)))
    nodes = {}
    for pid in order:
        body = "\n".join(c["text"] for c in parents.chunks(pid))
        title = f"{node_paths[pid]} > {labels[pid]}"
        text = passage_text(title, body, encoder=encoder, max_tokens=NODE_BODY_TOKENS)
        nodes[pid] = ParentNode(pid, placed[pid][1], text)
    return PolicyTree(tuple(groups), nodes, version, parents.name)


def build_fault_tree(parents: CorpusParents, *, encoder: TokenEncoder | None = None):
    """도표 parent 전부: 도표는 번호로 절에, 공통 수정요소 해설은 "3. 수정요소의 해설"에."""
    if parents.name != FAULT:
        raise ValueError(f"도표 코퍼스가 아닙니다: {parents.name}")
    order = _doc_order(parents)
    placed: dict[str, tuple[int, str]] = {}
    labels: dict[str, str] = {}
    items: dict[tuple[int, str], list[str]] = {}
    for pid in order:
        body = parents.bodies.get(pid)
        label = parents.title(pid)
        if body is None:  # 공통 수정요소 해설(제목 칸 없음)
            if ":modifier-guide:" not in pid:
                raise ValueError(f"도표도 수정요소 해설도 아닌 parent: {pid}")
            key = (0, MODIFIER_GUIDE_PATH)
            items.setdefault(key, list(MODIFIER_GUIDE_ITEMS))
        else:
            key = chart_section(body["chart_id"])
            items.setdefault(key, []).append(label)
        placed[pid] = key
        labels[pid] = label
    paths = {pid: placed[pid][1] for pid in order}
    return _assemble(placed, labels, paths, items, order, parents, encoder, FAULT_TREE_VERSION)


def build_dispute_tree(
    parents: CorpusParents, charts: Mapping[str, str], *, encoder: TokenEncoder | None = None
):
    """결정례 parent 전부를 연결된 도표의 절에. charts는 chart_titles(read_corpus()).

    묶음 글 = 경로 + 연결된 도표 제목들, 결정례 노드 = 경로 > 도표 제목 > "심의번호 … 사례 제목"
    + 본문 앞 128토큰.
    """
    if parents.name != DISPUTE:
        raise ValueError(f"결정례 코퍼스가 아닙니다: {parents.name}")
    order = _doc_order(parents)
    placed: dict[str, tuple[int, str]] = {}
    labels: dict[str, str] = {}
    paths: dict[str, str] = {}
    items: dict[tuple[int, str], list[str]] = {}
    for pid in order:
        body = parents.bodies[pid]
        chart_id = body["chart_id"]
        if chart_id not in charts:
            raise ValueError(f"{pid}: 연결된 도표 {chart_id}가 도표 코퍼스에 없습니다")
        key = chart_section(chart_id)
        placed[pid] = key
        labels[pid] = f"{parents.title(pid)} {_one_line(body['title'])}"
        paths[pid] = f"{key[1]} > {charts[chart_id]}"
        items.setdefault(key, []).append(charts[chart_id])
    return _assemble(placed, labels, paths, items, order, parents, encoder, DISPUTE_TREE_VERSION)
