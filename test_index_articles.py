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


class BuildArticlesCatalogTest(unittest.TestCase):
    def test_deduplicates_by_url_and_sorts_by_title(self):
        index = [
            {"url": "https://x.com/p/b", "titulo": "Beta", "capa": "https://x.com/b.jpg", "texto": "...", "embedding": [1]},
            {"url": "https://x.com/p/b", "titulo": "Beta", "capa": "https://x.com/b.jpg", "texto": "...(chunk 2)", "embedding": [2]},
            {"url": "https://x.com/p/a", "titulo": "Alfa", "capa": "https://x.com/a.jpg", "texto": "...", "embedding": [3]},
        ]
        catalog = index_articles.build_articles_catalog(index)
        self.assertEqual(catalog, [
            {"titulo": "Alfa", "url": "https://x.com/p/a", "capa": "https://x.com/a.jpg"},
            {"titulo": "Beta", "url": "https://x.com/p/b", "capa": "https://x.com/b.jpg"},
        ])

    def test_missing_capa_defaults_to_empty_string(self):
        index = [{"url": "https://x.com/p/a", "titulo": "Alfa", "texto": "...", "embedding": [1]}]
        catalog = index_articles.build_articles_catalog(index)
        self.assertEqual(catalog, [{"titulo": "Alfa", "url": "https://x.com/p/a", "capa": ""}])

    def test_strips_stray_whitespace_from_title(self):
        index = [{"url": "https://x.com/p/a", "titulo": "  Alfa Com Espaço  ", "texto": "...", "embedding": [1]}]
        catalog = index_articles.build_articles_catalog(index)
        self.assertEqual(catalog[0]["titulo"], "Alfa Com Espaço")

    def test_empty_index_returns_empty_catalog(self):
        self.assertEqual(index_articles.build_articles_catalog([]), [])


if __name__ == "__main__":
    unittest.main()
