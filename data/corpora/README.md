# 세 코퍼스 — 김지은 담당

- `fault_standards`: 과실비율 인정기준·도표·수정요소·관계·참고 판례
- `dispute_cases`: 분쟁심의 결정례·사실·쟁점·결정 이유
- `policy_terms`: 약관·보상 지침·담보·조항·예외 (1차 확장)

각 `manifest.example.json`은 원문/이용조건/검수/임베딩 메타데이터 양식이고, `manifest.json`이 실제 세 문서의 값이다.
manifest에는 출처·판·적용기간·content_hash·parser_version·이용조건 검토(`usage_review`)·검수자(`reviewed_by`)만 두고 원문은 넣지 않는다.
DB `corpus_documents`에는 manifest와 같은 값을 적재한다(`docs/contracts/corpus-index.md`).
파싱·청킹·문서 임베딩은 `app/ingestion/`, 온라인 질의 임베딩·검색은 옥석현의 `app/retrieval/`를 사용한다.
문서 모델/차원/버전은 옥석현과 합의한다. 약관 적용판은 사고 일자만으로 결정하지 않고 계약 자료를 확인한다.
도표 숫자·가감치·조건·중복/배타는 원문과 대조한 별도 구조 레코드로 만든다.

원본 PDF는 비공개 Storage 버킷, OCR 결과·청크·임베딩·평가셋은 `data/private/`(git 무시)에 둔다. 공개 저장소에 넣지 않는다.
파싱·청킹·임베딩은 끝났고, 비공개 버킷 업로드·DB 적재·원문 이용 허락 확인은 남아 있다.
