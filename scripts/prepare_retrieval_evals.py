"""세 코퍼스의 근거 연결 평가셋 초안 생성. 검수 라벨은 자동 부여하지 않는다."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def read(path):
    return [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines() if s.strip()]


def main():
    sources = [
        ("fault_standards", "01_fault_standards", "processed", "charts"),
        ("dispute_cases", "02_dispute_cases", "processed", "cases"),
        ("policy_terms", "03_policy_terms", "processed", "clauses"),
    ]
    questions = {
        "차1-1": "녹색신호에 직진한 A와 적색신호에 직진한 B가 교차로에서 충돌했다. 관련 인정기준을 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
        "차1-2": "A가 정상 신호에 진입했지만 교차로를 벗어나기 전 신호가 바뀌었고, 녹색신호로 들어온 B와 충돌했다. 어떤 기준을 확인해야 하나?",  # noqa: E501 -- source text or prompt kept intact
        "차1-3": "황색신호에 직진한 차량과 적색신호에 직진한 차량이 충돌한 사고의 기준을 찾아줘.",
        "차1-4": "양쪽 차량이 모두 적색신호에 직진하다 교차로에서 충돌했다. 해당 도표를 찾아줘.",
        "차1-5": "적색 점멸신호 방향의 직진 차량과 황색 점멸신호 방향의 직진 차량이 충돌한 경우의 기준은?",  # noqa: E501 -- source text or prompt kept intact
        "차2-1": "적색신호에 직진한 A와 맞은편에서 녹색 화살표 신호에 좌회전한 B의 충돌에 관한 근거를 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
        "차12-1": "폭이 같은 도로의 교차로에서 우측도로 직진 차량과 좌측도로 직진 차량이 충돌했다. 선진입 여부를 구분하는 기준을 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
        "차13-3": "오른쪽의 좁은 도로에서 우회전한 차량과 왼쪽의 넓은 도로에서 직진한 차량이 충돌했다. 관련 기준은?",  # noqa: E501 -- source text or prompt kept intact
        "차16-1": "왼쪽 도로에서 직진한 차량과 오른쪽 도로에서 좌회전한 차량이 충돌했다. 관련 도표를 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
        "차41-1": "앞차와 뒤차가 같은 방향으로 주행하다 뒤차가 앞차를 추돌했다. 앞차가 정지한 직후의 경우도 포함한 기준을 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
        "차43-4": "정체 차로에서 대기하던 차량이 옆의 직진 차로로 갑자기 변경해 직진 차량의 측면과 충돌했다. 관련 기준을 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
        "차52-1": "앞서 정차한 차량의 문이 열리면서 뒤에서 직진하던 차량과 접촉했다. 문 열림 사고 기준을 찾아줘.",  # noqa: E501 -- source text or prompt kept intact
    }
    term_questions = {
        "1": "이 약관에서 사용하는 가지급금과 무면허운전 등 용어의 정의를 찾아줘.",
        "3": "대인배상Ⅰ에서 보상하는 손해의 범위를 확인할 조항은?",
        "5": "대인배상Ⅰ에서 보상하지 않는 손해를 정한 조항을 찾아줘.",
        "6": "대인배상Ⅱ와 대물배상에서 보상하는 손해를 설명하는 조항은?",
        "8": "대인배상Ⅱ와 대물배상의 면책을 확인하려면 어떤 조항을 읽어야 하나?",
        "12": "자기신체사고에서 보상하는 손해를 확인할 조항을 찾아줘.",
        "17": "무보험자동차에 의한 상해에서 보상하는 손해의 근거 조항은?",
        "21": "자기차량손해에서 보상하는 손해의 범위를 정한 조항을 찾아줘.",
        "23": "자기차량손해에서 보상하지 않는 손해를 확인할 조항을 찾아줘.",
        "24": "자기차량손해 지급보험금 계산과 자기부담금 관련 내용을 확인할 조항은?",
        "34": "보험회사가 보험금을 지급한 뒤 갖는 대위 권리에 관한 조항을 찾아줘.",
        "55": "약관 뜻이 불명확하거나 고객에게 불리한 내용을 해석하는 원칙은 어디에 있는가?",
    }
    for corpus, folder, version, stem in sources:
        base = ROOT / "data/private" / folder
        records = read(base / version / f"{stem}.jsonl")
        rows = []
        selected = (
            [r for r in records if r["chart_id"] in questions]
            if corpus == "fault_standards"
            else [records[round(i * (len(records) - 1) / 11)] for i in range(12)]
            if corpus == "dispute_cases"
            else [
                r
                for r in records
                if r["clause_type"] == "general_article" and r["article_number"] in term_questions
            ]
        )
        for i, r in enumerate(selected, 1):
            doc = r["document"]
            question = (
                questions[r["chart_id"]]
                if corpus == "fault_standards"
                else "다음 사고와 유사한 심의사례 및 판단 이유를 찾아줘: "
                + " ".join(r["facts"].split())
                if corpus == "dispute_cases"
                else term_questions[r["article_number"]]
            )
            row = {
                "example_id": f"{corpus}-{i:03}",
                "request": {
                    "corpus": corpus,
                    "text": question,
                    "accident_date": "",
                    "filters": {},
                    "top_k": 3,
                },
                "expected_parent_ids": [r["parent_id"]],
                "expected_edition": doc["edition"],
                "split_group_id": f"document-family:{doc['document_id']}",
                "split": "unassigned",
                "label_status": "unlabelled",
                "synthetic": True,
                "source_pages": {"start": r["page_start"], "end": r["page_end"]},
                "source_document_id": doc["document_id"],
                "query_origin": "source_copy_sanity_only"
                if corpus == "dispute_cases"
                else "authored_from_source",
                "notes": "검수 전 초안. 복수 정답·유사 오답·질의 조건·적용판·사고 일자 확인 필요. 최종 성능 평가에 사용 금지.",  # noqa: E501 -- source text or prompt kept intact
            }
            rows.append(row)
        target = base / "evaluation.jsonl"
        if target.exists():
            previous = read(target)
            if any(
                r.get("dataset_version") or r.get("label_status") == "machine_reviewed"
                for r in previous
            ):
                print(f"{folder}: 릴리스 평가셋 유지 (초기 양식 생성 건너뜀)")
                continue
            if any("source_alignment" in r for r in previous):
                print(f"{folder}: 보완된 평가셋 유지 (초기 생성 건너뜀)")
                continue
            if any(r["label_status"] == "reviewed" for r in previous):
                raise ValueError(f"검수 라벨을 덮어쓸 수 없습니다: {target}")
        target.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
        )
        print(f"{folder}: {len(rows)} draft evaluation examples")


if __name__ == "__main__":
    main()
