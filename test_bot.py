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


class SubscribersJsonTest(unittest.TestCase):
    def test_load_and_save_roundtrip(self):
        with patch.object(bot, "SUBSCRIBERS_FILE", Path("test_subscribers_tmp.json")):
            try:
                bot.save_subscribers({111, 222})
                self.assertEqual(bot.load_subscribers(), {111, 222})
            finally:
                bot.SUBSCRIBERS_FILE.unlink(missing_ok=True)

    def test_load_returns_empty_set_when_file_missing(self):
        with patch.object(bot, "SUBSCRIBERS_FILE", Path("does_not_exist.json")):
            self.assertEqual(bot.load_subscribers(), set())


class ExtractCategoriesTest(unittest.TestCase):
    def test_returns_unique_categories_in_order(self):
        entries = [
            FakeEntry(tags=[{"term": "Filosofia"}, {"term": "Cultura"}]),
            FakeEntry(tags=[{"term": "cultura"}, {"term": "Política"}]),
            FakeEntry(tags=None),
        ]
        self.assertEqual(bot.extract_categories(entries), ["Filosofia", "Cultura", "Política"])

    def test_returns_empty_list_when_no_tags(self):
        self.assertEqual(bot.extract_categories([FakeEntry()]), [])


class CommandMessageBuildersTest(unittest.TestCase):
    def test_categories_message_lists_categories(self):
        entries = [FakeEntry(tags=[{"term": "Filosofia"}])]
        message = bot.build_categories_message(entries)
        self.assertIn("Filosofia", message)

    def test_categories_message_handles_empty(self):
        message = bot.build_categories_message([])
        self.assertIn("Nenhuma categoria", message)

    def test_recent_articles_message_lists_titles_and_links(self):
        entries = [
            FakeEntry(title="Artigo 1", link="https://example.com/1"),
            FakeEntry(title="Artigo 2", link="https://example.com/2"),
        ]
        message = bot.build_recent_articles_message(entries, count=2)
        self.assertIn("Artigo 1", message)
        self.assertIn("https://example.com/1", message)
        self.assertIn("Artigo 2", message)

    def test_recent_articles_message_respects_count(self):
        entries = [FakeEntry(title=f"Artigo {i}", link=f"https://example.com/{i}") for i in range(10)]
        message = bot.build_recent_articles_message(entries, count=3)
        self.assertIn("Artigo 2", message)
        self.assertNotIn("Artigo 3", message)

    def test_substack_message_contains_link(self):
        self.assertIn(bot.SUBSTACK_SUBSCRIBE_URL, bot.build_substack_message())

    def test_sugestao_message_asks_to_send_message(self):
        message = bot.build_sugestao_message().lower()
        self.assertIn("mensagem", message)


class ProcessUpdatesTest(unittest.TestCase):
    def _run(self, updates, initial_subscribers=None):
        with patch.object(bot, "SUBSCRIBERS_FILE", Path("test_subscribers_tmp.json")), \
             patch.object(bot, "OFFSET_FILE", Path("test_offset_tmp.json")), \
             patch.object(bot, "get_updates", return_value=updates), \
             patch.object(bot, "send_telegram_message") as mock_send:
            try:
                if initial_subscribers is not None:
                    bot.save_subscribers(initial_subscribers)
                bot.process_updates([])
                return mock_send, bot.load_subscribers()
            finally:
                bot.SUBSCRIBERS_FILE.unlink(missing_ok=True)
                bot.OFFSET_FILE.unlink(missing_ok=True)

    def test_start_command_subscribes_user(self):
        updates = [{"update_id": 1, "message": {"chat": {"id": 111}, "text": "/start"}}]
        mock_send, subscribers = self._run(updates)
        self.assertEqual(subscribers, {111})
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args[0][0], 111)

    def test_stop_command_unsubscribes_user(self):
        updates = [{"update_id": 2, "message": {"chat": {"id": 111}, "text": "/stop"}}]
        mock_send, subscribers = self._run(updates, initial_subscribers={111})
        self.assertEqual(subscribers, set())
        mock_send.assert_called_once()

    def test_unknown_text_does_not_subscribe_or_reply(self):
        updates = [{"update_id": 3, "message": {"chat": {"id": 999}, "text": "oi tudo bem?"}}]
        mock_send, subscribers = self._run(updates)
        self.assertEqual(subscribers, set())
        mock_send.assert_not_called()

    def test_offset_advances_past_processed_updates(self):
        updates = [{"update_id": 5, "message": {"chat": {"id": 111}, "text": "/start"}}]
        with patch.object(bot, "SUBSCRIBERS_FILE", Path("test_subscribers_tmp.json")), \
             patch.object(bot, "OFFSET_FILE", Path("test_offset_tmp.json")), \
             patch.object(bot, "get_updates", return_value=updates), \
             patch.object(bot, "send_telegram_message"):
            try:
                bot.process_updates([])
                self.assertEqual(bot.load_offset(), 6)
            finally:
                bot.SUBSCRIBERS_FILE.unlink(missing_ok=True)
                bot.OFFSET_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
