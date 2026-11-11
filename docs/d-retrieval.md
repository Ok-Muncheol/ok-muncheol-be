# D 구현 메모 — 공통 RAG(`app/retrieval/`) 뼈대

C(김지은)의 색인 데이터와 OpenAI 질의 임베더로 동작한다. 검색 방식(벡터·BM25·RRF)은 코퍼스별로 정하고 **기본값은 세 코퍼스 모두 RRF**다. 재순위·trigram은 하지 않는다.
자동 테스트는 네트워크 없이 돌아간다(가짜 클라이언트, 청크 벡터로 자기 자신 찾기). 실제 API 호출은 `scripts/try_retrieval.py`뿐이고 CI에서 실행하지 않는다.

## 구조

```text
app/tools/{fault_standards,dispute_cases,policy_terms}.py   (C 소유, 코퍼스를 고정해 호출)
        │ SearchRequest
        ▼
app/retrieval/common.py   search(request), configure_retrieval(...)
        ├─ embedding.py   embed_query(...): 색인과 모델·차원·버전 비교 후 질의 임베딩
        └─ hybrid.py      retrieve(...): 구조 필터 → 코퍼스별 방식(벡터·BM25·RRF) → top_k → EvidenceHit
                ▼
           index.py       VectorIndex(Protocol), InMemoryIndex
                ▲
           loader.py      data/private/corpus-db-import/db_import/*.jsonl 읽기(C의 DB 적재 파일)
                ▼
           evidence_store.py  같은 행으로 C의 EvidenceStore 로컬 어댑터(APP_ENV=local, Vercel 아님)
```

| 파일 | 내용 |
| --- | --- |
| `app/retrieval/common.py` | 진입점과 교체 설정. 색인·임베더가 없으면 `RetrievalNotConfigured` |
| `app/retrieval/embedding.py` | `Embedder`(Protocol), `OpenAIEmbedder`, `FakeEmbedder`(테스트용), `embed_query`, `EmbeddingMismatch`, `EmbeddingRequestFailed` |
| `app/retrieval/hybrid.py` | `build_filter`(코퍼스별 필터), `retrieve`, `RetrievalOptions(mode=…)`, `options_by_corpus`, `keyword_corpora_for`, `reciprocal_rank_fusion` |
| `app/retrieval/index.py` | `SearchFilter`, `VectorIndex`(Protocol), `InMemoryIndex`, `cosine` |
| `app/retrieval/loader.py` | `read_corpus`(네 파일 읽기, 없으면 `RetrievalNotConfigured`), `build_index`, `load_index`, `corpus_terms_edition`, `search_chunk`(청크 + 문서 조인), `handoff_embedding_spec`(질의 임베더 기대값). 없는 문서·코퍼스 불일치·`chunk_id` 중복은 `ValueError` |
| `app/retrieval/evidence_store.py` | `LocalCorpusEvidenceStore`(C의 `EvidenceStore` 4개 메서드), `local_store_allowed`, `register_local_evidence_store` |
| `scripts/try_retrieval.py` | 시연 질문 5개, `--eval` 평가 채점, `--compare` 세 방식 비교, `--short` 짧은 질문 비교. 임베딩 API만 호출 |
| `scripts/d_short_queries.jsonl` | 담당자 말투의 짧은 질문 30개와 정답 `parent_id`. **Claude 작성, 사람 검수 아님** |

## 검색 규칙

| 항목 | 규칙 |
| --- | --- |
| 코퍼스 | 요청 코퍼스만 검색한다. 다른 코퍼스로 폴백하지 않고, 색인이 다른 코퍼스 청크를 돌려주면 `CorpusMixError` |
| 필터 완화 | 하지 않는다. 결과가 없으면 빈 목록. 예외는 아래 '기간 모름'뿐이다 |
| `fault_standards` | `effective_from ≤ accident_date ≤ effective_to`(양끝 포함, `effective_to`가 `null`이면 상한 없음). 날짜로 비교. `accident_date`가 빈 문자열이면 날짜 필터 없음(C 도구 규칙) |
| C 도구 경유 | `search_policy_terms` 등은 요청을 검사하고(`prepare_request`) 결과를 원문과 대조한다(`validate_hits`). 필터 키는 `document_id`, `edition`과 코퍼스별 허용 키만, 값은 비어 있지 않은 문자열만 받는다. **`retrieval_role`, 목록 값, `None`은 도구에서 거절**되고 공통 `search`를 직접 부를 때만 쓸 수 있다 |
| 기간 모름 | `effective_from`이 빈 문자열이면 기간 모름으로 보고 **날짜 필터를 통과**시킨다. 적재할 때 문서당 한 번 경고 로그. 형식이 틀린 날짜(`2023.6` 등)는 적재 오류 |
| `dispute_cases` | **사고 일자 필터 없음**(사고 이후 결정도 참고 대상). `chart_id`는 metadata로 거른다(1,130건 모두 값 있음) |
| `policy_terms` | **문서 적용 기간 필터 없음.** `filters["edition"]`을 `document.edition`과 그대로 비교한다. 별표는 `filters["schedule"]`(예: `"별표3"`)로 `metadata.schedule`과 비교한다 |
| 항목 사고일 범위 | 청크 `metadata`에 `accident_date_from`·`accident_date_to`가 있으면 사고 일자가 그 안이어야 한다(양끝 포함, 빈 값은 열린 끝). 코퍼스와 상관없이 `matches`에서 거른다. 지금은 약관 별표3 과실상계 두 항목(~2022-12-31, 2023-01-01~)만 값이 있다. 사고 일자가 비면 거르지 않는다. 형식이 틀린 날짜는 적재 오류 |
| 약관 판 없음 | `edition`이 없거나 `null`이면 `configure_retrieval(default_terms_edition=...)`의 기본 판을 쓴다. 기본 판도 없으면 **빈 목록 + 경고 로그**. 임베딩도 호출하지 않는다 |
| `document_id` | `chunk.document.document_id`와 비교한다(metadata가 아님) |
| 그 밖의 필터 | `chunk.metadata`와 비교한다. **필터 값이 목록이면 그중 하나와 일치**, **metadata 값이 목록이면 그 안에 포함되면 통과**. 키가 없는 청크는 제외, 빈 목록은 아무것도 통과하지 않는다 |
| 약관 주 조항 | `search()`가 자동으로 거르지 않는다. 호출하는 쪽이 `retrieval_role="primary_clause"`를 넘길 때만 거른다 |
| 조항 직접 조회 | `fetch_by_parent(corpus, parent_ids, *, terms_edition=None)`. 넘긴 순서대로, 같은 조항은 페이지 순, `score=1.0`, 임베더 불필요. 판 규칙은 `search`와 같다. 없는 `parent_id`는 건너뛴다. `retrieval_version` 예: `direct-v1:<모델>:<임베딩 버전>` |
| 결과 | 청크를 그대로 돌려준다(`document`, `page_start`, `page_end`, `parent_id`, `metadata`). `retrieval_version` 예: `vector-v1:<모델>:<임베딩 버전>` |
| 잘못된 입력 | 모르는 코퍼스, `top_k ≤ 0`, 빈 질의, `fault_standards`의 잘못된 사고 일자는 `ValueError` |
| 임베딩 불일치 | 색인과 질의의 모델·차원·버전이 다르거나 벡터 길이가 다르면 `EmbeddingMismatch` |
| 미연결 | 빈 목록이 아니라 `RetrievalNotConfigured`(`NotImplementedError` 하위). "근거 없음"과 구분하기 위해서다 |
| API 실패 | `EmbeddingRequestFailed`. 메시지에는 오류 종류만 담는다(원문에 키 일부가 섞일 수 있어 원본 예외를 연결하지 않음) |

계약에는 `page` 필드가 없고 `page_start`·`page_end`만 있다. `edition`도 계약 필드가 아니라 `filters` 키다.

필터 키 이름은 호출자가 넘긴 그대로 쓴다.

## 교체 지점

```python
from app.retrieval.common import configure_retrieval
from app.retrieval.embedding import OpenAIEmbedder
from app.retrieval.evidence_store import register_local_evidence_store
from app.retrieval.hybrid import RetrievalOptions, keyword_corpora_for
from app.retrieval.loader import build_index, read_corpus

options = RetrievalOptions()                          # 세 코퍼스 모두 RRF(기본값). 생략해도 같다
# 코퍼스별 예: {"policy_terms": RetrievalOptions(mode="bm25"), "dispute_cases": RetrievalOptions(mode="vector")}
corpus = read_corpus()                                # db_import 네 파일, 없으면 RetrievalNotConfigured
configure_retrieval(
    index=build_index(corpus, keyword_corpora=keyword_corpora_for(options)),  # 나중에 SupabaseVectorIndex
    embedder=OpenAIEmbedder.from_env(),               # .env의 키·모델·차원, handoff와 다르면 오류
    options=options,
    default_terms_edition=corpus.terms_edition(),     # corpus_documents의 약관 판 "2026.11.11"
)
register_local_evidence_store(corpus)                 # APP_ENV=local이고 Vercel이 아닐 때만 등록
```

보고서 쪽은 `app.api.reports.configure_with_knowledge_base()` 하나가 위를 모두 한다.

### 로컬 EvidenceStore 어댑터 (#52, C와 합의)

| 항목 | 내용 |
| --- | --- |
| 읽는 파일 | `data/private/corpus-db-import/db_import/`의 `corpus_documents`·`corpus_chunks`·`corpus_records`·`corpus_source_pages` `.jsonl`. 색인과 어댑터가 같은 `read_corpus` 결과를 쓴다(88MB를 한 번만 읽음) |
| 같은 조인 | 검색 결과 청크와 `get_chunk`는 같은 `search_chunk`(청크 + `corpus_documents`를 `document_id`로 조인, `document`는 SourceDocument 8개 필드)로 만든다. `validate_hits`가 필드별로 비교하기 때문이다. 객체는 따로 만들어 검색 결과를 고쳐도 검증 기준이 바뀌지 않는다 |
| 등록 조건 | `APP_ENV=local`이고 `VERCEL`·`VERCEL_ENV`가 없을 때만(`app/config.py`의 로컬 인증 우회와 같은 규칙). 운영에서는 등록하지 않는다 |
| 파일 없음 | 아무것도 등록하지 않는다. `validate_hits`는 그대로 `RuntimeError("DB evidence store is not configured")`(fail-closed). 빈 결과로 바꾸지 않는다 |
| 실패 처리 | `configure_with_knowledge_base()`의 코퍼스 실패는 모두 `RetrievalNotConfigured`다. 파일·폴더·`handoff.json`이 없으면 그 클래스, 있는데 못 쓰면 하위 클래스 `CorpusDataError`(읽기 오류(OSError), UTF-8 아님, 빈 파일, 깨진 JSON 줄, 칼럼·타입 오류, 없는 `document_id`, `chunk_id`·`document_id` 중복, 벡터 길이·숫자 아님, 임베딩 값 섞임, 약관 판 하나 아님, 코퍼스 임베딩 ≠ 질의 임베더). 메시지에 파일·줄·문제를 담고 원인은 `__cause__`. `.env`의 `OPENAI_EMBEDDING_MODEL`·`OPENAI_EMBEDDING_DIMENSIONS`가 handoff와 다르거나 숫자가 아니면 `from_env`의 `EmbeddingMismatch`를 연결 함수 안에서만 `EmbeddingSettingsInvalid`(역시 하위 클래스, 키 값은 메시지에 없음)로 감싼다. 코드 버그(TypeError 등)는 감싸지 않는다. 모든 준비가 끝난 뒤에만 전역 설정을 바꾸므로 실패하면 이전 상태 그대로다 |
| 시작 비용 | 이 PC 기준 파일 읽기 1.0초, 세 코퍼스 BM25 색인 Kiwi 10.7초(간단 토큰화면 합계 1.2초), 어댑터 0.0초, 메모리 약 +230MB |
| 테스트 | `tests/test_d_corpus_loader.py`(tmp 가짜 행, CI에서 돎), 실제 코퍼스는 `test_d_retrieval_real_data`·`test_d_exclusion_clauses.RealDataTest`(파일 없으면 skip) |

| 교체 대상 | 할 일 |
| --- | --- |
| 색인 | `VectorIndex`를 구현한다: `spec`(모델·차원·버전), `search_vector(search_filter, query_vector, limit)`, `fetch_by_parent(corpus, parent_ids, edition=...)`. pgvector에서는 필터와 정렬을 SQL로 처리한다. 점수는 **유사도(클수록 가까움)** 로 돌려준다(pgvector 코사인 거리면 `1 − 거리`). '기간 모름'과 목록 포함 규칙도 같게 구현한다 |
| 임베더 | `OpenAIEmbedder`가 기본이다. 다른 임베더는 `Embedder`를 구현한다: `model`, `dimensions`, `version`, `async embed(text)`. 색인과 같은 값이어야 한다 |
| 검색 방식 | `RetrievalOptions(mode="vector"·"bm25"·"rrf")`, 기본 `"rrf"`(`rrf_k=60`, `candidate_limit=50`). `configure_retrieval(options=...)`에 하나를 넘기면 세 코퍼스 공통, `{코퍼스: RetrievalOptions}`를 넘기면 코퍼스별이다. 사전에 빠진 코퍼스는 RRF, 넘길 때마다 세 코퍼스를 모두 새로 정한다. 모르는 코퍼스·잘못된 값은 거절하고 이전 설정을 유지한다. 임베더는 벡터·RRF를 쓰는 코퍼스에만 필요하다. 지금 값은 `retrieval_options(corpus)` |
| BM25 통계 | `build_index(corpus, keyword_corpora=...)`·`InMemoryIndex(keyword_corpora=...)`. `None`이면 전체, 목록에 없는 코퍼스는 적재 때 토큰화하지 않는다(벡터만 쓰는 코퍼스). `keyword_corpora_for(options)`로 설정과 맞춘다. 통계 없는 코퍼스를 BM25·RRF로 검색하면 빈 결과가 아니라 `RetrievalNotConfigured` |
| 키워드 색인 구현 | 색인은 `keyword_version`, `search_keyword(search_filter, query_text, limit)`를 채운다 |
| 재순위·trigram | 하지 않았다. `use_rerank`를 켜면 `NotImplementedError` |
| 토큰화 | `pip install -e ".[keyword]"`(kiwipiepy, Apache-2.0). 없으면 간단 토큰화로 대체. 이름은 `retrieval_version`에 남는다(`bm25-v1:kiwi`) |

기본 판은 `configure_retrieval`에 넘겨야 적용된다. 코드 기본값은 `None`이라 넘기지 않으면 약관 판 없는 검색은 빈 목록이다.

## 질의 임베더 (`OpenAIEmbedder`)

| 항목 | 내용 |
| --- | --- |
| 준비 | `py -3.12 -m pip install -e ".[embedding]"`, `.env`에 `OPENAI_API_KEY`, `OPENAI_EMBEDDING_MODEL`, `OPENAI_EMBEDDING_DIMENSIONS` |
| 값 검사 | 모델·차원이 `handoff.json`의 `embedding`과 다르면 `EmbeddingMismatch`. 버전은 환경 변수에 없어 handoff 값을 쓴다 |
| 미연결 | 키·모델·차원이 없거나 `openai`·`python-dotenv`가 없으면 `RetrievalNotConfigured` |
| 키 | 객체에 저장하지 않는다. OpenAI 클라이언트가 환경 변수에서 직접 읽는다 |
| 캐시 | 같은 질문은 메모리에 캐시(최대 1,024개, 가장 오래 안 쓴 것부터 버림). 프로세스가 끝나면 사라진다 |

## 측정: 벡터·BM25·RRF 비교 (2026-11-09, 데이터 `4b925a1`)

`py -3.12 scripts/try_retrieval.py --compare`, 평가 코드는 `evals.retrieval.evaluate.evaluate`(`allow_machine_reviewed=True`).
같은 실행에서 같은 질의 벡터를 재사용한다. 토큰화는 Kiwi, RRF는 `k=60`, 후보 50개이고 평가셋에 맞춰 조정하지 않았다. 약관은 기본 판, 역할 필터 없음.
**라벨은 기계 검수 기준이다. 질문은 GPT-5가 원문을 보고 만들었고 실사용자 질문이나 전문가 정답이 아니다.**

| 코퍼스 | 방식 | Top-1 | Recall@3 | MRR@10 | 벡터 대비 Top-1 나아짐 / 나빠짐 |
| --- | --- | --- | --- | --- | --- |
| `fault_standards` (100) | 벡터 | 0.550 | 0.840 | 0.696 | - |
| | BM25 | 0.610 | 0.845 | 0.728 | +16 / -10 |
| | **RRF** | **0.690** | **0.855** | **0.785** | +19 / -5 |
| `dispute_cases` (100) | 벡터 | 0.550 | 0.740 | 0.673 | - |
| | **BM25** | **0.770** | **0.910** | **0.842** | +28 / -6 |
| | RRF | 0.730 | 0.910 | 0.823 | +21 / -3 |
| `policy_terms` (80) | 벡터 | 0.500 | 0.619 | 0.585 | - |
| | **BM25** | **0.812** | **0.881** | **0.856** | +27 / -2 |
| | RRF | 0.637 | 0.831 | 0.742 | +14 / -3 |

BM25만 간단 토큰화로 돌린 결과(Top-1 / Recall@3 / MRR@10, Kiwi 없이 대체될 때의 값): 도표 0.680 / 0.860 / 0.769, 결정례 0.700 / 0.920 / 0.815, 약관 0.775 / 0.894 / 0.837. 적재 시간은 간단 토큰화 1.2초, Kiwi 약 10초다(세 코퍼스 모두 BM25 통계를 만들 때. 벡터만 쓰는 코퍼스는 `keyword_corpora`에서 빼면 계산하지 않는다).

### 읽을 때 주의할 점

- **평가셋이 BM25에 유리할 수 있다.** 질문이 원문에서 만들어져, 질문 내용어가 정답 조항에 있는 비율이 도표 0.75, 결정례 0.79, 약관 0.63이다(다른 조항 표본 0.53, 0.46, 0.30). 실제 담당자 표현이 다르면 차이는 줄 수 있다. 얼마나 줄지는 이 평가로 알 수 없다.
- 1문항은 도표·결정례에서 0.01, 약관에서 0.0125다. 벡터 점수는 API 변동으로 실행마다 1문항 안에서 움직였다. 결정례의 BM25와 RRF 차이(Top-1 4문항, Recall@3 동일)는 작다.
- 문서·판이 코퍼스마다 하나뿐이라 판 구분은 측정하지 못한다. 정답 없는 질문도 없다.

### 짧은 질문 5개 표본 (`try_retrieval.py --mode …`, 정답 라벨 없음)

| 질문 | 벡터 1위 | BM25 1위 | RRF 1위 |
| --- | --- | --- | --- |
| 음주운전 중 사고 자기차량손해 보상하지 않는 손해 | 제23조 p.67 | 제23조 p.68 | 제23조 p.68 |
| 무면허 운전 면책 | 부록 p.274 | 부록 p.279 | 부록 p.279 |
| 자기부담금 | 제24조 p.68 | 제24조 p.69 | 제24조 p.68 |
| 신호등 있는 교차로 녹색 직진 대 적색 직진 | 차1-1 | **차1-2** (차1-1은 3위 밖) | 차1-1 |
| 교차로 신호위반 직진 차량 충돌 | 2019-022179 | 2019-022179 | 2019-022179 |

음주 질문은 BM25·RRF가 제14호가 있는 p.68을 1위로 올렸다. 도표 질문은 BM25만 쓰면 정확한 도표를 놓쳐 RRF 추천과 맞는다. 무면허 질문은 세 방식 모두 부록이 1위라, 면책 근거를 조항 ID 직접 조회로 두는 쪽이 여전히 맞다.

### 코퍼스별 추천 (기계 검수 평가셋 기준, 적용하지 않음)

| 코퍼스 | 추천 | 이유 |
| --- | --- | --- |
| `policy_terms` | BM25 | 세 지표 모두 가장 높고 나빠진 문항이 2개. RRF는 벡터가 끌어내려 BM25보다 낮다. handoff의 `clause_bm25`와도 맞다 |
| `dispute_cases` | BM25 | Top-1이 가장 높다. RRF와 차이가 작아 실제 사실 카드 질의로 다시 봐야 한다 |
| `fault_standards` | RRF | Top-1·MRR이 가장 높고 나빠진 문항이 가장 적다. 실제로는 handoff의 `attribute_first`(`chart_id` 우선)와 함께 정해야 한다 |

적용한 것은 **세 코퍼스 모두 RRF**(기본값)다. 코퍼스별 설정은 만들어 두었지만 위 추천(약관·결정례 BM25)은 적용하지 않았다. 아래 짧은 질문 비교에서 약관 BM25가 오히려 낮아, 한쪽 평가셋만 보고 바꾸지 않는다.

### 짧은 질문 30개 비교 (참고용, 2026-11-10, `try_retrieval.py --short`)

**질문과 정답 모두 Claude가 작성했다. 사람 검수가 아니다.** 질문은 청크 원문을 보기 전에 담당자 말투로 먼저 써서 저장했고(`69857a7`), 그다음 도표·결정례·조항 목록과 원문을 보고 정답 `parent_id`를 붙였다(`360f53b`). 코퍼스마다 10개, 정답이 코퍼스에 없는 3문항(보행자·자전거 도표, 후진 추돌 결정례)은 채점에서 뺐다. 1문항이 약 0.1이라 방식 사이의 차이로 결론을 내지 않는다. Kiwi, `top_k=10`, 약관은 기본 판, 역할 필터 없음.

| 코퍼스 | 방식 | 채점 문항 | Top-1 | Hit@3 | MRR@10 |
| --- | --- | --- | --- | --- | --- |
| `fault_standards` | 벡터 | 8 | 0.000 | 0.500 | 0.208 |
| | BM25 | 8 | 0.125 | 0.625 | 0.375 |
| | RRF | 8 | 0.250 | 0.625 | 0.438 |
| `dispute_cases` | 벡터 | 9 | 0.111 | 0.111 | 0.111 |
| | BM25 | 9 | 0.222 | 0.333 | 0.332 |
| | RRF | 9 | 0.222 | 0.333 | 0.272 |
| `policy_terms` | 벡터 | 10 | 0.200 | 0.400 | 0.328 |
| | BM25 | 10 | 0.200 | 0.200 | 0.220 |
| | RRF | 10 | 0.200 | 0.300 | 0.288 |

질문 내용어가 정답 원문에 그대로 있는 비율(같은 Kiwi 토큰화, 정답 / 다른 조항 표본):

| 코퍼스 | 짧은 질문 | 지난 평가셋(GPT-5 작성) |
| --- | --- | --- |
| `fault_standards` | 0.63 / 0.38 | 0.75 / 0.53 |
| `dispute_cases` | 0.47 / 0.28 | 0.79 / 0.46 |
| `policy_terms` | 0.46 / 0.24 | 0.63 / 0.30 |

읽을 때:
- **담당자 말투에서는 세 방식 모두 크게 떨어진다.** Top-1이 0~0.25로, 지난 평가셋(0.50~0.81)보다 훨씬 낮다. 질문 단어가 정답 원문에 있는 비율도 낮아서(결정례 0.79 → 0.47), 지난 평가셋이 원문 표현에 가까웠다는 걱정이 맞았다.
- 도표는 RRF가 세 지표 모두 가장 높고, 결정례는 BM25와 RRF가 비슷하며 둘 다 벡터보다 높다. 약관은 벡터가 Hit@3·MRR이 가장 높고 BM25가 가장 낮다. 지난 평가셋의 '약관은 BM25' 추천과 반대라 약관 방식은 아직 정하지 않는다.
- 약관은 1위가 부록(`appendix`)·방주(`side_note`)인 경우가 많다(세 방식 30회 중 13회: 벡터 2, BM25 5, RRF 6). 주 조항 필터(`retrieval_role`)는 C 도구에서 막혀 있어 이번 비교에 넣지 않았다.
- 구어 표현("자차", "자부담", "깜빡이", "끼어든", "파란불")이 원문("자기차량손해", "자기부담금", "방향지시등", "진로변경", "녹색")과 달라 BM25가 놓치고, 벡터도 비슷한 도표·사례를 1위로 올린다. 동의어·질의 바꿔 쓰기는 하지 않았다.

### 면책 조항 검색의 한계

**그래서 면책 근거는 벡터 검색 없이 조항 ID 직접 조회만 쓰고, C의 `validate_hits`로 검증한다**(`feat/d-claims-reports`의 `review_exclusions`). 아래는 벡터만 썼을 때의 기록이다.

주 조항 필터로 부록은 빠졌지만 **실제 면책 조항은 벡터 상위 3개에 들지 못한다.** 벡터 보조 검색은 `top_k=3`이다.
자기차량손해 "보상하지 않는 손해"인 제23조 중 해당 단어가 있는 청크의 순위:

| 검색 주제 | 전체 495 | `primary_clause` 332 | + `coverage=own_damage` 113 |
| --- | --- | --- | --- |
| 무면허 운전 면책 | 145등 | 75등 | 46등 |
| 음주 운전 자기차량손해 면책 | 12등 | 6등 | 5등 |
| 고의 사고 면책 | 152등 | 43등 | 15등 |

"무면허 운전 면책"의 필터 후 1위는 제1조 용어 정의(무면허운전 뜻풀이)이고, 2·3위는 운전자 연령 특약이다.
검색 주제 문구가 약관 표현("보상하지 않는 손해")과 달라 벡터 유사도가 낮은 것으로 보인다. 키워드 검색, 질의 문구, `top_k` 중 무엇을 고칠지는 아직 시험하지 않았다.

### LLM 면책 판정 시험 (2026-11-09, `gpt-5`, LLM 호출 8번)

`py -3.12 scripts/try_exclusion_judge.py`. 합성 사건 4개 × 근거 2가지. 사실 입력은 운영과 같게 `case_facts`의 무면허·음주·고의만 넘겼다. 재시도 없음, CI 제외.
조항 ID는 제23조 하나라 호를 구분하지 못하므로, 호 인용은 판정 이유 문구로 봤다.

| 사건 | 근거 | 기대 | 판정 | 인용 조항 | 판정 이유 요약 |
| --- | --- | --- | --- | --- | --- |
| 무면허=True | 제23조만(2) | fail, 제14호 | **unknown** | 제23조 | 제14호는 특정 주체에 한정, 운전자가 대상자인지 불명 |
| 무면허=True | 제23조+벡터(5) | fail, 제14호 | **unknown** | 제23조, 제1조 | 대상자 한정(호 번호 언급 없음), 실제 운전자 정보 없음 |
| 음주=True | 제23조만(2) | fail, 제14호 | **unknown** | 제23조 | 운전자가 제14호 대상자인지 불명 |
| 음주=True | 제23조+벡터(5) | fail, 제14호 | **unknown** | 제23조 | 운전자가 제14호 해당자인지 불명 |
| 고의=True | 제23조만(2) | fail, 제1호 | fail | 제23조 | 계약자·피보험자 고의 손해는 면책(호 번호 언급 없음) |
| 고의=True | 제23조+벡터(5) | fail, 제1호 | fail | 제23조 | 계약자·피보험자 고의 손해는 면책(호 번호 언급 없음) |
| 해당 없음 | 제23조만(2) | pass | pass | 제23조 | 무면허 사실 없음, 제14호 미적용 |
| 해당 없음 | 제23조+벡터(5) | pass | pass | 제23조 | 무면허 사실 없음 |

- **판정은 근거 2가지에서 8건 모두 같았다.** 벡터 보조가 판정을 바꾸지 않았고, 다른 담보 조항을 잘못 인용하지도 않았다. 차이는 무면허에서 제1조(용어 정의)를 함께 인용한 것뿐이다. 사건마다 1번씩 돌린 결과라 반복 안정성은 재지 않았다.
- **무면허·음주가 기대와 달리 `unknown`이다.** 제14호는 대상자가 정해져 있는데 `case_facts`에 운전자 정보가 없기 때문이다. 약관 해석으로는 맞는 답이다.
- 그래서 담당자 입력을 LLM보다 우선하기로 했다. 다시 돌린 결과(`--final`, LLM 4번): 무면허·음주 `True`는 LLM이 `unknown`이어도 최종 `not_payable`, 청구서 없음("지급 불가 — 구상할 보험금 없음"), LLM이 인용한 제23조는 근거로 표시된다. 주제를 사건당 하나로 줄인 시험이다.
- "해당 없음"은 호출 상한 때문에 무면허 주제로만 시험했다.

그 밖에 시연에서 본 점(표본이 적어 일반화하지 않는다):
- "교차로 신호위반 직진 차량 충돌" 2·3위가 신호기 없는 교차로 사례다. 벡터만으로는 신호 유무 구분이 약하다.
- "자기부담금" 필터 후 2위 본문은 제목 한 줄뿐이다.

아직 안 한 것: 재순위·trigram(하지 않기로 함), 동의어·질의 바꿔 쓰기, Supabase 구현, HTTP 연결, 코퍼스별 필수 필터 강제, 임베딩 API 실패 시 재시도 외 정책.

## C(김지은)에게 요청할 것

### 질문 4개 (`1f7b2dd` 기준 진행 상황)

| # | 질문 | 상태 |
| --- | --- | --- |
| 1 | 적용 기간 | **미해결** |
| 2 | 약관 판과 평가 질의 | **일부 해결** (판 문자열 해결, 나머지 미해결) |
| 3 | 결정례 도표 필터 | **해결** (주의 사항 있음) |
| 4 | 약관 담보(`coverage`) | **일부 해결** |

1. **적용 기간 — 미해결:** 세 코퍼스 모두 `effective_from`이 여전히 `""`입니다. `effective_period_status: "unknown"`으로 모른다는 것이 명시됐습니다.
   - 인정기준(`fault_standards`) 판의 실제 시작일·종료일을 채워 줄 수 있나요? 지금은 기간 모름으로 보고 날짜 필터를 통과시키므로, 개정 전 판이 들어오면 사고 일자로 구분하지 못합니다.
   - 결정례 `effective_from`의 의미는 결정일인가요? 지금은 결정례에 사고 일자 필터를 적용하지 않아 검색에 쓰지 않지만, 의미를 문서로 정해 두려고 합니다.
2. **약관 판과 평가 질의 — 일부 해결:**
   - ~~판 문자열에 `(표지)`가 붙어 있음~~ → **해결.** `"2026.11.11"`로 바뀌었고 기본 판도 이 값으로 읽힙니다. 다만 계약의 `terms_edition`이 이 형식으로 들어오는지는 아직 모릅니다.
   - 약관 평가 질의 80건에 `edition`이 없습니다. 질의에 넣을까요, 평가 때 기본 판(`default_terms_edition`)을 쓸까요? 지금 측정은 기본 판을 씁니다.
   - ~~평가 질의의 `accident_date`가 빈 값이면 인정기준이 멈춤~~ → **해결.** C 도구가 빈 값을 허용해서(`4b925a1`) 공통 검색도 빈 값을 '날짜 필터 없음'으로 처리합니다.
3. **결정례 도표 필터 — 해결:** `chart_id`가 1,130건 모두 채워져 `chart_id` 필터가 동작합니다(예: `차12-2` 40건). 확인할 것이 하나 남았습니다.
   - 값에 `chart_filter_usage: "exact_reference_lookup_not_accident_applicability"`, `chart_link_human_reviewed: false`가 붙어 있습니다. 이 필터를 "이 사고에 적용되는 결정례"를 찾는 판단 근거로 써도 되나요, 정확한 참조 조회 용도로만 써야 하나요?
4. **약관 담보(`coverage`) — 일부 해결:** 주 조항 332건 중 `own_damage` 113건, `null` 219건입니다. `null`은 `coverage_status: "unclassified_include_in_fallback"`입니다.
   - 면책 판정을 `coverage="own_damage"`로 거르면 무면허·음주 주 조항의 일부가 빠집니다(무면허 12건 중 6건, 음주 11건 중 5건만 남음). `null`을 어떻게 다뤄야 하나요? 분류가 더 채워질 계획이 있나요? 그때까지 면책 검색에는 `coverage`를 넣지 않습니다.

### 확인한 것 (`handoff.json`, `1f7b2dd`)

| 항목 | 내용 |
| --- | --- |
| 임베딩 | `text-embedding-3-small`, 1536차원, 버전 `text-embedding-3-small:1536:v1`, 코사인. 세 코퍼스 모두 같음 |
| 규모 | 인정기준 923, 결정례 1,130, 약관 495 청크. 벡터 수 같음. `chunk_id`는 코퍼스 안에서 유일 |
| 새 document 필드 | `effective_period_status`, `effective_from_source`, `source_name`, `source_uri_type`. 로더가 그대로 보존 |
| `retrieval_role` | 약관: `primary_clause` 332(`rider` 247 + `general_article` 85), `auxiliary_reference` 163(`side_note` 143 + `cited_law_appendix` 20). 결정례: `card_similarity` 226, `supporting_evidence` 904. 인정기준에는 키 없음 |
| 같은 벡터 | 약관에 같은 벡터의 청크가 일부 있음. 동점은 `chunk_id` 순으로 정렬 |
| `parent_id` | 예: `fault-standards-2023-06:modifier-guide:129`, `dispute-cases-web:2018-051544`. 근거 인용에 그대로 쓴다. 약관은 조항 ID로 쓴다 |

### 함께 정할 것

| 항목 | 내용 |
| --- | --- |
| `source_uri` | 로컬 경로는 없어지고 파일 이름이 됐습니다(`source_uri_type: document_name_not_download_url`). 인용 링크를 쓰려면 저장소 위치가 더 필요합니다 |
| 면책 조항 검색 | 위 '면책 조항 검색의 한계'를 어떻게 풀지 정합니다 |
| 평가 | 위 비교로 코퍼스별 방식을 정합니다. 사람이 쓴 질문이나 사실 카드 질의가 있으면 함께 재면 좋습니다. `handoff.json`의 검색 전략(`attribute_first`, `fact_card_similarity`, `clause_bm25`)도 이때 맞춥니다 |
