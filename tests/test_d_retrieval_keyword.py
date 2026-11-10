"""D: BM25 키워드 검색. 합성 청크로 확인한다. 외부 패키지·API 없음(Kiwi가 있으면 Kiwi도 확인).

BM25 정답은 공식으로 손계산했다.
"""

import asyncio
import importlib.util
import math
import sys
import unittest
from unittest.mock import patch

from app.retrieval.common import (
    RetrievalNotConfigured,
    configure_retrieval,
    retrieval_options,
    search,
)
from app.retrieval.embedding import FakeEmbedder
from app.retrieval.hybrid import (
    CORPORA,
    RetrievalOptions,
    keyword_corpora_for,
    reciprocal_rank_fusion,
)
from app.retrieval.index import InMemoryIndex
from app.retrieval.keyword import (
    K1,
    B,
    BM25Index,
    KiwiTokenizer,
    SimpleTokenizer,
    default_tokenizer,
)
from app.tools.policy_terms import search_policy_terms
from tests.evidence_d import skip_validation

EMBEDDER = FakeEmbedder()
VECTOR = RetrievalOptions(mode="vector")
BM25_ONLY = RetrievalOptions(mode="bm25")
has_kiwi = importlib.util.find_spec("kiwipiepy") is not None


class WordTokenizer:
    name = "word"

    def tokenize(self, text):
        return text.split()


def document(corpus="policy_terms", edition="판-1", start="", doc_id="d1"):
    return {
        "document_id": doc_id,
        "corpus": corpus,
        "source_uri": "synthetic",
        "edition": edition,
        "effective_from": start,
        "effective_to": None,
        "content_hash": "h",
        "parser_version": "p",
    }


def embedded(chunk_id, text, doc=None, metadata=None):
    chunk = {
        "chunk_id": chunk_id,
        "parent_id": f"parent-{chunk_id}",
        "document": doc or document(),
        "page_start": 1,
        "page_end": 1,
        "text": text,
        "metadata": metadata or {},
    }
    return {
        "chunk": chunk,
        "embedding": asyncio.run(EMBEDDER.embed(text)),
        "embedding_model": EMBEDDER.model,
        "embedding_dimensions": EMBEDDER.dimensions,
        "embedding_version": EMBEDDER.version,
    }


class TokenizerTest(unittest.TestCase):
    def test_simple_splits_korean_into_two_character_pieces(self):
        tokenizer = SimpleTokenizer()
        self.assertEqual(tokenizer.tokenize("무면허 운전"), ["무면", "면허", "운전"])
        self.assertEqual(tokenizer.tokenize("차 BMW-3"), ["차", "bmw", "3"])
        self.assertEqual(tokenizer.tokenize(" ,.! "), [])

    def test_default_falls_back_without_kiwi(self):
        default_tokenizer.cache_clear()
        self.addCleanup(default_tokenizer.cache_clear)
        with patch.dict(sys.modules, {"kiwipiepy": None}):
            self.assertEqual(default_tokenizer().name, "simple")

    @unittest.skipUnless(has_kiwi, "kiwipiepy 없음")
    def test_kiwi_keeps_content_words_only(self):
        tokens = KiwiTokenizer().tokenize("무면허운전을 하였을 때 생긴 손해는")
        self.assertIn("손해", tokens)
        self.assertNotIn("는", tokens)
        self.assertNotIn("을", tokens)


class BM25Test(unittest.TestCase):
    def index(self):
        index = BM25Index(WordTokenizer())
        index.add("a", "음주 운전 손해")  # 길이 3
        index.add("b", "음주 음주")  # 길이 2
        index.add("c", "고의 사고 손해 면책 조항")  # 길이 5
        return index

    def expected(self, tf, length, frequency, count=3, average=10 / 3):
        idf = math.log(1 + (count - frequency + 0.5) / (frequency + 0.5))
        return idf * tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / average))

    def test_scores_match_formula(self):
        scores = self.index().scores("음주")
        self.assertEqual(set(scores), {"a", "b"})
        self.assertAlmostEqual(scores["a"], self.expected(1, 3, 2))
        self.assertAlmostEqual(scores["b"], self.expected(2, 2, 2))
        self.assertGreater(scores["b"], scores["a"])  # 많이 나오고 짧으면 높다

    def test_multiple_terms_add_up_and_repeats_count_once(self):
        index = self.index()
        expected = self.expected(1, 3, 2) + self.expected(1, 3, 1)
        self.assertAlmostEqual(index.scores("음주 운전")["a"], expected)
        self.assertEqual(index.scores("음주 음주"), index.scores("음주"))

    def test_rare_term_weighs_more(self):
        scores = self.index().scores("운전 손해")
        self.assertGreater(scores["a"], scores["c"])

    def test_no_match_is_empty(self):
        self.assertEqual(self.index().scores("없는단어"), {})
        self.assertEqual(BM25Index(WordTokenizer()).scores("음주"), {})

    def test_same_id_replaces_stats(self):
        index = self.index()
        index.add("b", "고의")
        self.assertEqual(set(index.scores("음주")), {"a"})
        self.assertEqual(len(index), 3)
        fresh = BM25Index(WordTokenizer())
        fresh.add("a", "음주 운전 손해")
        fresh.add("c", "고의 사고 손해 면책 조항")
        fresh.add("b", "고의")
        self.assertEqual(index.scores("음주 고의"), fresh.scores("음주 고의"))


class KeywordSearchTest(unittest.TestCase):
    def setUp(self):
        self.index = InMemoryIndex(
            [
                embedded("t1", "음주 운전 손해", metadata={"coverage": "own_damage"}),
                embedded("t2", "음주 음주", metadata={"coverage": "liability"}),
                embedded("t3", "고의 사고", metadata={"coverage": "own_damage"}),
                embedded("o1", "음주 운전 다른 판", document(edition="판-2", doc_id="d2")),
                embedded(
                    "f-old",
                    "음주 도표 구판",
                    document("fault_standards", "구판", "2019-01-01", "f1"),
                ),
                embedded("d1", "음주 결정례", document("dispute_cases", doc_id="d3")),
            ],
            tokenizer=WordTokenizer(),
        )
        configure_retrieval(index=self.index, embedder=None, options=BM25_ONLY)

    def tearDown(self):
        configure_retrieval(
            index=None, embedder=None, options=RetrievalOptions(), default_terms_edition=None
        )

    def search(self, corpus="policy_terms", text="음주", filters=None, **kw):
        request = {
            "corpus": corpus,
            "text": text,
            "accident_date": kw.get("accident_date", ""),
            "filters": {"edition": "판-1"} if filters is None else filters,
            "top_k": kw.get("top_k", 10),
        }
        return asyncio.run(search(request))

    def ids(self, hits):
        return [h["chunk"]["chunk_id"] for h in hits]

    def test_ranks_by_bm25_without_embedder(self):
        hits = self.search()
        self.assertEqual(self.ids(hits), ["t2", "t1"])
        self.assertGreater(hits[0]["score"], hits[1]["score"])
        self.assertEqual(hits[0]["retrieval_version"], "bm25-v1:word")

    def test_top_k(self):
        self.assertEqual(self.ids(self.search(top_k=1)), ["t2"])

    def test_edition_filter_same_as_vector(self):
        self.assertEqual(self.ids(self.search(filters={"edition": "판-2"})), ["o1"])
        with self.assertLogs("app.retrieval.hybrid", "WARNING"):
            self.assertEqual(self.search(filters={}), [])  # 판도 기본 판도 없음
        configure_retrieval(default_terms_edition="판-2")
        self.assertEqual(self.ids(self.search(filters={})), ["o1"])

    def test_metadata_filter_same_as_vector(self):
        filters = {"edition": "판-1", "coverage": "own_damage"}
        self.assertEqual(self.ids(self.search(filters=filters)), ["t1"])
        filters["coverage"] = ["own_damage", "liability"]
        self.assertEqual(self.ids(self.search(filters=filters)), ["t2", "t1"])

    def test_corpus_is_kept(self):
        self.assertEqual(self.ids(self.search("dispute_cases", filters={})), ["d1"])

    def test_date_filter_same_as_vector(self):
        hits = self.search("fault_standards", filters={}, accident_date="2026-10-20")
        self.assertEqual(self.ids(hits), ["f-old"])  # 기간을 모르는 문서는 통과
        doc = document("fault_standards", "구판", "2019-01-01", "f2")
        doc["effective_to"] = "2020-12-31"
        self.index.add([embedded("f-end", "음주 종료된 판", doc)])
        hits = self.search("fault_standards", filters={}, accident_date="2026-10-20")
        self.assertNotIn("f-end", self.ids(hits))
        hits = self.search("fault_standards", filters={}, accident_date="2020-06-01")
        self.assertIn("f-end", self.ids(hits))

    def test_no_match_is_empty_list(self):
        self.assertEqual(self.search(text="없는단어"), [])

    def test_blank_query_rejected(self):
        with self.assertRaises(ValueError):
            self.search(text="   ")

    def test_source_preserved(self):
        hit = self.search(top_k=1)[0]
        self.assertEqual(hit["chunk"]["parent_id"], "parent-t2")
        self.assertEqual(hit["chunk"]["document"]["edition"], "판-1")

    def test_replaced_chunk_updates_stats(self):
        self.index.add([embedded("t2", "고의")])
        self.assertEqual(self.ids(self.search()), ["t1"])

    def test_goes_through_tool(self):
        query = {"text": "음주", "accident_date": "", "filters": {"edition": "판-1"}, "top_k": 3}
        with patch("app.tools.policy_terms.validate_hits", skip_validation):
            hits = asyncio.run(search_policy_terms(query))
        self.assertEqual(self.ids(hits), ["t2", "t1"])


class RRFTest(unittest.TestCase):
    """정답은 공식 1/(k + 순위)로 손계산했다."""

    def chunk(self, chunk_id):
        return {"chunk_id": chunk_id}

    def fuse(self, *lists, k=60):
        ranked = [[(self.chunk(i), 0.0) for i in ids] for ids in lists]
        return [(c["chunk_id"], s) for c, s in reciprocal_rank_fusion(ranked, k=k)]

    def test_scores_match_formula(self):
        out = dict(self.fuse(["a", "b"], ["b", "c"], k=60))
        self.assertAlmostEqual(out["a"], 1 / 61)
        self.assertAlmostEqual(out["b"], 1 / 62 + 1 / 61)
        self.assertAlmostEqual(out["c"], 1 / 62)

    def test_found_by_both_ranks_first(self):
        self.assertEqual([i for i, _ in self.fuse(["a", "b"], ["b", "c"])], ["b", "a", "c"])

    def test_original_scores_are_ignored(self):
        ranked = [[(self.chunk("a"), 0.001)], [(self.chunk("b"), 999.0)]]
        out = reciprocal_rank_fusion(ranked, k=60)
        self.assertEqual([c["chunk_id"] for c, _ in out], ["a", "b"])  # 동점은 chunk_id 순
        self.assertEqual(out[0][1], out[1][1])

    def test_k_changes_how_much_rank_matters(self):
        small = dict(self.fuse(["a", "b"], k=1))
        large = dict(self.fuse(["a", "b"], k=1000))
        self.assertGreater(small["a"] / small["b"], large["a"] / large["b"])

    def test_empty_lists(self):
        self.assertEqual(self.fuse([], []), [])
        self.assertEqual([i for i, _ in self.fuse([], ["a"])], ["a"])


class RRFSearchTest(unittest.TestCase):
    RRF = RetrievalOptions(mode="rrf")

    def setUp(self):
        self.index = InMemoryIndex(
            [
                embedded("t1", "음주 운전 손해", metadata={"coverage": "own_damage"}),
                embedded("t2", "음주 음주", metadata={"coverage": "liability"}),
                embedded("t3", "무면허 면책 조항", metadata={"coverage": "own_damage"}),
                embedded("o1", "음주 운전 다른 판", document(edition="판-2", doc_id="d2")),
            ],
            tokenizer=WordTokenizer(),
        )
        configure_retrieval(index=self.index, embedder=EMBEDDER, options=self.RRF)

    def tearDown(self):
        configure_retrieval(
            index=None, embedder=None, options=RetrievalOptions(), default_terms_edition=None
        )

    def search(self, filters=None, top_k=10, text="음주 운전"):
        request = {
            "corpus": "policy_terms",
            "text": text,
            "accident_date": "",
            "filters": {"edition": "판-1"} if filters is None else filters,
            "top_k": top_k,
        }
        return asyncio.run(search(request))

    def ids(self, hits):
        return [h["chunk"]["chunk_id"] for h in hits]

    def test_combines_both_and_records_version(self):
        hits = self.search()
        self.assertIn("t3", self.ids(hits))  # 벡터로만 잡히는 청크도 들어온다
        self.assertEqual(self.ids(hits)[0], "t1")  # 둘 다 상위인 청크가 1등
        self.assertEqual(
            hits[0]["retrieval_version"],
            f"rrf-v1:k60:{EMBEDDER.model}:{EMBEDDER.version}:bm25-v1:word",
        )
        self.assertLessEqual(hits[0]["score"], 2 / 61)

    def test_top_k_applies_after_fusion(self):
        self.assertEqual(len(self.search(top_k=1)), 1)

    def test_filters_apply_to_both_lists(self):
        self.assertEqual(self.ids(self.search({"edition": "판-2"})), ["o1"])
        filters = {"edition": "판-1", "coverage": "liability"}
        self.assertEqual(self.ids(self.search(filters)), ["t2"])

    def test_no_duplicates(self):
        ids = self.ids(self.search())
        self.assertEqual(len(ids), len(set(ids)))

    def test_needs_embedder(self):
        configure_retrieval(embedder=None)
        with self.assertRaises(RetrievalNotConfigured):
            self.search()

    def test_keyword_only_match_still_found_when_vector_limit_small(self):
        options = RetrievalOptions(mode="rrf", candidate_limit=1)
        configure_retrieval(options=options)
        self.assertEqual(len(self.search(top_k=4)), 3)  # 후보 수는 top_k보다 작아지지 않는다

    def test_k_setting_used(self):
        configure_retrieval(options=RetrievalOptions(mode="rrf", rrf_k=10))
        self.assertIn(":k10:", self.search()[0]["retrieval_version"])

    def test_invalid_options_rejected(self):
        for kw in (
            {"mode": "hybrid"},
            {"mode": "rrf", "rrf_k": 0},
            {"candidate_limit": 0},
        ):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                RetrievalOptions(**kw)


class SwitchTest(unittest.TestCase):
    def tearDown(self):
        configure_retrieval(
            index=None, embedder=None, options=RetrievalOptions(), default_terms_edition=None
        )

    def request(self):
        return {
            "corpus": "dispute_cases",
            "text": "음주",
            "accident_date": "",
            "filters": {},
            "top_k": 3,
        }

    def rows(self):
        return [embedded("d1", "음주 결정례", document("dispute_cases"))]

    def test_default_is_rrf_for_every_corpus(self):
        configure_retrieval(index=InMemoryIndex(self.rows()), embedder=EMBEDDER)
        for corpus in CORPORA:
            self.assertEqual(retrieval_options(corpus).mode, "rrf")
        hit = asyncio.run(search(self.request()))[0]
        self.assertTrue(hit["retrieval_version"].startswith("rrf-v1:"))

    def test_vector_mode_still_needs_embedder(self):
        configure_retrieval(index=InMemoryIndex(self.rows()), embedder=None, options=VECTOR)
        with self.assertRaises(RetrievalNotConfigured):
            asyncio.run(search(self.request()))

    def test_keyword_mode_needs_index(self):
        configure_retrieval(index=None, embedder=None, options=BM25_ONLY)
        with self.assertRaises(RetrievalNotConfigured):
            asyncio.run(search(self.request()))

    def test_empty_index_is_empty_list(self):
        configure_retrieval(index=InMemoryIndex(), embedder=None, options=BM25_ONLY)
        self.assertEqual(asyncio.run(search(self.request())), [])

    def test_loader_style_default_tokenizer_recorded(self):
        index = InMemoryIndex(self.rows(), tokenizer=SimpleTokenizer())
        self.assertEqual(index.keyword_version, "bm25-v1:simple")


class CorpusOptionsTest(unittest.TestCase):
    """코퍼스마다 다른 검색 방식. 사전에 없는 코퍼스는 기본값(RRF)."""

    def setUp(self):
        self.index = InMemoryIndex(
            [
                embedded("t1", "음주 운전 손해"),
                embedded("f1", "음주 도표", document("fault_standards", "판", "", "f")),
                embedded("d1", "음주 결정례", document("dispute_cases", doc_id="d")),
            ],
            tokenizer=WordTokenizer(),
        )

    def tearDown(self):
        configure_retrieval(
            index=None, embedder=None, options=RetrievalOptions(), default_terms_edition=None
        )

    def version(self, corpus):
        filters = {"edition": "판-1"} if corpus == "policy_terms" else {}
        request = {
            "corpus": corpus,
            "text": "음주",
            "accident_date": "",
            "filters": filters,
            "top_k": 1,
        }
        return asyncio.run(search(request))[0]["retrieval_version"]

    def test_each_corpus_uses_its_own_mode(self):
        configure_retrieval(
            index=self.index,
            embedder=EMBEDDER,
            options={"policy_terms": BM25_ONLY, "dispute_cases": VECTOR},
        )
        self.assertTrue(self.version("policy_terms").startswith("bm25-v1:"))
        self.assertTrue(self.version("dispute_cases").startswith("vector-v1:"))
        self.assertTrue(self.version("fault_standards").startswith("rrf-v1:"))  # 빠지면 기본값
        self.assertEqual(retrieval_options("fault_standards").mode, "rrf")

    def test_single_options_apply_to_every_corpus(self):
        configure_retrieval(index=self.index, embedder=EMBEDDER, options=VECTOR)
        for corpus in CORPORA:
            self.assertTrue(self.version(corpus).startswith("vector-v1:"))

    def test_bm25_corpus_needs_no_embedder_but_others_do(self):
        configure_retrieval(index=self.index, embedder=None, options={"policy_terms": BM25_ONLY})
        self.assertTrue(self.version("policy_terms").startswith("bm25-v1:"))
        with self.assertRaises(RetrievalNotConfigured):
            self.version("dispute_cases")

    def test_new_options_replace_all_corpora(self):
        configure_retrieval(options={"policy_terms": BM25_ONLY})
        configure_retrieval(options={"dispute_cases": VECTOR})
        self.assertEqual(retrieval_options("policy_terms").mode, "rrf")

    def test_invalid_corpus_options_rejected_and_previous_kept(self):
        configure_retrieval(options=VECTOR)
        for options in ({"unknown": VECTOR}, {"policy_terms": "bm25"}):
            with self.subTest(options=options), self.assertRaises((ValueError, TypeError)):
                configure_retrieval(options=options)
        self.assertEqual(retrieval_options("policy_terms").mode, "vector")
        with self.assertRaises(ValueError):
            retrieval_options("unknown")

    def test_keyword_corpora_for(self):
        self.assertEqual(keyword_corpora_for(), set(CORPORA))
        self.assertEqual(keyword_corpora_for(VECTOR), set())
        self.assertEqual(
            keyword_corpora_for({"policy_terms": BM25_ONLY, "dispute_cases": VECTOR}),
            {"policy_terms", "fault_standards"},
        )


class CountingTokenizer(WordTokenizer):
    name = "counting"

    def __init__(self):
        self.texts = []

    def tokenize(self, text):
        self.texts.append(text)
        return super().tokenize(text)


class KeywordCorporaTest(unittest.TestCase):
    """벡터만 쓰는 코퍼스는 BM25 통계를 만들지 않는다(적재 때 토큰화하지 않음)."""

    def rows(self):
        return [
            embedded("t1", "약관 음주"),
            embedded("d1", "결정례 음주", document("dispute_cases", doc_id="d")),
        ]

    def tearDown(self):
        configure_retrieval(
            index=None, embedder=None, options=RetrievalOptions(), default_terms_edition=None
        )

    def request(self, corpus):
        return {
            "corpus": corpus,
            "text": "음주",
            "accident_date": "",
            "filters": {"edition": "판-1"} if corpus == "policy_terms" else {},
            "top_k": 3,
        }

    def test_vector_only_corpus_is_not_tokenized(self):
        tokenizer = CountingTokenizer()
        InMemoryIndex(self.rows(), tokenizer=tokenizer, keyword_corpora=["policy_terms"])
        self.assertEqual(tokenizer.texts, ["약관 음주"])

    def test_no_keyword_corpora_never_loads_default_tokenizer(self):
        with patch("app.retrieval.index.default_tokenizer") as default:
            index = InMemoryIndex(self.rows(), keyword_corpora=[])
        default.assert_not_called()
        self.assertEqual(index.keyword_corpora, frozenset())

    def test_default_builds_every_corpus(self):
        tokenizer = CountingTokenizer()
        InMemoryIndex(self.rows(), tokenizer=tokenizer)
        self.assertEqual(len(tokenizer.texts), 2)

    def test_keyword_search_on_vector_only_corpus_is_an_error_not_empty(self):
        index = InMemoryIndex(
            self.rows(), tokenizer=WordTokenizer(), keyword_corpora=["policy_terms"]
        )
        configure_retrieval(
            index=index,
            embedder=EMBEDDER,
            options={"policy_terms": BM25_ONLY, "dispute_cases": VECTOR},
        )
        self.assertEqual(len(asyncio.run(search(self.request("policy_terms")))), 1)
        self.assertEqual(len(asyncio.run(search(self.request("dispute_cases")))), 1)
        configure_retrieval(options={"policy_terms": BM25_ONLY})  # dispute_cases가 RRF로 바뀜
        with self.assertRaises(RetrievalNotConfigured):
            asyncio.run(search(self.request("dispute_cases")))

    def test_settings_and_index_agree_via_keyword_corpora_for(self):
        options = {"policy_terms": BM25_ONLY, "dispute_cases": VECTOR, "fault_standards": VECTOR}
        index = InMemoryIndex(
            self.rows(), tokenizer=WordTokenizer(), keyword_corpora=keyword_corpora_for(options)
        )
        self.assertEqual(index.keyword_corpora, {"policy_terms"})

    def test_unknown_keyword_corpus_rejected(self):
        with self.assertRaises(ValueError):
            InMemoryIndex(keyword_corpora=["unknown"])


if __name__ == "__main__":
    unittest.main()
