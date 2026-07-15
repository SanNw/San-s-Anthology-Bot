"""Testes do controle de rate limit da indexação (sem chamar a API da Voyage)."""

import unittest
from unittest.mock import MagicMock, patch

from voyageai.error import RateLimitError

import index_articles


class RateLimitedEmbedTest(unittest.TestCase):
    def setUp(self):
        index_articles._last_embed_call_at[0] = 0.0

    def test_waits_out_the_minimum_interval_between_calls(self):
        client = MagicMock()
        # 1ª chamada: cálculo da espera (100 -> 105, 5s decorridos). 2ª: registro no finally.
        times = iter([105.0, 105.0])
        with patch.object(index_articles.time, "monotonic", side_effect=lambda: next(times)), \
             patch.object(index_articles.time, "sleep") as mock_sleep:
            index_articles._last_embed_call_at[0] = 100.0
            index_articles._rate_limited_embed(client, ["chunk"])
        mock_sleep.assert_called_once_with(index_articles.EMBED_MIN_INTERVAL_SECONDS - 5.0)
        client.embed.assert_called_once_with(
            ["chunk"], model=index_articles.rag.EMBEDDING_MODEL, input_type="document"
        )

    def test_retries_once_after_rate_limit_error(self):
        client = MagicMock()
        success = MagicMock()
        client.embed.side_effect = [RateLimitError("3 RPM"), success]
        with patch.object(index_articles.time, "monotonic", return_value=0.0), \
             patch.object(index_articles.time, "sleep") as mock_sleep:
            result = index_articles._rate_limited_embed(client, ["chunk"])
        self.assertIs(result, success)
        self.assertEqual(client.embed.call_count, 2)
        mock_sleep.assert_any_call(index_articles.EMBED_MIN_INTERVAL_SECONDS)


if __name__ == "__main__":
    unittest.main()
