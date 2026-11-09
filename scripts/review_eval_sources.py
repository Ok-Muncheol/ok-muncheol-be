"""48개 질문의 원문 기반 1차 검수. 검색 순위는 정답 판정에 사용하지 않는다."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "data/private"

CHART_QUESTIONS = {
    1: "양쪽 신호등이 정상 작동하는 사거리에서 A는 녹색신호에 직진하고 B는 측면 도로에서 적색신호에 직진해 충돌했다. 이 사고의 인정기준을 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
    2: "양쪽 신호등이 있는 사거리에서 A가 녹색신호에 직진 진입했으나 교차로를 벗어나기 전 적색으로 바뀌었고, 측면에서 녹색신호에 직진 진입한 B와 충돌했다. 관련 인정기준은?",  # noqa: E501 -- source text or prompt kept intact
    3: "양쪽 신호등이 있는 사거리에서 A는 황색신호에 직진하고 측면 도로의 B는 적색신호에 직진해 충돌했다. 관련 인정기준은?",  # noqa: E501 -- source text or prompt kept intact
    4: "양쪽 신호등이 있는 사거리에서 서로 측면 도로로부터 진입한 A와 B 모두 적색신호에 직진하다 충돌했다. 관련 인정기준은?",  # noqa: E501 -- source text or prompt kept intact
    5: "사거리에서 A는 적색 점멸신호에 직진하고 측면 도로의 B는 황색 점멸신호에 직진해 충돌했다. 점멸신호 사고의 인정기준은?",  # noqa: E501 -- source text or prompt kept intact
    7: "신호등이 없는 동일 폭의 사거리에서 우측도로의 A와 좌측도로의 B가 모두 직진하다 충돌했다. 동시진입과 각 차량의 선진입을 구분하는 인정기준은?",  # noqa: E501 -- source text or prompt kept intact
    8: "신호등이 없는 사거리에서 오른쪽 좁은 도로의 A가 우회전하고 왼쪽 넓은 도로의 B가 직진하다 충돌했다. 관련 인정기준은?",  # noqa: E501 -- source text or prompt kept intact
    9: "신호등이 없는 동일 폭의 사거리에서 왼쪽 도로의 A가 직진하고 오른쪽 도로의 B가 좌회전하다 충돌했다. 관련 인정기준은?",  # noqa: E501 -- source text or prompt kept intact
    12: "정차한 사륜 자동차 B의 문이 열리면서 뒤에서 직진하던 사륜 자동차 A와 접촉했다. 후행차가 이륜차인 경우와 구분해 해당 인정기준을 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
    15: "신호등이 없는 사거리에서 오른쪽 넓은 도로의 A가 우회전하고 왼쪽 좁은 도로의 B가 직진하다 충돌했다. 관련 인정기준은?",  # noqa: E501 -- source text or prompt kept intact
    16: "신호등이 없는 동일 폭의 사거리에서 오른쪽 도로의 A가 직진하고 왼쪽 도로의 B가 좌회전하다 충돌했다. 관련 인정기준은?",  # noqa: E501 -- source text or prompt kept intact
    18: "일반 도로에서 선행 자동차 B가 진로변경 중 같은 방향에서 후행 직진하던 A와 충돌했다. 정체 차로 대기 중 급차로변경이나 양 차량 동시 진로변경을 전제로 하지 않는 인정기준을 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
}


def read(path):
    return [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines() if s.strip()]


def main():
    for corpus, folder, version, stem in [
        ("fault_standards", "01_fault_standards", "processed", "charts"),
        ("dispute_cases", "02_dispute_cases", "processed", "cases"),
        ("policy_terms", "03_policy_terms", "processed", "clauses"),
    ]:
        base = ROOT / folder
        path = base / "evaluation.jsonl"
        rows = read(path)
        records = read(base / version / f"{stem}.jsonl")
        chunks = read(base / version / "chunks.jsonl")
        if any(
            r.get("dataset_version") or r.get("label_status") == "machine_reviewed" for r in rows
        ):
            print(folder, "릴리스 평가셋 유지. 이전 초안 검수 건너뜀")
            continue
        lookup = {r["parent_id"]: r for r in records}
        for e in rows:
            number = int(e["example_id"].rsplit("-", 1)[1])
            if corpus == "fault_standards" and number in CHART_QUESTIONS:
                e["request"]["text"] = CHART_QUESTIONS[number]
            decisions = []
            if corpus == "dispute_cases":
                if number == 2:
                    e["request"]["text"] = (
                        "신호등 없는 사거리에서 사륜 자동차가 직진하다 맞은편에서 좌회전한 사륜 자동차와 충돌했다. 두 운전자의 주의의무를 다룬 심의사례를 찾아줘."  # noqa: E501 -- source text or prompt kept intact
                    )
                if number == 3:
                    e["expected_parent_ids"] = [
                        "dispute-cases-web:2019-000088",
                        "dispute-cases-web:2017-052646",
                    ]
                    decisions.append(
                        {
                            "parent_id": "dispute-cases-web:2017-052646",
                            "decision": "include",
                            "reason": "같은 사거리·대소로·우회전/직진 조합이며 선진입 여부를 다룸. 질문이 동시진입으로 한정되지 않음.",  # noqa: E501 -- source text or prompt kept intact
                        }
                    )
                if number == 6:
                    e["request"]["text"] = (
                        "사륜 자동차가 유턴 허용 신호를 받고 돌던 중, 맞은편에서 적색신호에 진입해 직진한 다른 사륜 자동차와 충돌했다. 이 사고를 다룬 결정례를 찾아줘."  # noqa: E501 -- source text or prompt kept intact
                    )
                    decisions.append(
                        {
                            "parent_id": "dispute-cases-web:2019-042370",
                            "decision": "not_primary",
                            "reason": "직진차가 황색에 진입한 후 적색으로 바뀐 사례라 적색신호 진입 조건과 다름.",  # noqa: E501 -- source text or prompt kept intact
                        }
                    )
                if number == 7:
                    e["request"]["text"] = (
                        "신호기 있는 교차로에서 직진/우회전 가능한 2차로의 선행차가 우회전하다 우회전 전용 3차로에서 직진한 차와 충돌했다. 노면표시 위반을 다룬 사례들을 찾아줘."  # noqa: E501 -- source text or prompt kept intact
                    )
                    e["expected_parent_ids"] = [
                        "dispute-cases-web:2018-056848",
                        "dispute-cases-web:2019-016823",
                    ]
                    decisions.append(
                        {
                            "parent_id": "dispute-cases-web:2019-016823",
                            "decision": "include",
                            "reason": "청구/피청구 역할은 반대지만 2차로 우회전·3차로 직진 및 노면표시 위반이라는 질문 조건이 같음.",  # noqa: E501 -- source text or prompt kept intact
                        }
                    )
                if number == 9:
                    decisions.append(
                        {
                            "parent_id": "dispute-cases-web:2017-040521",
                            "decision": "not_primary",
                            "reason": "좌회전차 진행 도로에 신호기가 없다고 결정 근거에 명시. 질문은 좌회전 신호 준수를 요구함.",  # noqa: E501 -- source text or prompt kept intact
                        }
                    )
                if number == 1:
                    e["request"]["text"] = (
                        "신호기 있는 사거리에서 사륜 자동차가 녹색신호에 직진하다 측면 도로에서 적색신호에 진입한 사륜 자동차와 충돌했다. 노면표시 위반이 아닌 상대 신호위반 여부 자체가 쟁점인 심의사례를 찾아줘."  # noqa: E501 -- source text or prompt kept intact
                    )
                    decisions.append(
                        {
                            "parent_id": "dispute-cases-web:2019-008384",
                            "decision": "not_primary",
                            "reason": "해당 사례의 구체적 쟁점은 좌회전차로 직진이라는 노면표시 위반임.",  # noqa: E501 -- source text or prompt kept intact
                        }
                    )
            if corpus == "policy_terms":
                text = e["request"]["text"]
                if number <= 12 and "보통약관 본조항" not in text:
                    e["request"]["text"] = (
                        "제공된 삼성화재 개인용 자동차보험 약관에서 특별약관이나 비고를 제외한 보통약관 본조항을 대상으로, "  # noqa: E501 -- source text or prompt kept intact
                        + text
                    )
                e["request"]["filters"] = {"document_id": records[0]["document"]["document_id"]}
            evidence = []
            for parent in e["expected_parent_ids"]:
                r = lookup[parent]
                if corpus == "fault_standards":
                    excerpt = (
                        r["title"] + "\n" + json.dumps(r["party_descriptions"], ensure_ascii=False)
                    )
                elif corpus == "dispute_cases":
                    excerpt = (
                        r["facts"]
                        + "\n"
                        + "\n".join(
                            p["text"] for parts in list(r["sections"].values())[1:2] for p in parts
                        )
                    )
                else:
                    excerpt = r["text"][:1100]
                evidence.append(
                    {
                        "parent_id": parent,
                        "document_hash": r["document"]["content_hash"],
                        "pdf_page_start": r["page_start"],
                        "pdf_page_end": r["page_end"],
                        "excerpt": excerpt,
                    }
                )
            e["assistant_source_review"] = {
                "status": "completed",
                "reviewer": "assistant",
                "scope": "질문 조건과 주 근거 대조, 같은 도표 계열의 유사 사례 검토. 검색 순위 미사용",  # noqa: E501 -- source text or prompt kept intact
                "evidence": evidence,
                "inclusion_decisions": decisions,
                "independent_human_review": "pending",
                "exhaustive_relevance_review": False,
            }
            e["source_alignment"]["evidence_chunk_ids"] = [
                c["chunk_id"] for c in chunks if c["parent_id"] in e["expected_parent_ids"]
            ]
            e["source_pages_by_parent"] = {
                x["parent_id"]: {"start": x["pdf_page_start"], "end": x["pdf_page_end"]}
                for x in evidence
            }
            e["evaluation_scope"] = (
                "provided_document_retrieval_not_accident_liability_or_contract_applicability"
            )
            e["split"] = "unassigned"
            e["label_status"] = "unlabelled"
            e["notes"] = (
                "담당 assistant의 원문 대조 1차 완료. 독립 검수 및 관련 정답 완전성 검토 전. 검색 순위에 맞춘 정답 변경 아님."  # noqa: E501 -- source text or prompt kept intact
            )
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
        )
        result_path = base / "evaluation_results.json"
        if result_path.exists():
            report = json.loads(result_path.read_text(encoding="utf-8"))
            report["status"] = "stale_after_evaluation_source_review"
            report["stale_reason"] = (
                "질문 조건/정답 목록 수정. 이전 검색 순위를 현재 평가셋의 결과로 사용하지 않음."
            )
            result_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        print(folder, len(rows), "source review recorded")


if __name__ == "__main__":
    main()
