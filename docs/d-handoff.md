# D 담당 인수인계 — Claude Code 작업 시작용

이 문서는 claude.ai 대화에서 정한 내용과 지금까지의 구현 상태를 정리한 것이다(2026-11-06 갱신).
Claude Code로 이어서 작업할 때 이 문서, `TEAM.md`, `docs/d-claims-reports.md`를 먼저 읽는다.

## 1. 프로젝트와 내 역할

- 서비스: 삼성화재 보상 담당자용 **사고 영상 기반 과실 검토 에이전트** (5조, 마감 2026-11-13).
  영상 → 사실 카드 → 담당자 확인 → 과실비율 판단·검증 → 검토 초안 보고서. **1차 판정 초안을 빠르게 출력**하는 것이 목적이고,
  최종 판단은 항상 담당자(마지막에 [확인 완료] 한 번).
- 나는 **D**: 공통 RAG 검색 함수, 손해액·보험금 산정, 구상 청구 패키지, 검토 초안 보고서, 통합 창구
  (`app/main.py`, `app/schemas/common.py`, DB 마이그레이션, 인증). 담당 경로는 `TEAM.md`의 D 표.
- 약관 PDF 파싱·청킹·문서 임베딩은 **C**. 질의 임베딩과 온라인 검색 실행(`app/retrieval/`)은 **D**.

## 2. 대화에서 확정한 결정

| 주제 | 결정 |
| --- | --- |
| 범위 | 선행 MVP는 과실비율 보고서(1~7번). 1차 확장은 **자차 + 약관 + what-if + 구상**(8~9번). 2차에 대물·대인·자손·자상·이륜차 |
| 처리 흐름 (11-06 확정) | ① 판단 결과(draft_ready) → 보고서 생성과 동시에 산정 ② 견적서(PDF·수동)가 들어오면 자동 재산정 → 8·9번까지 채운 새 버전 ③ 담당자 [확인 완료] 한 번 → 확인 기록 + 구상 청구 패키지. **단계별 승인 없음** |
| 서류 입력 | 프로토타입은 담당자가 서식 견적서 PDF 업로드 → 자동 추출 값을 바로 사용(확인 완료에서 확인). OCR 값은 담당자 확인 후 사용. 실제 도입 시 입력부만 사내 견적 시스템 연동으로 교체 |
| 서류 없을 때 | 과실비율까지만 진행, 금액은 `null`(0 아님). 견적이 들어오면 금액부터 재산정, 판단·비전은 다시 돌리지 않음 |
| 추정치 참고 표시 (11-06) | 설계 초안의 '견적서 없을 때 평균 수리비 추정치 참고 표시'는 **1차에서 제외**. 견적서가 없으면 손해액은 항상 `null`(미정). `DamageSeverityDoc`·평균 수리비 테이블 삭제. `estimated` 값은 팀 공통 계약이라 **값은 유지, D는 만들지 않음** |
| 숫자 | 금액은 전부 코드가 계산. LLM은 면책·감액 조항 해당 여부만 판단하고, 근거 조항 ID가 없으면 '확인 필요' |
| 구상 (11-06) | 9번 자동: 예정액 = 산정 보험금 × 상대 과실, 청구액 = 예정액(자동), 회수액 `null`. 고객 자기부담금 검토액은 9번 내부 참고 |
| 확인 완료 (11-06 갱신) | `confirm_report` 한 번. **막는 경우는 최신 버전 아님(`StaleVersion`)·`draft_ready` 아님(`ConfirmBlocked`) 두 가지뿐**. 그 밖에는 항상 확인 기록을 남기고, 청구서 조건이 안 맞으면 `package=None` + `claim_blocked_reasons`(사유 전부) + `claim_blocked_reason`(첫 번째). 확인 후 금액이 바뀌면 새 버전 → 다시 확인 |
| 청구서 없음 사유 (11-06) | 해당 사유를 전부 돌려줌. 견적서 없음 → "견적서 필요 — 견적서 추가 후 청구서 생성" / `not_payable` → "지급 불가 — 구상할 보험금 없음" / 보험금 0원 → "자기부담금이 손해액 이상이라 지급액 0원 — 구상할 보험금 없음"(산정 사유와 같은 문구) / 금액 `null`(매핑 불명·가입금액 없음 등) → "보험금 산정 불가 — {사유}" / 상대 과실 0%(협의 0% 포함) → "상대 과실 0% — 구상 대상 아님" |
| 기본 가정: 계약 (11-06) | 계약 서류가 없으면 자차 가입·보험기간 안·운전자 범위 안으로 가정, 전손·가입금액 한도 검사 생략. 8번 '산정 전 검증'에 "기본 가정(미입력)". 계약 서류가 들어오면 그 값으로 재산정(자차 미가입 등이면 `not_payable`) |
| 기본 가정: 자기부담금 (11-06) | 계약에 자기부담금 값이 없으면 `rules.py` 기본 규칙. 8번에 "자기부담금: 기본 규칙 적용(20%, 최저 20만·최고 50만)". 계약 값이 있으면 그 값(FE 입력 칸 없음) |
| 기본 가정: 면책 (11-06) | FE 체크박스(무면허·음주·고의·해당 없음). 미입력 → "면책 사실 미입력 — 해당 없음으로 가정", 셋 다 False → "담당자 확인 — 해당 없음", 하나라도 True → `not_payable`(근거 "약관 조항 미연결 — 담당자 입력 기준"). 지식베이스 연결 후 사실이 없어 unknown → "면책 확인 필요 — 사실 미입력" 표시만. 면책 사실이 나중에 들어오면 재산정 → 새 버전 |
| 가정 표시 (11-06) | 가정이 있어도 확인 완료·청구서 생성은 막지 않음. 산정 `assumptions`, 8번 `기본 가정`, 청구서 `assumptions`·HTML에 표시. 가정(자기부담금 기본 규칙 제외)이 있으면 산정 상태는 `confirmed`가 아님 |
| `case_facts` 검사 (11-06) | 키는 무면허·음주·고의만, 값은 True/False/None만. 그 밖이면 `ValueError`(서류도 저장하지 않음) |
| 견적서 증빙 (11-06) | `upload_estimate`로 올린 견적서 PDF는 증빙(`doc_type="repair_estimate"`)으로 자동 등록. 구상 청구서에는 우리 차 견적서의 증빙만 들어가고, 상대 차 견적서·진단서·진료비 영수증은 빠진다(11-13, #198 `claim_evidence`) |
| 예시 규칙 (11-06) | 확인 완료·패키지를 막지 않음. 패키지 JSON·HTML에 "예시 규칙 기준 — 약관 검수 전 초안" 표시. 산정 상태는 `needs_contract_review` 유지 |
| 지급 내역 (11-06) | **완전 제거**(선택 기능도 아님). 구상 청구서는 사건 1건 = 1행, 증빙은 사건에 바로 연결 |
| A-RAG | 공식 판단에는 쓰지 않고(재현성·비용), 질의응답 에이전트와 비교 실험(S2 비교)에만 아이디어 활용 |
| 형태 | 사내 웹 서비스(앱 아님). 담당자는 PC에서 영상·초안을 나란히 봄 |
| 사고 유형 (11-05) | 1차 확장 산정·구상은 **차대차 1:1 사고만** 대상. 다자·단독 사고는 범위 밖 (지금 코드에서 따로 막지는 않음) |
| 지급 0원 (11-05) | 자기부담금 ≥ 손해액이면 지급 0원. 산정 상태는 그대로 두고 사유 "자기부담금이 손해액 이상이라 지급액 0원 — 구상할 보험금 없음"(11-06 문구 통일) |
| 견적 혼재 (11-05) | 확정 견적보다 나중에 접수된 미확인(OCR) 견적이 있으면 금액 `null`·`needs_documents`로 보류, 사유 "더 최신 견적 확인 필요". 접수 시각은 실제 시각으로 비교(시간대 없으면 KST) |
| 상대 과실 0% (11-05) | 구상 패키지 생성 차단 — "상대 과실 0% — 구상 대상 아님" |
| 협의 과실 (11-05, 11-06 갱신) | `set_agreed_fault`로 입력, **이유 필수**. 9번(`RecoveryAmounts`)에 저장하고 이후 재산정에도 이어 씀. `clear=True`(이유 필수)로 판단 과실로 되돌림. 패키지 과실은 항상 9번 과실 |
| 면책 근거 (11-09) | **제23조 조항 ID 직접 조회만** 판정기에 넘긴다(벡터 보조 검색 없음). 조회 결과는 C의 `validate_hits`로 검증한다. 조항 ID는 `rules.py`의 삼성화재 2026.11.11판 제23조(고의 제1호 p.67, 무면허·음주 제14호 p.68). 조항이 색인에 없거나 검증에 실패하면 담당자 입력을 따르고, 입력이 없으면 "면책 확인 필요 — 조항 미확인"(막지 않음). `configure(fetch_clauses=fetch_by_parent)`로 연결 |
| 담당자 입력 우선 (11-09) | **담당자 입력이 LLM보다 우선.** `True` → LLM과 상관없이 `not_payable`, LLM 인용 조항을 근거로 표시. `False` + LLM pass·unknown → 통과. `False` + LLM fail → 통과 + 8번 경고 "LLM은 면책 가능성 제시(조항 …) — 담당자 확인". 입력 없음 → LLM 결과를 따르고 unknown이면 표시만(막지 않음) |
| 체크박스 뜻 (11-09) | 무면허·음주는 "운전자(계약자·기명피보험자·30일 초과 임차인·동거 친족)의 무면허/음주". 약관 제23조 제14호의 대상자에 맞춘 뜻. 마약·약물 운전은 추가 후보(지금 하지 않음) |
| 지식베이스 연결 (11-09) | **통합(`app/main.py`) 시 `configure_with_knowledge_base()` 이 함수 하나만 호출**한다. 실제 색인 적재 + `OpenAIEmbedder`(C 도구 검색용) + `fetch_clauses` + `llm_judge`. 키·모델·패키지가 없으면 `RetrievalNotConfigured`를 내고 아무것도 바꾸지 않는다. 시험 환경에서 연결 1.8초, 메모리 약 230MB |
| 면책 (11-05) | 약관 검색 미연결·미구현이면 담당자 입력(`case_facts`: 무면허·음주·고의)으로 판정. `decided_by='handler'`, "약관 조항 미연결 — 담당자 입력 기준" 표시. 미입력 항목은 11-06부터 `assumed`(해당 없음으로 가정) → `needs_contract_review` |
| 8번 부담 요약 (11-05) | 고객 부담(자기부담금) · 보험사 지급 · 상대방 부담 예상액(보험금 × 상대 과실, **지급 전 예상치**) · 보험사 최종 부담 예상액. 모르면 `null`, 상대 과실 0%면 "구상 대상 아님". 9번 구상 계산과는 별개 |
| 구상 청구서 (11-05) | 패키지에는 **청구 금액만**(`ClaimAmounts`). 고객 자기부담금 검토액은 보고서 9번(내부 참고)에만 |
| 견적서 PDF (11-06) | 자동차관리법 시행규칙 별지 제89호의3 서식 텍스트 PDF 자동 추출. 총액 인식 실패 → `null`, 합계 불일치 → 계산 + "확인 필요" 사유. 8번에 손해액 출처(일련번호) |

## 3. 이 저장소의 규칙 (팀원 초안 기준)

- 계약은 **TypedDict**(표준 라이브러리). Pydantic·OpenAPI는 후속. `make check`는 외부 패키지 없이 통과해야 한다.
- 판정 상태 `draft_ready` / `needs_confirmation` / `deferred`. 산정 상태 `needs_documents` /
  `needs_contract_review` / `not_payable` / `confirmed`, 그리고 `estimated` — **값은 유지, D는 만들지 않음**(11-06 참고값 기능 제거, 계약 호환용).
- 공통 계약을 바꾸면 BE 타입 → `make contracts` → FE 타입·견본을 같은 변경 단위로 맞춘다.
- 브랜치 `<타입>/#<번호>`, PR 제목 `[feat/#12] ...`, 본문 `close #12` + 검증 결과.
- 키·실제 사건·원본 영상·이용조건 미확인 PDF는 커밋하지 않는다.

## 4. 구현 완료 (D 1차 확장)

2026-11-06 기준. 브랜치 `feat/d-claims-reports` (push 전). 확정 흐름은 `docs/d-claims-reports.md` 첫 절 참고.

| 파일 | 상태 |
| --- | --- |
| `app/schemas/reports.py` | 서류(견적·계약·증빙)·산정 전 검증·보고서 항목·구상 계약. 지급 타입 삭제, 패키지는 사건 1행·확인 기록·예시 규칙 표시 |
| `app/calculation/rules.py` | 규칙 값 — **전부 예시**. 예시 규칙이면 산정이 `confirmed`로 나오지 않음(패키지는 '약관 검수 전 초안' 표시로 생성) |
| `app/calculation/damages.py` | `calculate_damages` — 최신 확정 견적 사용, PDF 자동 추출 견적은 바로 사용, 미확인 최신 견적이면 보류 |
| `app/calculation/estimate_reader.py` | 서식 견적서 PDF 자동 추출 (pdfplumber 선택 의존성) |
| `app/calculation/exclusions.py` | 약관 검색 → 플래그/LLM 판정, 검색 없으면 `review_exclusions_from_facts`(담당자 입력) |
| `app/calculation/insurance.py` | `calculate_insurance` — `draft_ready` 판단으로 바로 산정(승인 단계 없음), 지급 0원 사유 |
| `app/claims/package.py` | `compute_recovery`(9번 자동), `check_claimable`, `build_claim_package`(사건 1행, 상대 과실 0% 차단, 예시 규칙 표시) |
| `app/reports/assembler.py` | `assemble_review` (1~9번), 8번 `부담 요약`·`손해액 출처`, 9번 자동 구상·`협의 과실 / 변경 이유` |
| `app/reports/render.py` | HTML·Markdown·JSON, PDF(WeasyPrint 있을 때만). '확인 완료' 표시, 패키지 사건 1행 |
| `app/reports/repository.py` | 메모리 저장소 (Supabase로 교체 예정). 협의 과실 보관 |
| `app/api/reports.py` | `submit_judgment` → `add_documents`/`upload_estimate`(자동 재산정) → `set_agreed_fault`(선택) → `confirm_report`(확인 완료 + 패키지) |
| `tests/test_d_reports.py` | 기본 동작 21개 (예시 규칙 패키지 표시 포함) |
| `tests/test_d_edge_cases.py` | 경계 사례 40개 — 가입금액 초과, 자기부담금 ≥ 손해액, 견적 혼재, 과실 0:100·100:0, 원 미만 버림, 협의 과실·되돌리기, 면책 담당자 입력, 8번 부담 요약, 패키지 청구 금액·증빙 경고 |
| `tests/test_d_estimate_pdf.py` | 견적서 PDF 17개 (pdfplumber 없으면 10개 건너뜀) — 견적서 증빙 자동 등록 포함 |
| `tests/test_d_flow.py` | 확정 흐름 13개 — 바로 산정, 견적서 필요, 확인 완료·패키지, 최신만, 같은 입력 같은 패키지, 확인 후 재산정, 증빙만 추가, 추가 확인·보류, 상대 과실 0%, 예시 규칙 |
| `tests/test_d_assumptions.py` | 기본 가정 13개 — 견적서만으로 산정·청구서, 면책 '해당 없음' 확인, 청구서 없음 사유 5종·사유 전부 반환, `case_facts` 검사, 금액 null 사유, 확인 후 견적 추가·재확인, 계약 값 우선, 지식베이스 unknown 표시 |
| `tests/test_d_exclusion_clauses.py` | 면책 근거 33개 — 제23조 직접 조회만, validate_hits 검증·실패 처리, 조항 없음·담당자 입력, 담당자 입력 우선, 실제 제23조 청크 |
| `tests/test_d_knowledge_base.py` | 운영 연결·LLM 호출부 10개 — 한 번에 설정, 실패 시 변경 없음, 같은 프롬프트 재호출 없음, 키 미노출(가짜 클라이언트) |
| `scripts/try_exclusion_judge.py` | 실제 모델 면책 판정 시험(최대 4번), `--final`은 무면허·음주 True 최종 결과(최대 4번). CI 제외 |
| `scripts/demo_d.py` | 판단 → 견적 없음 → 견적서 PDF → 보고서 전체 → 확인 완료 → 패키지 시연 |

검증: check 통과, 테스트 **147개**(`test_d_retrieval*` 제외) 모두 통과. Windows에는 `make`가 없고 `python3`가 스토어 바로가기라
`py -3.12 scripts/check_scaffold.py`, `py -3.12 -m unittest discover -s tests -t .`로 직접 실행했다.

## 5. 지금 집중할 범위

**손해액·보험금 산정, 검토 초안 보고서, 구상 청구 패키지 세 파트만** 먼저 마무리한다.
공통 검색(`app/retrieval/`), 통합(`app/main.py`, 인증), DB 마이그레이션은 **나중**에 하고 지금은 건드리지 않는다.

11-06에 흐름을 확정했다(단계별 승인 제거, 지급 내역 제거, 확인 완료 한 번). 세 파트의 남은 작업 (우선순위 순)

1. **push·PR** — `feat/d-claims-reports` 확인 후 push, PR 본문에 `close #<번호>` + 검증 결과(check, make lint, make test).
   저장소 규칙의 브랜치 이름(`<타입>/#<번호>`)과 다르니 이슈 번호를 확인해 PR 제목 `[feat/#번호] ...`로 맞춘다.
2. **FE 전달** — `docs/d-claims-reports.md`의 'FE 타입 변경' 표. 특히 삭제된 API(`approve_report` 등)·`PaymentDoc`,
   `ClaimRow`·`ClaimPackage` 변경, `approval_status`의 의미가 '확인 완료'로 바뀐 점.
3. **B 필드** — `JudgmentResult`에 `base_fault_A`, `modifiers`, `alternatives` 추가를 요청.
   그 전까지 보고서 3~5번은 '판단 결과에 없음'으로 비어 있는 것이 정상이다.
4. **B와 확인 지점 맞추기** — B 쪽에 별도 확인 단계가 있으면, 담당자 확인이 D의 `confirm_report` 한 번으로 끝나는지 팀과 확인.

약관·실무 검수를 받으면: `app/calculation/rules.py`의 값과 조항 ID만 바꾼다(버전이 `example-`가 아니면 '약관 검수 전 초안' 표시가 빠진다).

나중에 할 D 작업(지금 하지 않음): 공통 검색 `app/retrieval/`, FastAPI 통합 `app/main.py`·인증, Supabase 테이블·RLS.

## 6. Claude Code 첫 요청 예시

```
docs/d-handoff.md, TEAM.md, docs/d-claims-reports.md를 읽고 D 담당 맥락을 파악해 줘.
나는 손해액·보험금 산정, 검토 초안 보고서, 구상 청구 패키지 세 파트만 작업할 거야.
app/retrieval, app/main.py, supabase는 건드리지 마.
make check와 make test로 현재 상태를 확인하고, 5번 '세 파트의 남은 작업' 1번(테스트 보강)부터
계획을 세워 줘. 코드 수정 전에 계획을 먼저 보여 줘.
```
