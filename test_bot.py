"""Testes básicos das funções de parsing do bot (sem chamar a API do Telegram)."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch

import bot


class FakeEntry(dict):
    """Simula uma entry do feedparser (que também se comporta como dict)."""


class StripHtmlTest(unittest.TestCase):
    def test_removes_tags_and_unescapes_entities(self):
        raw = "<p>Ol&aacute; <strong>mundo</strong> &amp; cia!</p>"
        self.assertEqual(bot.strip_html(raw), "Olá mundo & cia!")

    def test_handles_empty_input(self):
        self.assertEqual(bot.strip_html(""), "")
        self.assertEqual(bot.strip_html(None), "")


class TruncateSummaryTest(unittest.TestCase):
    def test_keeps_short_text_untouched(self):
        text = "Um resumo curto."
        self.assertEqual(bot.truncate_summary(text), text)

    def test_truncates_long_text_at_word_boundary(self):
        text = "palavra " * 200
        result = bot.truncate_summary(text)
        self.assertLessEqual(len(result), bot.SUMMARY_MAX_LENGTH + 3)
        self.assertTrue(result.endswith("..."))


class ExtractImageUrlTest(unittest.TestCase):
    def test_prefers_media_content(self):
        entry = FakeEntry(media_content=[{"url": "https://example.com/capa.jpg"}])
        self.assertEqual(bot.extract_image_url(entry), "https://example.com/capa.jpg")

    def test_falls_back_to_img_in_content(self):
        entry = FakeEntry(
            content=[{"value": '<p>Texto <img src="https://example.com/foto.png"> fim</p>'}]
        )
        self.assertEqual(bot.extract_image_url(entry), "https://example.com/foto.png")

    def test_returns_none_when_no_image(self):
        entry = FakeEntry(summary="<p>Sem imagem aqui.</p>")
        self.assertIsNone(bot.extract_image_url(entry))


class BuildCaptionTest(unittest.TestCase):
    def test_builds_html_caption_with_title_summary_and_link(self):
        entry = FakeEntry(
            title="Meu <b>Artigo</b>",
            link="https://seudominio.substack.com/p/meu-artigo",
            summary="<p>Este é um resumo com <em>HTML</em>.</p>",
        )
        caption = bot.build_caption(entry)
        self.assertIn("<b>Meu Artigo</b>", caption)
        self.assertIn("Este é um resumo com HTML.", caption)
        self.assertIn('Leia o artigo completo →</a>', caption)
        self.assertIn(entry["link"], caption)


class PostedJsonTest(unittest.TestCase):
    def test_load_and_save_roundtrip(self):
        with patch.object(bot, "POSTED_FILE", Path("test_posted_tmp.json")):
            try:
                bot.save_posted({"id-1", "id-2"})
                loaded = bot.load_posted()
                self.assertEqual(loaded, {"id-1", "id-2"})
            finally:
                bot.POSTED_FILE.unlink(missing_ok=True)

    def test_load_returns_empty_set_when_file_missing(self):
        with patch.object(bot, "POSTED_FILE", Path("does_not_exist.json")):
            self.assertEqual(bot.load_posted(), set())


if __name__ == "__main__":
    unittest.main()
