"""현재 평가셋의 원문 복사 질문을 교체하고 출처/검수 정보를 보강한다."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "data/private"

CASE_QUERIES = {
    "2018-051544": "나는 초록불에 교차로를 직진했고 옆 도로의 차가 빨간불에 들어와 충돌했다. 상대의 신호위반 여부가 쟁점인 심의사례를 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
    "2019-034337": "신호등 없는 사거리에서 직진 차량과 맞은편 좌회전 차량이 부딪쳤다. 두 운전자의 주의의무를 다룬 결정례가 필요하다.",  # noqa: E501 -- source text or prompt kept intact
    "2019-000088": "신호등 없는 사거리에서 좁은 길의 우회전차와 큰길의 직진차가 충돌했다. 어느 차가 먼저 들어왔는지 다툰 심의사례를 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
    "2018-060387": "삼거리 오른쪽 좁은 길에서 우회전으로 나오는 차와 왼쪽 큰길의 직진차가 충돌했다. 큰길 차량의 선진입이 쟁점인 사례는?",  # noqa: E501 -- source text or prompt kept intact
    "2019-034069": "앞지르기 금지 구간인 교차로에서 뒤차가 앞차를 추월해 앞으로 들어오다 사고가 났다. 앞차가 이를 알아차릴 수 있었는지 검토한 사례를 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
    "2018-029704": "유턴 허용 신호를 받고 돌던 차가 적색신호를 어기고 직진한 차와 충돌했다. 이런 사고를 다룬 결정례를 보고 싶다.",  # noqa: E501 -- source text or prompt kept intact
    "2018-056848": "교차로에서 직진과 회전이 허용된 2차로의 차가 우회전하는데, 우회전 전용 3차로의 차가 직진해 충돌했다. 차로 노면표시 위반을 다룬 사례는?",  # noqa: E501 -- source text or prompt kept intact
    "2019-042047": "같은 폭의 신호 없는 사거리다. 오른쪽 도로의 자동차가 먼저 들어왔고 왼쪽 도로의 오토바이가 나중에 들어와 충돌했다. 선진입을 검토한 심의사례를 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
    "2019-062773": "좌회전 신호대로 돌던 자동차와 왼쪽 도로에서 빨간불을 어기고 직진한 오토바이가 부딪쳤다. 유사 결정례를 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
    "2019-063377": "같은 차로에서 앞차가 크게 좌회전하는 동안 뒤의 오토바이가 왼쪽으로 추월하다 충돌했다. 대좌회전 여부를 따진 사례를 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
    "2019-033822": "내 진행 방향에만 신호등이 있는 교차로에서 황색불에 직진하다, 왼쪽 작은 도로에서 직진하던 차와 부딪쳤다. 이런 심의사례를 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
    "2019-044386": "고속도로 갓길로 달리던 차 앞으로 2차로의 차가 가까운 거리에서 갓길로 끼어들어 충돌했다. 근접 진로변경을 다룬 사례를 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
}

EXTRA_CHARTS = {
    "차2-2": "녹색 직진신호를 따라간 A와 맞은편에서 신호를 위반하고 좌회전한 B가 부딪쳤다. 적색 직진 대 정상 좌회전 사고와 구별할 기준을 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
    "차2-4": "직진차와 맞은편 좌회전차가 같은 황색 또는 적색신호를 어기고 충돌했다. 양쪽의 동일신호 위반을 다루는 기준은?",  # noqa: E501 -- source text or prompt kept intact
    "차13-4": "오른쪽 큰길에서 우회전하던 A가 왼쪽 좁은 길의 직진차 B와 충돌했다. 소로 우회전 대 대로 직진 사고와 구별해 기준을 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
    "차16-2": "오른쪽 도로에서 직진한 A와 왼쪽 도로에서 좌회전한 B가 충돌했다. 좌우 도로 위치를 구분해 도표를 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
    "차42-3": "고속도로 갓길에 주정차 중인 차를 뒤차가 들이받았다. 주행차로 주정차 사고와 구별되는 기준을 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
    "차43-2": "같은 방향으로 진행하던 두 차 중 한 차가 진로를 바꾸다 직진차와 충돌했다. 진로변경과 직진 간 사고의 기준을 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
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
        if any(
            r.get("dataset_version") or r.get("label_status") == "machine_reviewed" for r in rows
        ):
            print(folder, "릴리스 평가셋 유지. 초안 보완 건너뜀")
            continue
        if any(r.get("assistant_source_review", {}).get("status") == "completed" for r in rows):
            print(folder, "원문 대조 결과 유지. 자동 초안 보완 건너뜀")
            continue
        records = read(base / version / f"{stem}.jsonl")
        lookup = {r["parent_id"]: r for r in records}
        chunks = read(base / version / "chunks.jsonl")
        if any(r["label_status"] == "reviewed" for r in rows):
            raise ValueError("검수 완료 데이터는 자동 덮어쓰기하지 않습니다.")
        if corpus == "dispute_cases":
            selected = {r["case_id"]: r for r in records}
            for e, (case, query) in zip(rows, CASE_QUERIES.items(), strict=False):
                record = selected[case]
                e["request"]["text"] = query
                e["expected_parent_ids"] = [record["parent_id"]]
                e["source_pages"] = {"start": record["page_start"], "end": record["page_end"]}
                e["query_origin"] = "assistant_authored_paraphrase_from_source"
        extras = []
        if corpus == "fault_standards":
            extras = [
                (next(r for r in records if r["chart_id"] == identifier), q)
                for identifier, q in EXTRA_CHARTS.items()
            ]
        if corpus == "policy_terms":
            for number in [1, 4, 21, 23, 24, 34]:
                record = next(
                    r
                    for r in records
                    if r["clause_type"] == "rider" and r["rider_number"] == str(number)
                )
                extras.append(
                    (
                        record,
                        f"삼성화재 개인용 약관의 {record['title']}에서 보상 범위와 예외를 확인할 근거를 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
                    )
                )
        existing = {tuple(r["expected_parent_ids"]) for r in rows}
        for record, query in extras:
            if (record["parent_id"],) in existing:
                continue
            e = {
                **rows[0],
                "request": dict(rows[0]["request"]),
                "expected_parent_ids": [record["parent_id"]],
            }
            e["example_id"] = f"{corpus}-{len(rows) + 1:03}"
            e["request"]["text"] = query
            e["source_pages"] = {"start": record["page_start"], "end": record["page_end"]}
            e["query_origin"] = "authored_from_source"
            rows.append(e)
        for e in rows:
            parent = e["expected_parent_ids"][0]
            record = lookup[parent]
            evidence = [c for c in chunks if c["parent_id"] == parent]
            assert evidence, "정답 후보를 뒷받침할 청크가 없음"
            e["source_alignment"] = {
                "status": "checked_against_extracted_source",
                "document_hash": record["document"]["content_hash"],
                "evidence_chunk_ids": [c["chunk_id"] for c in evidence],
                "source_page_start": record["page_start"],
                "source_page_end": record["page_end"],
                "checker": "assistant",
                "human_review": "pending",
            }
            e["label_status"] = "unlabelled"
            e["split"] = "unassigned"
            e["split_group_id"] = f"document-family:{record['document']['document_id']}"
            e["review_checks_pending"] = [
                "모든 관련 정답 포함 여부",
                "유사하지만 틀린 후보 판정",
                "담당자 정답 검수",
                "사고일/계약판 조건",
                "자료 계열 단위 평가 분할",
            ]
            e["notes"] = (
                "추출 원문과 근거 연결 확인. 사람이 검수한 정답이 아님. 날짜/판 적용성은 미확정. 정답 없음·범위 밖 질문은 아직 미포함. 최종 성능 평가 전 검수 필요."  # noqa: E501 -- source text or prompt kept intact
            )
            e["difficulty"] = (
                "contrastive_candidate"
                if corpus == "fault_standards" and e in rows[12:]
                else "source_grounded"
            )
            if corpus == "dispute_cases":
                chartset = set(record["chart_id_candidates"])
                e["alternative_parent_ids_to_review"] = [
                    r["parent_id"]
                    for r in records
                    if r["parent_id"] != parent and chartset.intersection(r["chart_id_candidates"])
                ]
            else:
                e["alternative_parent_ids_to_review"] = []
        path.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in rows), encoding="utf-8"
        )
        print(folder, len(rows), "source-aligned drafts")


if __name__ == "__main__":
    main()
