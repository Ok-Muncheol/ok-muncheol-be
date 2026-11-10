# D 구현 메모 — 손해액·보험금, 검토 초안 보고서, 구상 청구 패키지

2026-11-06 기준. 공통 RAG(`app/retrieval/`)와 통합 기반(`app/main.py`, DB, 인증)은 아직 미구현이다.

## 확정 흐름 (2026-11-06 팀 확정)

이 솔루션은 **1차 판정을 빠르게 출력**하는 것이 목적이다. 단계별 승인은 없고, 담당자 확인은 마지막 한 번이다.

```
submit_judgment(draft_ready) ─▶ 보고서 v1 (바로 산정, 8·9번 자동. 견적서 없으면 금액 null)
        │
add_documents / upload_estimate ─▶ 자동 재산정 → 입력이 바뀌면 새 버전 (같으면 그대로)
set_agreed_fault (선택)          ─▶ 협의 과실 반영한 9번으로 새 버전
        │
confirm_report(actor, case_id, version) ─▶ 확인 기록 + 구상 청구 패키지(JSON·HTML)
```

| 단계 | 규칙 |
| --- | --- |
| 1. 판단 → 보고서 | `draft_ready`면 승인 없이 바로 산정해 8·9번까지 채운다. 추가 확인(`needs_confirmation`)·보류(`deferred`)면 산정·패키지를 만들지 않는다 |
| 2. 견적서 | PDF(`upload_estimate`) 또는 수동(`add_documents`). 없으면 금액 `null`. 추가하면 자동 재산정 → 새 보고서 버전 |
| 9번 구상 | 예정액 = 산정 자차 보험금 × 상대 과실(원 미만 버림). 청구액은 예정액으로 자동. 회수액 `null`. 고객 자기부담금 검토액(자기부담금 × 상대 과실)은 9번 내부 참고 |
| 기본 가정 | 견적서만 넣어도 산정한다. 계약 없음 → 자차 가입·보험기간 안·운전자 범위 안(전손·가입금액 한도 검사 생략), 자기부담금 → `rules.py` 기본 규칙, 면책 사실 미입력 → 해당 없음. 8번·청구서에 표시하고 막지 않는다 |
| 3. 확인 완료 | `confirm_report` 한 번. 막는 경우는 **최신 버전 아님(`StaleVersion`)·`draft_ready` 아님(`ConfirmBlocked`)** 두 가지뿐. 그 밖에는 확인 기록(`approval_status="approved"`, `approved_by`, `approved_at`)을 남기고, 조건이 맞으면 청구서(패키지) 생성 |
| 청구서 없음 | 확인 완료는 되고 `package=None` + `claim_blocked_reasons`(해당 사유 **전부**, 아래 순서) + `claim_blocked_reason`(첫 번째 값). `not_payable` 뒤에 견적서, 산정 불가, 0원, 상대 과실 0% 순서. 견적서 없음 → "견적서 필요 — 견적서 추가 후 청구서 생성", `not_payable` → "지급 불가 — 구상할 보험금 없음", 보험금 0원 → "자기부담금이 손해액 이상이라 지급액 0원 — 구상할 보험금 없음", 금액 `null`(매핑 불명·가입금액 없음 등) → "보험금 산정 불가 — {사유}", 상대 과실 0%(협의 0% 포함) → "상대 과실 0% — 구상 대상 아님" |
| 예시 규칙 | 막지 않는다. 산정 상태는 `needs_contract_review`로 두고, 패키지 JSON·HTML에 "예시 규칙 기준 — 약관 검수 전 초안"(`rule_notice`) 표시 |
| 확인 후 변경 | 서류 추가로 금액·검증 결과가 바뀌면 새 버전(확인 풀림) → 다시 확인 완료. 증빙만 추가하면 새 버전 없이 같은 버전을 다시 확인해 패키지를 갱신 |
| 같은 입력 | 같은 서류 재전송은 새 버전 없음. 같은 입력으로 다시 확인하면 같은 패키지(`idempotency_key`) |
| 협의 과실 | `set_agreed_fault(..., agreed_counterparty_fault, fault_change_reason)` — 이유 필수. `clear=True`(이유 필수)로 판단 과실로 되돌림. 이후 재산정에도 이어 쓰고, 패키지 과실은 항상 9번 과실 |
| 지급 내역 | **없음**. `PaymentDoc`, 지급 기반 구상, 지급 건별 청구 행은 삭제. 증빙은 사건에 바로 연결 |

## 구현한 것

| 파일 | 내용 |
| --- | --- |
| `app/schemas/reports.py` | 서류(견적·계약·증빙)·산정 전 검증·보고서 항목·구상 패키지 계약. 추가 필드는 `NotRequired` |
| `app/calculation/rules.py` | 자기부담금·전손·조항 ID 규칙. **전부 예시**(`example-` 버전) |
| `app/calculation/damages.py` | 확정 견적 우선, OCR 미확인은 `null`, 견적서 없으면 항상 `null`(평균 수리비 참고값 없음) |
| `app/calculation/estimate_reader.py` | 서식(별지 제89호의3, `data/forms/car_estimate_form.pdf`) 견적서 텍스트 PDF에서 총액·일련번호·발급일·견적구분·부품·공임·부가가치세 추출(`read_by="pdf_extract"`, 담당자 확인 없이 계산에 사용). pdfplumber 선택 의존성 |
| `app/calculation/exclusions.py` | **면책 근거 = 조항 ID 직접 조회만**(C의 `validate_hits`로 검증, 벡터 검색 없음). 조항이 색인에 없거나 검증에 실패하면 담당자 입력을 따르고, 입력이 없으면 "면책 확인 필요 — 조항 미확인"(막지 않음). |
| `app/calculation/insurance.py` | 판단 과실(`draft_ready`) 기반 자차 보험금. 승인 단계 없음. 계약·보험기간·운전자 범위·매핑·면책 확인 후 코드 계산 |
| `app/claims/package.py` | `compute_recovery`(9번 자동), `check_claimable`(확인 완료 조건), `build_claim_package`(사건 1행, 예시 규칙 표시, 멱등 키) |
| `app/reports/assembler.py` | 템플릿 1~9항목 조립. 확인/보류 시 4~6 수치 비움, 8~9는 `draft_ready` 판단으로 바로 채움 |
| `app/reports/render.py` | HTML·Markdown·JSON, PDF(WeasyPrint 설치 시). 확인 전 '검토 초안', 예시 규칙 패키지는 '약관 검수 전 초안' 표시 |
| `app/reports/repository.py` | 메모리 저장소(사건·보고서 버전·서류·패키지·협의 과실). Supabase로 교체 예정 |
| `app/api/reports.py` | `submit_judgment` → `add_documents`/`upload_estimate`(견적서 PDF를 증빙으로 자동 등록) → (`set_agreed_fault`) → `confirm_report`, `get_report` |

## 상태 규칙

- 산정 상태: `confirmed`는 **검수된 규칙 + 확정 견적 + 모든 산정 전 검증 통과(가정 없음)**일 때만. 예시 규칙이나 기본 가정(계약·면책 미입력)이 있으면 `needs_contract_review`(확인 완료·청구서는 막지 않고 표시만). 자기부담금 기본 규칙은 규칙 값이라 상태를 낮추지 않는다(표시만).
- 미확정 금액은 0이 아니라 `null`. 면책 입력이 없으면 해당 없음으로 가정(`assumed`, 금액은 계산).
- 서식 견적서 PDF는 자동 추출 값을 바로 쓴다(담당자 확인 완료에서 확인). 총액을 못 읽으면 금액 `null`, 부품+공임+부가가치세 ≠ 총액이면 계산은 하고 "견적서 합계 불일치 — 확인 필요" 사유를 남긴다. OCR(`read_by="ocr"`) 견적은 여전히 담당자 확인 전 `null`.
- 판단·비전은 서류 추가로 다시 돌리지 않는다. 금액부터 재산정한다.
- 증빙이 없으면 패키지 `missing_evidence`에 경고만 남긴다(생성은 막지 않음).

## 다른 담당과 맞출 것

| 대상 | 요청 |
| --- | --- |
| B | `JudgmentResult`에 `base_fault_A`, `modifiers`(modifier_id·name·applies_to·delta·decision·evidence_fields), `alternatives`(chart_id·reason) 추가 제안. 없으면 보고서 4·5번과 3번 차순위가 비어 있다 |
| C | `policy_terms` 검색 결과의 `chunk.parent_id`를 조항 ID로 사용. 면책 근거는 `rules.py`의 조항 ID로 직접 가져옵니다(`fetch_by_parent`). `terms_edition`(계약 적용판)을 받아 주세요 |
| A | `FactCard.party_mapping.insured_party_id`가 도표 A/B 중 하나와 일치해야 산정이 확정된다 |

## FE 타입 변경

| 대상 | 변경 |
| --- | --- |
| `confirm_report` 결과 (11-06 갱신) | 항상 `{report, package, html, claim_blocked_reasons, claim_blocked_reason}`. 청구서를 만들면 `package`·`html` 채움, `claim_blocked_reasons: []`, `claim_blocked_reason: null`. 못 만들면 `package: null`, `html: null`, `claim_blocked_reasons: string[]`(해당 사유 전부, 예: 음주 + 견적서 없음 → `["지급 불가 — 구상할 보험금 없음", "견적서 필요 — 견적서 추가 후 청구서 생성"]`), `claim_blocked_reason: string`(= `claim_blocked_reasons[0]`, 기존 화면 호환). 화면에는 `claim_blocked_reasons`를 목록으로 보여 주기를 권장. 확인 기록(`report.approval_status="approved"`)은 두 경우 모두 남음. 예외는 `StaleVersion`(최신 아님)·`ConfirmBlocked`(추가 확인·보류)뿐 |
| 면책 체크박스 뜻 (11-09) | 무면허·음주 체크박스 문구: **"운전자(계약자·기명피보험자·30일 초과 임차인·동거 친족)의 무면허/음주"**. 약관 제23조 제14호의 대상자에 맞춘 뜻이다. 그 밖의 운전자라면 체크하지 않는다 |
| 담당자 입력과 LLM 판정 (11-09) | **담당자 입력이 우선한다.** `true` → LLM과 상관없이 `not_payable`, LLM 인용 조항을 근거로 표시. `false` + LLM pass·unknown → 통과. `false` + LLM fail → 통과 + 8번 경고 "LLM은 면책 가능성 제시(조항 …) — 담당자 확인"(`PreCheck.warning`, 산정 사유에도 포함). 입력 없음 → LLM 결과를 따르고 unknown이면 표시만 |
| 면책 체크박스 (11-06) | 4개: 무면허·음주·고의·해당 없음 → `add_documents(..., case_facts=...)`. 키는 `무면허`·`음주`·`고의`만, 값은 `true`/`false`/`null`만(그 밖이면 `ValueError`, 서류도 저장하지 않음). 입력했던 값을 미입력으로 되돌리려면 그 키에 `null`. **아무것도 선택 안 함** → `case_facts`를 보내지 않음(미입력: "면책 사실 미입력 — 해당 없음으로 가정"). **'해당 없음'** → `{"무면허": false, "음주": false, "고의": false}`("담당자 확인 — 해당 없음"). **항목 선택** → 선택한 항목 `true`, 나머지 `false`(→ `not_payable`, 청구서 없음 "지급 불가 — 구상할 보험금 없음"). '해당 없음'과 항목은 함께 선택할 수 없게 |
| `PreCheck.result` (11-06) | `"assumed"` 추가 = 기본 가정(미입력). 화면 결과 문구 "기본 가정(미입력)". 확인 완료·청구서를 막지 않음 |
| `CalculationResult` (11-06) | `NotRequired` `assumptions: list[str]` — 기본 가정 문구(계약·자기부담금·면책). 보고서 8번 본문에도 `기본 가정` 키로 같은 목록 |
| `ClaimPackage` 가정 (11-06) | `assumptions: list[str]` 추가(없으면 `[]`), `basis["기본 가정"]`에도 같은 목록. HTML에 "기본 가정 N건" 표시 |
| `EvidenceDoc` 견적서 (11-06) | `doc_type`에 `"repair_estimate"` 추가 — `upload_estimate`가 견적서 PDF를 증빙으로 자동 등록(`document_id="ev-<견적 문서 ID>"`) |
| `estimated` 상태·참고값 제거 (11-06) | `CalculationStatus`·`DamageResult.status`의 `"estimated"`는 **값은 유지, D는 만들지 않음**(팀 공통 계약 호환용, 타입에서 빼지 말 것). `DamageSeverityDoc`(`kind="damage_severity"`) **삭제**. 견적서가 없으면 손해액·보험금은 항상 `null`(`needs_documents`), 평균 수리비 참고값을 보여 주는 화면·타입 제거 |
| API 흐름 (11-06) | `approve_report`·`calculate_payment`·`set_recovery`·`export_claim`·`release_claim` **삭제**. 새 함수: `confirm_report(actor, case_id, version, counterparty?)` → `{report, package, html, claim_blocked_reasons, claim_blocked_reason}`, `set_agreed_fault(actor, case_id, agreed_counterparty_fault?, fault_change_reason, clear?)`. `submit_judgment`·`add_documents`·`upload_estimate`가 산정까지 한 보고서를 돌려줌 |
| `ReviewReport` (11-06) | 필드 이름은 그대로, 의미 변경: `approval_status="approved"` = **담당자 확인 완료**, `approved_by`·`approved_at` = 확인자·확인 시각. 화면 문구는 "확인 완료" / "담당자 확인 전" |
| `PaymentDoc` (11-06) | **삭제**. 지급 내역 입력 화면·타입 제거 |
| `EvidenceDoc` (11-06) | `related_payment_id` **삭제**(증빙은 사건에 바로 연결), `doc_type`에서 `payment_record` 삭제 |
| `RecoveryAmounts` (11-06) | `review_reasons` **삭제**. `planned` = 산정 보험금 × 상대 과실, `claimed` = `planned`(자동), `recovered` = `null`. `NotRequired` `agreed_counterparty_fault: int \| None`(협의 상대 과실 %, `null`이면 협의 해제 기록), `fault_change_reason: str \| None` 유지 |
| 보고서 9번 본문 (11-06) | 키: `구상 예정액(산정 보험금 × 상대 과실)`, `청구액(예정액 자동)`, `고객 자기부담금 검토액`, `협의 과실 / 변경 이유`, `주의`. `실제 청구액`·`회수액`·`확인 필요` 키 삭제. 금액이 없으면 9번은 비어 있음(`empty_reason`: "견적서·산정 후 작성") |
| `ClaimRow` (11-06) | 사건 1건 = 1행. `payment_id`·`victim`·`paid_amount`·`paid_at` **삭제**, `insured_party_id: str \| null`·`insurance_payment: int`(산정 보험금) 추가. `claim_no`(`<case_id>-C<패키지 버전>`)·`coverage`·`counterparty_fault`·`planned_share`·`evidence_count` 유지 |
| `ClaimPackage` (11-06) | `released` **삭제** → `confirmed_by: str`, `confirmed_at: str` 추가. `rule_notice: str \| null`("예시 규칙 기준 — 약관 검수 전 초안") 추가. `counterparty: CounterpartyInfo \| null`(미입력 가능). `missing_evidence`는 경고만 |
| `ClaimAmounts` | 패키지 금액은 청구 금액만: `planned`, `claimed`(= `planned`), `recovered`(`null`), `NotRequired` `agreed_counterparty_fault`·`fault_change_reason`. 고객 자기부담금 검토액은 보고서 9번에만 |
| `CalculationResult` | `status`에 `needs_contract_review`·`not_payable` 추가, 추가 필드(`damage`, `checks`, `formulas` 등) |
| 보고서 8번 본문 | `부담 요약` 4항목: `고객 부담(자기부담금)`, `보험사 지급(자차 보험금)`, `상대방 부담 예상액(지급 전 예상치)`, `보험사 최종 부담 예상액`. 값은 `"9,500,000원"` 형식, 모르면 `null`, 상대 과실 0%면 `"구상 대상 아님"`. PDF 자동 추출 견적이면 `손해액 출처`(예: `"견적서 PDF 자동 추출 (일련번호 SYN-2026-0001)"`) |
| `RepairEstimateDoc` | `read_by`에 `"pdf_extract"` 추가. `NotRequired` `serial_no`, `issued_on`(ISO 날짜), `estimate_type`(`"insurance"`·`"general"`·`null`), `parts`·`labor`·`vat`, `extract_reasons: list[str]` |
| `DamageResult` | `NotRequired` `source_label`(손해액 출처), `document_reasons`(산정 사유로 넘긴 서류 확인 사유) |

## 추가 후보 (지금 하지 않음)

- 제23조 제14호의 **마약·약물 운전**: 지금 면책 주제와 체크박스에는 무면허·음주·고의만 있다. 같은 호에 함께 있으므로 체크박스를 넓힐 때 추가한다.

## 실무 검수 전까지 예시로 둔 것

자기부담금 비율·최저·최고, 전손 기준, 조항 ID(`rules.py`),
구상 계산 단위와 자기부담금 정산 방식, 보험사 간 구상 양식(현직자 인터뷰).
