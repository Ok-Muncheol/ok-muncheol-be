import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.ingestion.embed import embed_chunks, validate_vector


class EmbeddingTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_text_and_response_index_order(self):
        chunks = [
            {"chunk_id": "a", "text": "첫 문서"},
            {"chunk_id": "b", "text": "다른 문서"},
            {"chunk_id": "c", "text": "첫 문서"},
        ]
        client = SimpleNamespace(
            embeddings=SimpleNamespace(
                create=AsyncMock(
                    return_value=SimpleNamespace(
                        data=[
                            SimpleNamespace(index=1, embedding=[0.0, 1.0]),
                            SimpleNamespace(index=0, embedding=[1.0, 0.0]),
                        ]
                    )
                )
            )
        )
        with patch("openai.AsyncOpenAI") as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=client)
            factory.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await embed_chunks(chunks, "model", 2, "v1")
        self.assertEqual(
            client.embeddings.create.call_args.kwargs["input"], ["첫 문서", "다른 문서"]
        )
        self.assertEqual([r["embedding"] for r in result], [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]])

    def test_invalid_vectors_rejected(self):
        for vector in ([0.0, 0.0], [float("nan"), 1.0], [1.0]):
            with self.assertRaises(ValueError):
                validate_vector(vector, 2)


if __name__ == "__main__":
    unittest.main()
