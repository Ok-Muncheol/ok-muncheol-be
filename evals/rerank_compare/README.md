# 약관 재정렬 비교 평가 틀 — 옥석현(D)·김지은(C)

약관 검색 9가지를 **같은 질문·같은 지표·같은 결과 JSON**으로 비교한다. 운영 코드(`app/`)는 바꾸지 않는다.
코퍼스는 `data/private/corpus-db-import/`(1413), 질문은 약관 88문항이다. 결과는 `evals/results/rerank_compare/`(무시 경로)에 저장하고 **커밋하지 않는다**.

| 단계 | 방식 | 담당 | 이 틀에서 |
| --- | --- | --- | --- |
| 1단계 검색 | 벡터, BM25, RRF | D | `baselines.py`(구현됨) |
| 2단계 검색 + 재정렬 | ③ 후보 50개 → Qwen3-Reranker-0.6B | 김지은 | 끼워 넣기 |
| | ④ 후보 50개 → GPT(gpt-4o-mini) | 김지은 | 끼워 넣기 |
| | ⑥ 후보 50개 → Jev(공유 문맥) | 옥석현 | `jev_judge.build_rerank` |
| 3단계 목차 탐색(검색 없음) | ① DecisionRAG + Qwen3-Reranker | 옥석현 | 끼워 넣기 |
| | ② DecisionRAG + GPT | 옥석현 | 끼워 넣기 |
| | ⑤ DecisionRAG + Jev(공유 문맥) | 옥석현 | `decisionrag.build_jev` |

## 공통 조건 (`common.py`, ①~⑥ 모두 이 상수·함수만 쓴다. ⑤⑥ 점수만 Jev noul)

| 조건 | 값 · 함수 |
| --- | --- |
| 모델에 넣는 글 | `passage_text(제목, 본문)` = "조항 제목 + 본문". 본문은 `truncate_tokens`로 **o200k_base 512토큰**까지 자른다(제목은 세지 않음). Qwen에도 같은 기준으로 자른 같은 글을 넣는다 |
| 조항 글 만들기 | `PolicyParents(corpus).passages(parent_id)`: parent의 청크마다 하나(페이지 순). 제목은 레코드 `title`, 없으면 별표 `schedule·schedule_title·item`, 그것도 없으면 첫 줄 |
| 청크 → parent | `max_p([(parent_id, 점수), ...])`: 같은 parent는 **최고 점수(MaxP)**. 동점은 parent_id 순 |
| "관련 있나?" 문구 | `RELEVANCE_INSTRUCTION` 하나(①~④ 공통), `SYSTEM_PROMPT`(Qwen3-Reranker 모델 카드 문장, GPT에도 같은 system) |
| 모델 입력 | Qwen: `qwen_prompt(질문, 글)` → 다음 토큰 "yes"·"no" 확률. GPT: `gpt_request(질문, 글)` → `chat.completions.create(**…)` 인자. user 글은 둘 다 `relevance_user_text`이고 GPT에만 끝에 `"Answer only Yes or No."` |
| GPT | `gpt-4o-mini`, temperature 0, max_tokens 1, logprobs·top_logprobs 5. 점수는 `yes_probability(top_logprobs)` = "Yes"(대소문자·공백 다른 것 합침)의 확률. No로 나누지 않음, 없으면 0 |
| 후보(③④) | **RRF 고유 parent 50개** (`CANDIDATE_MODE = "rrf"`, `CANDIDATE_PARENTS = 50`). 11/11 결정: 약관 80 R@50이 RRF 1.000, BM25 0.975(2문항 놓침)라 후보에서 정답이 빠지지 않는 RRF로 정했다 |
| 최종 출력 | 상위 **10개** parent_id (`TOP_K`) |

**평가 80문항을 보고 문구·자르기 길이·후보 수를 고치지 않는다.** 바꿔야 하면 평가 질문을 보지 않은 이유로만 바꾸고,
`PROMPT_VERSION`을 올린 뒤 모든 방식을 다시 돌린다. 결과 JSON의 `common`에 이 값이 모두 남는다(조건이 같은 결과끼리만 비교).

## 끼우는 함수

```python
from evals.rerank_compare.harness import Meter, Question

async def retrieve(question: Question, meter: Meter) -> list[str]:  # 동기 함수도 된다
    ...
    return parent_ids  # 상위 parent_id 최대 10개, 순위대로
```

- `Question`: `example_id`, `text`, `accident_date`(ISO 날짜 또는 "", 별표3 과실상계 문항 하나만 값이 있음), `filters`(평가셋 그대로, `document_id`).
  **정답·세트 이름은 들어 있지 않다.**
- 같은 parent가 반복되면 첫 순위만 쓴다. 10개를 넘거나 list[str]이 아니면 그 자리에서 멈춘다(예외를 감싸지 않음).
- 모델을 부를 때마다 **부르기 전에** `meter.add(모델 이름)`, 응답의 사용량은 `meter.add(모델 이름, calls=0, input_tokens=…, output_tokens=…)`.
  `--max-model-calls` 상한을 넘으면 `ModelCallLimitExceeded`로 멈춘다(돈을 쓰기 전에 멈추도록 먼저 센다). 로컬 Qwen도 호출 수를 센다.

### 예: ④ 후보 50개 → GPT

```python
# 예시. 파일 위치·이름은 김지은 님이 정한다
from openai import AsyncOpenAI

from evals.rerank_compare.common import GPT_MODEL, TOP_K, gpt_request, max_p, yes_probability

def build(search):  # run.py --retriever 모듈:build 가 PolicySearch를 넘겨 부른다
    client = AsyncOpenAI()

    async def retrieve(question, meter):
        scored = []
        for passage in await search.candidate_passages(question, mode="rrf"):  # 후보 parent의 모든 청크
            meter.add(GPT_MODEL)
            response = await client.chat.completions.create(**gpt_request(question.text, passage.text))
            meter.add(GPT_MODEL, calls=0, input_tokens=response.usage.prompt_tokens,
                      output_tokens=response.usage.completion_tokens)
            top = response.choices[0].logprobs.content[0].top_logprobs
            scored.append((passage.parent_id, yes_probability(top)))
        return [parent_id for parent_id, _ in max_p(scored)[:TOP_K]]

    retrieve.params = {"candidates": "rrf", "candidate_parents": 50}  # 결과 JSON params에 남는다
    return retrieve
```

```bash
python -m evals.rerank_compare.run --retriever 모듈경로:build --name gpt-rerank-rrf50 --max-model-calls 9000
```

- `search`(`baselines.PolicySearch`)가 주는 것: `candidate_parents(question, mode="rrf", n=50)`(고유 parent 50개, 기본 RRF),
  `candidate_passages(question, mode="rrf")`(그 parent들의 모든 청크 글), `parents`(`PolicyParents`: 제목·청크·글, 전체 parent 목록 — ①②의 목차용), `corpus`(db_import 행).
- 후보 50개의 청크 수: 질문당 평균 약 91개(RRF)·96개(BM25), 최대 114개. 청크마다 한 번 부르면 88문항에 **약 8,000~8,500회**,
  글 토큰만 질문당 약 3.2만~3.5만(o200k)이다. 먼저 `--max-model-calls`를 작게 두고 몇 문항으로 확인한다.
- 직접 스크립트로 돌려도 된다: `harness.load_examples()` → `await harness.run_method(이름, retrieve, examples, max_model_calls=…)` → `harness.save_result(결과)`.

## 실행

```bash
python -m evals.rerank_compare.run                     # 기준선 6개(아래)
python -m evals.rerank_compare.run --methods bm25,rrf  # 일부만
python -m evals.rerank_compare.run --candidates        # ③④ 후보 풀: BM25·RRF의 R@10·20·30·50·100
python -m evals.rerank_compare.run --compare evals/results/rerank_compare/a.json b.json   # 표 하나로
```

- 질의 벡터는 `evals/results/rerank_compare/query_vectors.json`({질의 문장: 벡터}, 무시 경로) 캐시에서 읽는다.
  기본은 오프라인이라 캐시에 없는 질의가 나오면 API를 부르지 않고 멈춘다. `--allow-embedding-api N`이면 최대 N회 받아 캐시에 더한다.
  캐시 파일은 옥석현이 갖고 있다(88문항 모두 들어 있음). 필요하면 비공개로 전달받는다.
- 준비: `pip install -e ".[dev]"`(tiktoken 포함) + Kiwi(`kiwipiepy`, BM25 토큰화). tiktoken은 처음 쓸 때 o200k_base 파일을 받을 수 있다.

## 기준선 두 가지 — 비교표에는 "고유 parent 10개"를 쓴다

| 이름 | 뜻 | 용도 |
| --- | --- | --- |
| `vector`, `bm25`, `rrf` | 고유 parent 10개가 찰 때까지 청크를 더 가져온다 | **비교표용**(③④·①②와 같은 10개) |
| `vector-legacy`, `bm25-legacy`, `rrf-legacy` | 상위 10개 **청크**를 parent로 합친다(평균 9.1~9.3개) | 예전 숫자(`scripts/try_retrieval.py --compare`) 재현 확인용 |

RRF는 운영과 같이 벡터·BM25 각각 max(50, 요청 청크 수)개를 합친다. 그래서 요청 청크 수가 바뀌면 순위도 조금 바뀔 수 있고,
`--candidates`는 k마다 풀을 따로 만든다(`candidate_parents(n=k)`와 같은 풀).

## 지금 숫자 (2026-11-11, 코퍼스 1413, 질의 벡터 캐시, 기계 검수 라벨)

비교표용(고유 parent 10개). 질문당 시간은 이 PC 기준.

| 세트 | 방식 | Top-1 | R@3 | R@5 | R@10 | MRR@10 |
| --- | --- | --- | --- | --- | --- | --- |
| 약관 80 | vector | 0.487 | 0.619 | 0.694 | 0.769 | 0.578 |
| | bm25 | 0.812 | 0.881 | 0.894 | 0.944 | 0.856 |
| | rrf | 0.637 | 0.819 | 0.856 | 0.919 | 0.743 |
| 별표 8 | 세 방식 모두 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |

legacy 3개는 예전 값과 같고 질문별 순위도 88문항 모두 같다. 후보 풀(고유 parent k개 안에 정답이 있는 비율):

| 세트 | 방식 | R@10 | R@20 | R@30 | R@50 | R@100 |
| --- | --- | --- | --- | --- | --- | --- |
| 약관 80 | bm25 | 0.944 | 0.963 | 0.975 | 0.975 | 1.000 |
| | rrf | 0.919 | 1.000 | 1.000 | **1.000** | 1.000 |

→ ③④ 후보 기본값은 **RRF**(`CANDIDATE_MODE`). 다른 후보로 돌릴 때는 `mode=`를 넘기고 `params`에 적는다.

## ① DecisionRAG 방식의 계층 탐색을 무료 오픈소스 모델로 재현 (`decisionrag.py`, D)

논문: Wan et al., "Retriever-Free Retrieval: Navigating Documents with Decision Models" (arXiv 2610.35868).
검색기 없이 약관 목차 트리를 위에서 아래로 내려가며, 단계마다 판단 모델 점수로 상위 몇 개만 남긴다(top-k 가지치기).
마지막에 남은 parent의 청크를 점수 매기고 `max_p`로 상위 10개 parent를 돌려준다.

| 단계 | 노드 (382 parent 모두 도달 가능) | 모델에 보여 주는 글 | 남기는 수 |
| --- | --- | --- | --- |
| L1 묶음 28개 | 보통약관 장·절 14, 별표 1~5, 특약 Ⅰ~Ⅷ, 인용 법령 1 | 경로 + 하위 조항 제목(인용 법령은 본문의 `[법령명]`), `passage_text` 512토큰 | **5** |
| L2 parent | 남은 묶음의 조·특약·별표 항목·각주·법령 페이지 | 경로 > 제목 + 본문 앞 **128토큰**(각주는 "연결 조항 제목 각주") | **20** |
| L3 청크 | 남은 20개 parent의 모든 청크 | ③④와 같은 `passage_text(제목, 청크)` | MaxP → 상위 10 |

- 묶음은 원문 PDF 목차(p.45~51)의 편·장·절 제목과 조·특약 번호 범위다(코퍼스에 편·장 칸이 없음). 제9~11조는 목차대로 "제3절 배상책임에서 공통으로 적용할 사항"에 둔다.
- 각주는 처음 연결한 조항(페이지 순)의 묶음에 둔다. 동점은 문서 순서로 가른다.
- 판단 모델: `qwen_reranker.QwenReranker`(Qwen3-Reranker-0.6B, revision 고정). 글은 `common.qwen_prompt`와 같고(`RELEVANCE_INSTRUCTION`을 **모든 단계에**), 점수는 마지막 위치 yes/no 로짓 softmax의 yes 확률.
- **예산(묶음 5, parent 20)과 노드 글 길이(128)는 평가 문항을 보기 전에 트리 크기만 보고 정했다. 결과를 보고 바꾸지 않는다.** 결과 JSON `params`에 예산·트리·`deviations`·모델 revision·장치가 남는다.
- **단계 생존율**: 끼우는 함수가 단계마다 `meter.stage(이름, 남은 parent)`를 남기고, 채점 때만 정답과 비교한다(L1 = 정답 parent가 든 묶음이 남았나, L2 = 정답 parent가 20개 안에 남았나). 결과 JSON `metrics.*.stage_survival`, 질문별 `stages`, `--compare` 표의 "단계 생존율" 칸.

### 논문과 다른 점

의도한 변경
1. 판단 모델: 논문의 Jev(유료 API) 대신 **Qwen3-Reranker-0.6B**(무료, 로컬)
2. 후보 판단: 논문은 한 단계의 후보를 한 문맥에서 함께 판단(공유 문맥), 여기서는 **하나씩** 판단(Qwen3-Reranker 구조상). 논문 실험(§6.1)에서는 하나씩 판단하면 Top-1이 낮고 R@10은 비슷했다
3. 계층: **약관 목차 구조**(묶음 → 조항 → 청크)를 그대로 사용

조사하며 생긴 차이
4. 다지선다 재정렬 단계 없음(Jev 기능)
5. 결과 단위: 논문은 문단 순위, 여기서는 parent 순위(청크 MaxP, 평가 틀 공통)
6. 노드 설명: 논문은 제목·절 제목 또는 LLM 요약, 여기서는 위 표의 글(LLM 요약 없음). 각주 143개·법령 페이지 20개는 제목이 모두 같아 본문 앞부분을 넣었다
7. 질문 문구: 논문은 단계별 질문, 여기서는 공통 `RELEVANCE_INSTRUCTION` 하나
8. 예산: 논문은 평가와 겹치지 않는 설계용 셋으로 고름, 여기서는 설계용 셋이 없어 평가 전에 고정
9. 확률 임계 규칙·대화 메모(notepad)·dense 후보 추가·페이지 점수 곱하기는 쓰지 않음
10. 편·장 정보는 원문 목차를 코드 상수로 옮김(목차의 제52조의2는 코퍼스에 없음)

### 실행 (실험용 가상환경, 저장소 밖)

```bash
py -3.12 -m venv C:\Users\Admin\.venvs\okm-rerank
C:\Users\Admin\.venvs\okm-rerank\Scripts\python.exe -m pip install -r evals/rerank_compare/requirements-qwen.txt
C:\Users\Admin\.venvs\okm-rerank\Scripts\python.exe -m pip install -e .
C:\Users\Admin\.venvs\okm-rerank\Scripts\python.exe -m evals.rerank_compare.run --retriever evals.rerank_compare.decisionrag:build --name decisionrag-qwen --max-model-calls 36000 [--limit 3]
```

torch·transformers는 운영 의존성(pyproject)에 넣지 않는다. 모델(약 1.2GB)은 처음 실행 때 Hugging Face 캐시로 받는다. 문항당 호출은 평균 약 125회, 최대 약 400회다.

## ② DecisionRAG 방식 + gpt-4o-mini (`decisionrag.build_gpt`, `gpt_judge.py`, D)

①과 **같은 트리·예산(묶음 5 / parent 20)·노드 글·`RELEVANCE_INSTRUCTION`·하나씩 판단**이고 점수기만 다르다(모델 차이만 보려고).

- 점수기 `gpt_judge.GptJudge`: 글마다 `common.gpt_request`(gpt-4o-mini, temperature 0, max_tokens 1, logprobs·top_logprobs 5)로 한 번씩 요청, 점수는 `common.yes_probability`. 동시 요청은 16개까지(속도용, 판단은 서로 독립).
- 키는 `.env`의 `OPENAI_API_KEY`(클라이언트가 환경에서 읽음, 출력·저장 안 함). API 오류는 점수 0으로 메우지 않고 멈춘다(메시지에는 예외 종류만).
- 실제 사용량·비용은 결과 JSON `params.usage`(요청 수, 입력·캐시·출력 토큰, `cost_usd`). 단가는 `gpt_judge.PRICE_PER_MILLION`(2026-11-11 OpenAI 모델 페이지: 입력 $0.15, 캐시 입력 $0.075, 출력 $0.60 / 100만 토큰).
- `params.deviations`는 ①의 목록에서 모델 줄을 바꾸고 "점수: 논문은 Yes·No로 정규화한 P(Yes), 여기서는 정규화하지 않은 Yes 확률(평가 틀 공통)"을 더한 것이다.
- temperature 0이어도 OpenAI 응답은 완전히 결정적이지 않다. 다시 돌리면 몇 문항이 바뀔 수 있다.

```bash
# 메인 .venv(openai·python-dotenv·tiktoken). 3문항으로 비용 확인 → 전체
python -m evals.rerank_compare.run --retriever evals.rerank_compare.decisionrag:build_gpt --name decisionrag-gpt --max-model-calls 1500 --limit 3
python -m evals.rerank_compare.run --retriever evals.rerank_compare.decisionrag:build_gpt --name decisionrag-gpt --max-model-calls 12000
```

## ⑤ DecisionRAG 방식 + Jev, ⑥ RRF 50 → Jev (`jev_judge.py`, `decisionrag.build_jev`, D)

판단 모델만 Jev(TypeSafe System One, 논문 버전 **`jev-1.13.0`**, 공식 API `POST https://api.typesafe.ai/v1/systemone`)로 바꾼다.

- ⑤는 ①②와 같은 트리·예산(묶음 5 / parent 20)·노드 글·`RELEVANCE_INSTRUCTION`이다.
- ⑥은 ③④와 같은 후보(`candidate_passages(mode="rrf")`, 고유 parent 50개의 모든 청크)·`passage_text`·`max_p`이고, 김지은 님 `current_rag_rerank.CurrentRagReranker`를 import해서 쓴다.
- 다른 점은 Jev의 **공유 문맥** 하나다. 한 단계(⑥은 후보 묶음)의 글을 state 하나에 함께 넣고, 후보마다 noul 질문을 하나씩 붙인다.

```text
state     = {"question": 질문, "candidates": {"c000": 글, "c001": 글, ...}}
questions = {"c000": {"type": "noul", "instructions": {"task": RELEVANCE_INSTRUCTION, "candidate": "c000"}}, ...}
점수       = answers["c000"].noul   # P(yes) 0~1, 가공하지 않음
```

### 요청 크기와 나누기 (평가 결과를 보기 전에 정함)

- 공식 한도(docs.typesafe.ai/models, 2026-11-12): **질문당 32k 토큰**(state + 질문 하나), **요청당 64k 토큰**(state + 질문 전부). 바이트가 아니라 토큰 기준이다.
- 긴 문맥 모드는 `TypeSafe-Danger-Zone-Long-Context: true` 헤더를 보낼 때만 켜진다("느리고 덜 정확"). **이 코드는 그 헤더를 보내지 않는다**(결과 JSON `long_context: false`).
- 기준: 한 요청의 state를 **Jev 토큰 24k**(질문당 한도의 75%) 이하로 맞춘다. Jev 토큰화는 공개되지 않아 o200k × **1.412**로 어림했다(평가 문항이 아닌 확인 요청 1회: Jev 2,378 / o200k 1,684). 그래서 o200k 기준 상한은 **16,997**이다.
- 넘으면 **번갈아 배정**한다(후보 i → i mod k). k는 모든 묶음이 상한 이하가 되는 가장 작은 수다. 나뉜 요청끼리는 문맥을 공유하지 않는다.
- 측정(o200k, 88문항, API 없이):

| 단계 | 후보 수 평균 / 최대 | state 평균 / 최대 | 결과 |
| --- | --- | --- | --- |
| ⑤ L1 묶음 28 | 28 | 4.2k / 4.2k | 항상 한 요청 |
| ⑤ L2 | 47 / 141 (②에서 남은 묶음 기준) | 7.9k / 22.2k | 대개 한 요청 |
| ⑤ L3 | 27 / 35 | 8.1k / 16.0k | 대개 한 요청 |
| ⑥ 후보 전체 | 91 / 109 | 33.4k / 42.0k | 2~3요청 |

- 실제(88문항): ⑤는 264단계 중 12단계를 2요청으로 나눴다. ⑥은 88단계를 모두 나눴다(2요청 39 · 3요청 49). 가장 큰 요청의 응답 `input_tokens`는 ⑤ 24,840, ⑥ 23,292다(질문당 32k 안).

### 비용·키·멈춤

- 가격: 입력 100만 토큰당 $0.042, 출력 무료. state는 질문 수와 상관없이 요청마다 한 번 청구된다(`--limit 3`으로 확인).
- 결과 JSON `params.usage`에 남는 것: 요청 수, 재시도, 입력·출력 토큰, `cost_usd`, 응답의 실제 모델(`response_models`), `long_context`, 요청 크기 최대값(UTF-8 바이트·o200k·어림 Jev 토큰·응답 토큰), 나눈 단계 수(`stages_split`), 묶음 크기 분포(`batch_sizes`).
- 비용 상한: `JEV_MAX_COST_USD`(기본 3달러). 보내기 전 어림으로 넘을 것 같으면 멈추고, 응답의 누적 비용이 넘어도 그 자리에서 멈춘다.
- 키: `.env`의 `TYPESAFE_API_KEY`. 요청 헤더에만 쓰고 출력·저장하지 않는다.
- 오류 처리: 429·5xx·연결 오류는 `retry-after`를 따라 4번까지 다시 보낸다. 그 밖의 오류는 점수 0으로 메우지 않고 멈춘다. 오류 메시지에는 상태 코드와 오류 종류만 남긴다(422의 `input` 미리보기는 버림).
- 호출 수는 ①~④와 같이 **판단(후보) 하나당 1회**로 센다. 실제 요청 수는 `usage.requests`에 있다.
- Jev 응답은 소수 둘째 자리 단위다. 하위 후보에 동점이 많고, 동점은 문서 순서(L1·L2)·parent_id(MaxP)로 가른다.

### 논문과 다른 점 (⑤, `params.deviations`)

- **[Jev로 해소]**
  - 판단 모델: 논문과 같은 Jev 1.13
  - 공유 문맥: 단, 24k를 넘으면 나눔
  - 점수: noul = P(yes)
- `params.deviations_resolved_by_jev`에 따로 남는다. 요청과 다른 모델이 답하면 "모델 버전: 논문 jev-1.13.0 → 실제 응답 …"이 더해진다(이번 실행은 276·225요청 모두 `jev-1.13.0`).
- 그대로 남은 것:
  - 다지선다 재정렬 없음(Jev로 할 수 있으나 ①②와 같은 단계로 비교하려고 넣지 않음)
  - 결과 단위 parent
  - 노드 설명
  - 공통 질문 문구
  - 예산 고정
  - 확률 임계 규칙 등
  - 목차 상수

```bash
# 메인 .venv. 3문항으로 비용 확인 → 전체(⑤ 다음 ⑥은 남은 상한으로)
JEV_MAX_COST_USD=0.5 python -m evals.rerank_compare.run --retriever evals.rerank_compare.decisionrag:build_jev --name decisionrag-jev --max-model-calls 1500 --limit 3
JEV_MAX_COST_USD=2.95 python -m evals.rerank_compare.run --retriever evals.rerank_compare.decisionrag:build_jev --name decisionrag-jev --max-model-calls 12000
JEV_MAX_COST_USD=2.80 python -m evals.rerank_compare.run --retriever evals.rerank_compare.jev_judge:build_rerank --name jev-rerank-rrf50 --max-model-calls 9000
```

### 결과 (2026-11-12, 약관 80, 코퍼스 1413)

| 방식 | Top-1 | R@3 | R@5 | R@10 | MRR@10 | 질문당 초 | 질문당 호출 | 비용(88문항) | 생존율 L1 · L2 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BM25(기준선) | 0.812 | 0.881 | 0.894 | 0.944 | 0.856 | 0.003 | 0 | 0 | - |
| ① DecisionRAG + Qwen | 0.725 | 0.769 | 0.781 | 0.781 | 0.753 | 3.1 | 120 | 0 | 0.819 · 0.781 |
| ② DecisionRAG + GPT | 0.738 | 0.844 | 0.844 | 0.844 | 0.790 | 6.3 | 102 | $0.436 | 0.844 · 0.844 |
| ⑤ DecisionRAG + Jev | 0.875 | 0.931 | 0.931 | 0.931 | 0.904 | 0.8 | 126 | $0.131 | 0.956 · 0.931 |
| ③ RRF 50 → Qwen | 0.838 | 0.950 | 0.975 | 0.988 | 0.900 | 3.8 | 92 | 0 | - |
| ④ RRF 50 → GPT | 0.875 | 0.988 | 1.000 | 1.000 | 0.930 | 8.3 | 92 | 약 $0.59 | - |
| ⑥ RRF 50 → Jev | **0.938** | **1.000** | **1.000** | **1.000** | **0.967** | 0.45 | 92 | $0.169 | - |

- L1 놓침(약관 80): ① 15, ② 13, ⑤ 4. ②와 ⑤가 겹치는 문항은 2개, ②만 놓친 문항 11개, ⑤만 놓친 문항 2개다.
- ⑤는 L2에서 2문항을 더 놓쳤다. 별표 8은 ⑤⑥ 모두 Top-1 1.000이다.
- **재현성**(같은 날 한 번 더 실행):
  - ⑥은 지표와 질문별 1위가 모두 같다. 정답 아래 순위만 바뀐다.
  - ⑤는 약관 80 Top-1 0.875 → 0.887, R@10 0.931 → 0.944로 1문항 차이가 났다.
  - 원인은 Jev가 같은 요청에도 확률을 조금씩(대개 ±0.01~0.03) 다르게 주기 때문이다. ⑤는 L1 5·6위 경계 점수가 붙어 있어 남는 묶음이 바뀐다.
  - 그래서 ⑤ 숫자는 실행마다 약관 80 Top-1 ±0.0125 정도 흔들린다고 본다.

## 결과 JSON (모든 방식 같은 모양, `schema: rerank-compare-v1`)

```text
method, description, created_at, git_commit
dataset   {count, subsets{policy80, schedule8}, sha256}
corpus    {db_import 파일별 sha256}
common    {공통 상수 전부: 문구·자르기·모델·후보 수}
params    {방식별 설정(retrieve.params)}
metrics   {policy80 | schedule8 | all88: n, top1, r3, r5, r10, mrr10,
           seconds_mean/p50/p95/total, model_calls, model_calls_per_question, model_calls_by_model,
           input_tokens, output_tokens (보고하지 않으면 null), stage_survival{L1, L2}(계층 탐색만)}
questions [{example_id, subset, question, accident_date, expected, ranked(상위 10),
            scores{first_correct_rank, top1, r3, r5, r10, mrr10}, seconds, model_calls,
            model_calls_by_model, input_tokens, output_tokens,
            stages[{name, kept_count, survival, kept_parents}]}]
```

지표 정의는 `evals/retrieval/evaluate.score_predictions`와 같다(R@k = 정답 parent 중 상위 k개 안에 든 비율, MRR@10은 첫 정답 순위의 역수).

## 한계

- 정답은 GPT-5가 원문에서 만들고 기계 검수한 것이다(사람 검수 아님). 원문에서 만든 질문이라 BM25에 유리할 수 있다. 방식끼리 비교하는 용도로 본다.
- 1문항 = 약관 80에서 0.0125, 별표 8에서 0.125. 별표 8문항은 지금 세 방식 모두 만점이라 차이를 가르지 못한다.
- 결과 파일·질의 벡터 캐시·`data/private`는 커밋하지 않는다(원문 인용 포함).
