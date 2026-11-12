# 검색 방식 비교 평가 틀 — 옥석현(D)·김지은(C)

약관·도표·결정례 검색 9가지(기준선 3 + 재정렬 6가지 조합)를 **같은 질문·같은 지표·같은 결과 JSON**으로 비교한다.
운영 코드(`app/`)는 바꾸지 않는다. 이 문서는 설정·실행 방법만 적는다.
**결과·관찰·채택 제안은 [`docs/rerank-compare-report.md`](../../docs/rerank-compare-report.md)** 에 있다.

- 코퍼스: `data/private/corpus-db-import/`(김지은 님 zip, 판 1413). 평가 질문: 약관 88(약관 80 + 별표 8), 도표 100, 결정례 100.
- 결과 원본 JSON은 `evals/results/rerank_compare/`(무시 경로)에 저장하고 **커밋하지 않는다**(질문 문장·원문 글 포함).
  원문을 뺀 실행 요약만 [`runs/`](runs/)에 둔다(아래 "실행 요약").

## 방식과 번호 (발표 17쪽 기준)

| 번호 | 방식 | 담당 | 코드(`--retriever`) | 결과 이름 | 예전 저장소 번호 |
| --- | --- | --- | --- | --- | --- |
| 기준선 | 벡터, BM25, RRF | D | `baselines.py`(`--methods`) | `vector`·`bm25`·`rrf` | 같음 |
| ① | DecisionRAG 방식 + Qwen3-Reranker-0.6B | 옥석현 | `decisionrag:build` | `decisionrag-qwen` | ① |
| ② | DecisionRAG 방식 + gpt-4o-mini | 옥석현 | `decisionrag:build_gpt` | `decisionrag-gpt` | ② |
| ③ | DecisionRAG 방식 + Jev(공유 문맥) | 옥석현 | `decisionrag:build_jev` | `decisionrag-jev` | ⑤ |
| ④ | 하이브리드(RRF 50) + Qwen3-Reranker-0.6B | 김지은 | `current_rag_rerank:build_qwen` | `qwen-rerank-rrf50` | ③ |
| ⑤ | 하이브리드(RRF 50) + gpt-4o-mini | 김지은 | `current_rag_rerank:build_gpt` | `gpt-rerank-rrf50` | ④ |
| ⑥ | 하이브리드(RRF 50) + Jev(공유 문맥) | 옥석현 | `jev_judge:build_rerank` | `jev-rerank-rrf50` | ⑥ |

코드 안 주석·docstring에는 예전 저장소 번호가 남아 있다(`current_rag_rerank.py`의 ③④, `decisionrag.py`의 ⑤). 도표·결정례 실행과 공통 평가 틀은 옥석현, 약관 ④⑤ 실행은 김지은 담당이다.

## 공통 조건 (`common.py`, 모든 방식이 이 상수·함수만 쓴다)

| 조건 | 값 · 함수 |
| --- | --- |
| 모델에 넣는 글 | `passage_text(제목, 본문)` = 제목 한 줄 + 본문. 본문은 **o200k_base 512토큰**까지(제목은 세지 않음). Qwen에도 같은 글 |
| 제목 | 약관은 레코드 `title`(없으면 별표 `schedule·schedule_title·item`, 그것도 없으면 첫 줄). 도표 `차1-1 녹색직진 대 적색직진`, 결정례 `심의번호 2018-051544`(청크 첫 줄과 같은 꼴이라 본문 첫 줄의 같은 제목은 뗀다) |
| 청크 → parent | `max_p`: 같은 parent는 **최고 점수(MaxP)**. 동점은 parent_id 순 |
| "관련 있나?" 문구 | 틀 하나에 명사만 코퍼스별(`relevance_instruction()`): 약관 "policy clause"(`relevance-v1`), 도표 "fault ratio standard chart"(`relevance-v1-fault`), 결정례 "fault ratio dispute decision case"(`relevance-v1-dispute`). `SYSTEM_PROMPT`는 Qwen3-Reranker 모델 카드 문장, GPT에도 같은 system |
| 모델 입력 | Qwen `qwen_prompt(질문, 글)` → 답 자리 yes·no 로짓 softmax. GPT `gpt_request(질문, 글)`, user 글은 둘 다 `relevance_user_text` + GPT에만 `"Answer only Yes or No."`. Jev는 noul 질문의 task가 같은 문구 |
| GPT | `gpt-4o-mini`, temperature 0, max_tokens 1, logprobs·top_logprobs 5. 점수 `yes_probability` = "Yes"(대소문자·공백 합침) 확률, No로 나누지 않음 |
| 후보(④⑤⑥) | 1단계 검색의 **고유 parent 50개**의 모든 청크(`CANDIDATE_PARENTS`). 방식은 코퍼스별 `candidate_mode`: R@50이 높은 쪽, 같으면 RRF. 세 코퍼스 모두 **RRF**(약관 R@50 RRF 1.000·BM25 0.975, 도표·결정례 둘 다 1.000) |
| 최종 출력 | 상위 **10개** parent_id(`TOP_K`) |

**평가 문항을 보고 문구·자르기 길이·후보 수·트리·예산을 고치지 않는다.** 바꿔야 하면 평가 질문을 보지 않은 이유로만 바꾸고,
`PROMPT_VERSION`(또는 `TREE_VERSION`)을 올린 뒤 모든 방식을 다시 돌린다. 결과 JSON `common`·`params`에 이 값이 남는다.

## 실행

```bash
python -m evals.rerank_compare.run                          # 약관 기준선 6개(아래 "기준선 두 가지")
python -m evals.rerank_compare.run --corpus fault           # 도표(fault)·결정례(dispute). 결과 이름 앞에 fault-·dispute-
python -m evals.rerank_compare.run --corpus dispute --candidates   # 후보 풀: BM25·RRF의 R@10·20·30·50·100
python -m evals.rerank_compare.run [--corpus …] --retriever 모듈:팩토리 --name 이름 --max-model-calls N [--limit 3]
python -m evals.rerank_compare.run --compare evals/results/rerank_compare/a.json b.json   # 저장한 결과를 표 하나로
python -m evals.rerank_compare.summarize evals/rerank_compare/runs/2026-11-12/manifest.json   # 실행 요약 만들기
```

- `--corpus`는 `common.set_corpus`로 프로세스 하나에 코퍼스 하나를 정한다. 점수기 문구·후보 검색·평가셋(`request.filters` 그대로)·결과 이름이 따른다. 기본은 약관.
- 유료 방식은 `--limit 3`으로 비용을 본 뒤 전체를 돌린다. `--max-model-calls`는 판단(후보) 하나당 1회로 세고, 부르기 전에 센다(넘으면 돈을 쓰기 전에 멈춤).
- 질의 벡터는 `evals/results/rerank_compare/query_vectors.json`(무시 경로) 캐시에서 읽는다. 기본은 오프라인이라 캐시에 없는 질의는 API를 부르지 않고 멈춘다(`--allow-embedding-api N`이면 최대 N회 받음). 캐시는 288문항 모두 들어 있고 옥석현이 갖고 있다.
- 준비: 메인 `.venv`(`pip install -e ".[dev]"` + `kiwipiepy`)로 기준선·GPT·Jev를 돌린다. Qwen(①④)은 저장소 밖 가상환경:

```bash
py -3.12 -m venv C:\Users\Admin\.venvs\okm-rerank
C:\Users\Admin\.venvs\okm-rerank\Scripts\python.exe -m pip install -r evals/rerank_compare/requirements-qwen.txt
C:\Users\Admin\.venvs\okm-rerank\Scripts\python.exe -m pip install -e .
HF_HUB_OFFLINE=1 C:\Users\Admin\.venvs\okm-rerank\Scripts\python.exe -m evals.rerank_compare.run --retriever evals.rerank_compare.decisionrag:build --name decisionrag-qwen --max-model-calls 36000
```

torch·transformers는 운영 의존성(pyproject)에 넣지 않는다. 모델(약 1.2GB)은 처음 실행 때 Hugging Face 캐시로 받는다.

## 끼우는 함수

```python
from evals.rerank_compare.harness import Meter, Question

async def retrieve(question: Question, meter: Meter) -> list[str]:  # 동기 함수도 된다
    ...
    return parent_ids  # 상위 parent_id 최대 10개, 순위대로
```

- 팩토리 `build(search)`가 끼우는 함수를 돌려준다. `search`(`baselines.CorpusSearch`)는 `candidate_passages(question)`(후보 parent의 모든 청크 글), `candidate_parents(question, mode=None, n=50)`, `parents`(`CorpusParents`: 제목·청크·글), `corpus`(db_import 행), `corpus_name`을 준다.
- `Question`: `example_id`, `text`, `accident_date`, `filters`. **정답·세트 이름은 들어 있지 않다.**
- 같은 parent가 반복되면 첫 순위만 쓴다. 10개를 넘거나 list[str]이 아니면 멈춘다.
- 모델을 부르기 **전에** `meter.add(모델 이름, calls=n)`, 사용량은 `meter.add(모델 이름, calls=0, input_tokens=…, output_tokens=…)`. 계층 탐색은 단계마다 `meter.stage(이름, 남은 parent)`(채점 때만 정답과 비교 → 단계 생존율).
- 끼우는 함수에 `params`(결과 JSON에 남길 설정)와 `after_run()`(실행 뒤 사용량·비용을 params에 더함)을 붙일 수 있다.

## 기준선 두 가지 — 비교표에는 "고유 parent 10개"를 쓴다

| 이름 | 뜻 | 용도 |
| --- | --- | --- |
| `vector`, `bm25`, `rrf` | 고유 parent 10개가 찰 때까지 청크를 더 가져온다 | **비교표용**(재정렬과 같은 10개) |
| `vector-legacy`, `bm25-legacy`, `rrf-legacy` | 상위 10개 **청크**를 parent로 합친다 | 예전 숫자(`docs/d-retrieval.md`) 재현 확인용 |

RRF는 운영과 같이 벡터·BM25 각각 max(50, 요청 청크 수)개를 합친다(k=60).

## 방식별 설정

### ①②③ DecisionRAG 방식의 계층 탐색 (`decisionrag.py`, `corpus_trees.py`)

논문: Wan et al., "Retriever-Free Retrieval: Navigating Documents with Decision Models" (arXiv 2610.35868).
검색기 없이 목차 트리를 위에서 내려가며 단계마다 판단 모델 점수로 상위 몇 개만 남기고, 마지막에 남은 parent의 청크를 `max_p`로 합쳐 상위 10개를 낸다.
①②③은 트리·예산·노드 글·문구가 같고 판단 모델만 다르다(①② 하나씩 판단, ③ 공유 문맥).

| 코퍼스 | 트리(`TREE_VERSION`) | L1 묶음 | L2 노드 | 노드 글 |
| --- | --- | --- | --- | --- |
| 약관 | `policy-toc-v1` | **28**: 보통약관 장·절 14, 별표 1~5, 특약 Ⅰ~Ⅷ, 인용 법령 1(원문 PDF 목차 p.45~51) | parent 382(조·특약·별표 항목·각주·법령 페이지) | 묶음 = 경로 + 하위 조항 제목. 조항 = 경로 > 제목 + 본문 앞 128토큰 |
| 도표 | `fault-toc-v1` | **24**: 원문 목차 절 23(가. 교차로 (1)~(6), 나. 마주보는 방향 (1)~(3), 다. 같은 방향 (1)~(8), 라. 기타 (1)~(5), 마. 이륜차 특수) + "3. 수정요소의 해설" | 도표 108 + 공통 수정요소 해설 19 | 묶음 = 장·절 경로 + 하위 도표 제목. 도표 = 경로 > 도표 번호·제목 + 본문 앞 128토큰 |
| 결정례 | `dispute-chart-v1` | **21**: 결정례가 연결된 도표(`chart_id`)의 절 | 결정례 226 | 묶음 = 경로 + 연결된 도표 제목. 결정례 = 경로 > 도표 제목 > 심의번호·사례 제목 + 본문 앞 128토큰 |

- **예산(세 코퍼스 같음)**: L1 묶음 5, L2 parent 20, L3 남은 parent의 모든 청크 → MaxP → 상위 10. 평가 문항을 보기 전에 트리 크기만 보고 정했다.
- 목차 정보는 코퍼스에 칸이 없어 원문 목차 제목과 번호 범위를 코드 상수로 옮겼다(`ARTICLE_GROUPS` 등, `corpus_trees.FAULT_SECTIONS`). 도표는 차대차만 있어 "차대차" 단계가 없고 장(가~마)은 경로에만 쓴다.
- 결정례의 도표 연결은 사람 검토 전이다(`chart_link_human_reviewed False`, 부록 번호 대응).
- 단계 생존율: L1 = 정답 parent가 든 묶음이 남았나, L2 = 정답 parent가 20개 안에 남았나.
- 논문과 다른 점은 결과 JSON `params.deviations`(코퍼스·판단 모델별, `corpus_deviations`)에 있고, 요약은 보고서에 있다.

### ①②④⑤ 하나씩 판단하는 점수기 (`qwen_reranker.py`, `gpt_judge.py`)

- ①④ `QwenReranker`: Qwen3-Reranker-0.6B, revision `e61197ed…` 고정, fp16, 배치 16, `logits_to_keep=1`. 모델 카드처럼 세 부분을 따로 토큰화해 잇는다.
- ②⑤ `GptJudge`: 글마다 `gpt_request` 한 번, 동시 요청 16. 키는 `.env`의 `OPENAI_API_KEY`(클라이언트가 환경에서 읽음, 출력·저장 안 함). API 오류는 0점으로 메우지 않고 멈춘다. 사용량·비용은 `params.usage`(단가 `PRICE_PER_MILLION`: 입력 $0.15, 캐시 $0.075, 출력 $0.60 / 100만 토큰, 2026-11-11 확인).
- ④⑤ `current_rag_rerank.CurrentRagReranker`(김지은): 후보 청크 전부를 점수기로 판단하고 MaxP → 상위 10.

### ③⑥ Jev 공유 문맥 점수기 (`jev_judge.py`)

Jev(TypeSafe System One, 논문 버전 **`jev-1.13.0`**, 공식 API `POST https://api.typesafe.ai/v1/systemone`). 한 단계(⑥은 후보 전체)의 글을 state 하나에 함께 넣고 후보마다 noul 질문을 붙인다.

```text
state     = {"question": 질문, "candidates": {"c000": 글, "c001": 글, ...}}
questions = {"c000": {"type": "noul", "instructions": {"task": relevance_instruction(), "candidate": "c000"}}, ...}
점수       = answers["c000"].noul   # P(yes) 0~1, 소수 둘째 자리, 가공하지 않음
```

- 요청 크기: 공식 한도는 질문당 32k·요청당 64k 토큰. state를 **Jev 토큰 24k**(o200k × 1.412 → o200k 16,997) 이하로 맞추고, 넘으면 **번갈아 배정**(후보 i → i mod k, 모든 묶음이 상한 이하가 되는 가장 작은 k)으로 나눈다. 나뉜 요청끼리는 문맥을 공유하지 않는다. 긴 문맥 헤더(`TypeSafe-Danger-Zone-Long-Context`)는 보내지 않는다.
- 비용: 입력 100만 토큰당 $0.042, 출력 무료, state는 요청마다 한 번 청구. 상한 `JEV_MAX_COST_USD`(기본 3달러) — 보내기 전 어림과 응답 누적 모두 넘으면 멈춘다.
- 키: `.env`의 `TYPESAFE_API_KEY`. 요청 헤더에만 쓰고 출력·저장하지 않는다. 429·5xx·연결 오류는 `retry-after`를 따라 4번까지 다시 보내고, 그 밖의 오류는 멈춘다(메시지에는 상태 코드와 오류 종류만).
- `params.usage`: 요청 수, 재시도, 토큰, `cost_usd`, 실제 응답 모델(`response_models`), `long_context`, 요청 크기 최대값, 나눈 단계 수(`stages_split`), 단계당 요청 수(`requests_per_stage`), 묶음 크기 분포.

```bash
JEV_MAX_COST_USD=0.05 python -m evals.rerank_compare.run --retriever evals.rerank_compare.decisionrag:build_jev --name decisionrag-jev-limit3 --max-model-calls 1500 --limit 3
JEV_MAX_COST_USD=0.40 python -m evals.rerank_compare.run --corpus fault --retriever evals.rerank_compare.decisionrag:build_jev --name decisionrag-jev --max-model-calls 35000
```

## 결과 JSON (모든 방식 같은 모양, `schema: rerank-compare-v1`, 로컬에만)

```text
method, description, created_at, git_commit
dataset   {corpus, count, subsets{policy80, schedule8} | {fault100} | {dispute100}, sha256}
corpus    {db_import 파일별 sha256}
common    {공통 상수 전부: 코퍼스·문구·자르기·모델·후보 수·후보 방식}
params    {방식별 설정(retrieve.params) + after_run(usage 등)}
metrics   {policy80 | schedule8 | all88 | fault100 | dispute100: n, top1, r3, r5, r10, mrr10,
           seconds_mean/p50/p95/total, model_calls, model_calls_per_question, model_calls_by_model,
           input_tokens, output_tokens (보고하지 않으면 null), stage_survival{L1, L2}(계층 탐색만)}
questions [{example_id, subset, question, accident_date, expected, ranked(상위 10),
            scores{first_correct_rank, top1, r3, r5, r10, mrr10}, seconds, model_calls,
            model_calls_by_model, input_tokens, output_tokens,
            stages[{name, kept_count, survival, kept_parents}]}]
```

지표 정의는 `evals/retrieval/evaluate.score_predictions`와 같다(R@k = 정답 parent 중 상위 k개 안에 든 비율, MRR@10은 첫 정답 순위의 역수).

## 실행 요약 (`runs/<날짜>/`, 커밋함)

`summarize.py`가 `manifest.json`의 실행마다 원본 JSON을 읽어 `runs/<날짜>/<코퍼스>/<방식>.summary.json`을 쓴다(재현성 실행은 `policy/repro/`).

```text
schema "rerank-compare-summary-v1"
run       {corpus, slot(발표 번호), repo_number, name, owner, role}
source    {file(원본 JSON 이름), sha256}
method, description, created_at, git_commit
dataset, common, params, metrics          # 원본과 같음
corpus_files_sha256, corpus_edition       # 판 1413·1655·1057(corpus_records.jsonl sha256으로 가림)
questions [{example_id, subset, first_correct_rank}]
```

질문 문장·정답 목록·순위 목록·후보 글·단계별 parent 목록은 넣지 않는다. 남으면 `summarize`가 멈춘다(칸 이름과 질문 문장 검사).
