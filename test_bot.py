"""Testes básicos das funções de parsing do bot (sem chamar a API do Telegram)."""

import gzip
import io
import json
import unittest
import urllib.error
import zlib
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


class SanitizeXmlBytesTest(unittest.TestCase):
    def test_strips_invalid_control_characters(self):
        raw = b"<title>Ol\x0bA & B</title>"
        cleaned = bot.sanitize_xml_bytes(raw)
        self.assertEqual(cleaned, b"<title>OlA & B</title>")

    def test_keeps_valid_bytes_untouched(self):
        raw = "<title>Título válido, ç ã é</title>".encode("utf-8")
        self.assertEqual(bot.sanitize_xml_bytes(raw), raw)

    def test_keeps_newlines_and_tabs(self):
        raw = b"<title>linha 1\nlinha 2\tcom tab</title>"
        self.assertEqual(bot.sanitize_xml_bytes(raw), raw)


class DecompressResponseTest(unittest.TestCase):
    def test_decompresses_gzip(self):
        raw = b"<rss>conteudo</rss>"
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as f:
            f.write(raw)
        self.assertEqual(bot.decompress_response(buf.getvalue(), "gzip"), raw)

    def test_decompresses_deflate(self):
        raw = b"<rss>conteudo</rss>"
        compressed = zlib.compress(raw)
        self.assertEqual(bot.decompress_response(compressed, "deflate"), raw)

    def test_passes_through_uncompressed(self):
        raw = b"<rss>conteudo</rss>"
        self.assertEqual(bot.decompress_response(raw, ""), raw)
        self.assertEqual(bot.decompress_response(raw, None), raw)


class FetchFeedTest(unittest.TestCase):
    RSS = b"<rss><channel><title>Feed</title></channel></rss>"

    def test_returns_feed_on_direct_success(self):
        with patch.object(bot, "_fetch_raw", return_value=(self.RSS, "")) as mock_fetch:
            feed = bot.fetch_feed("https://exemplo.substack.com/feed")
        mock_fetch.assert_called_once_with("https://exemplo.substack.com/feed")
        self.assertEqual(feed.channel.title, "Feed")

    def test_falls_back_to_proxy_on_403(self):
        forbidden = urllib.error.HTTPError("https://exemplo.substack.com/feed", 403, "Forbidden", {}, None)
        proxy_json = json.dumps(
            {
                "items": [
                    {
                        "title": "Artigo via proxy",
                        "link": "https://exemplo.substack.com/p/artigo",
                        "guid": "https://exemplo.substack.com/p/artigo",
                        "description": "Resumo",
                        "content": "<p>Conteúdo completo</p>",
                        "enclosure": {"link": "https://exemplo.com/capa.jpg", "type": "image/jpeg"},
                        "categories": ["Filosofia"],
                    }
                ]
            }
        ).encode("utf-8")

        def side_effect(url):
            if "rss2json.com" in url:
                return (proxy_json, "")
            raise forbidden

        with patch.object(bot, "_fetch_raw", side_effect=side_effect) as mock_fetch:
            feed = bot.fetch_feed("https://exemplo.substack.com/feed")
        self.assertEqual(mock_fetch.call_count, 2)
        self.assertIn("rss2json.com", mock_fetch.call_args[0][0])
        self.assertFalse(feed.bozo)
        entry = feed.entries[0]
        self.assertEqual(entry["title"], "Artigo via proxy")
        self.assertEqual(entry["link"], "https://exemplo.substack.com/p/artigo")
        self.assertEqual(entry["enclosures"][0]["href"], "https://exemplo.com/capa.jpg")
        self.assertEqual(entry["tags"][0]["term"], "Filosofia")

    def test_reraises_non_403_http_errors(self):
        server_error = urllib.error.HTTPError("https://exemplo.substack.com/feed", 500, "Server Error", {}, None)
        with patch.object(bot, "_fetch_raw", side_effect=server_error):
            with self.assertRaises(urllib.error.HTTPError):
                bot.fetch_feed("https://exemplo.substack.com/feed")


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
    def _run(self, updates, initial_subscribers=None, should_respond=False, rag_answer="resposta"):
        with patch.object(bot, "SUBSCRIBERS_FILE", Path("test_subscribers_tmp.json")), \
             patch.object(bot, "OFFSET_FILE", Path("test_offset_tmp.json")), \
             patch.object(bot, "get_updates", return_value=updates), \
             patch.object(bot, "get_me", return_value=(999, "meubot")), \
             patch.object(bot.rag, "should_respond", return_value=should_respond), \
             patch.object(bot.rag, "answer_question", return_value=rag_answer), \
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

    def test_unknown_text_does_not_subscribe_or_reply_when_not_eligible_for_chat(self):
        updates = [{"update_id": 3, "message": {"chat": {"id": 999}, "text": "oi tudo bem?"}}]
        mock_send, subscribers = self._run(updates, should_respond=False)
        self.assertEqual(subscribers, set())
        mock_send.assert_not_called()

    def test_dispatches_eligible_message_to_rag_and_replies(self):
        updates = [{
            "update_id": 4,
            "message": {
                "message_id": 42,
                "chat": {"id": 555, "type": "private"},
                "text": "qual o tema do blog?",
            },
        }]
        mock_send, _ = self._run(updates, should_respond=True, rag_answer="Resposta gerada.")
        mock_send.assert_called_once()
        args, kwargs = mock_send.call_args
        self.assertEqual(args[0], 555)
        self.assertIn("Resposta gerada.", args[1])
        self.assertEqual(kwargs.get("reply_to_message_id"), 42)

    def test_chat_reply_html_escapes_the_answer(self):
        updates = [{
            "update_id": 4,
            "message": {"message_id": 42, "chat": {"id": 555, "type": "private"}, "text": "pergunta"},
        }]
        mock_send, _ = self._run(updates, should_respond=True, rag_answer="Menos que <isso> & mais.")
        args, _ = mock_send.call_args
        self.assertIn("&lt;isso&gt;", args[1])
        self.assertIn("&amp;", args[1])

    def test_rag_failure_does_not_crash_or_reply(self):
        updates = [{
            "update_id": 4,
            "message": {"message_id": 42, "chat": {"id": 555, "type": "private"}, "text": "pergunta"},
        }]
        with patch.object(bot, "SUBSCRIBERS_FILE", Path("test_subscribers_tmp.json")), \
             patch.object(bot, "OFFSET_FILE", Path("test_offset_tmp.json")), \
             patch.object(bot, "get_updates", return_value=updates), \
             patch.object(bot, "get_me", return_value=(999, "meubot")), \
             patch.object(bot.rag, "should_respond", return_value=True), \
             patch.object(bot.rag, "answer_question", side_effect=Exception("boom")), \
             patch.object(bot, "send_telegram_message") as mock_send:
            try:
                bot.process_updates([])
                mock_send.assert_not_called()
            finally:
                bot.SUBSCRIBERS_FILE.unlink(missing_ok=True)
                bot.OFFSET_FILE.unlink(missing_ok=True)

    def test_offset_advances_past_processed_updates(self):
        updates = [{"update_id": 5, "message": {"chat": {"id": 111}, "text": "/start"}}]
        with patch.object(bot, "SUBSCRIBERS_FILE", Path("test_subscribers_tmp.json")), \
             patch.object(bot, "OFFSET_FILE", Path("test_offset_tmp.json")), \
             patch.object(bot, "get_updates", return_value=updates), \
             patch.object(bot, "get_me", return_value=(999, "meubot")), \
             patch.object(bot, "send_telegram_message"):
            try:
                bot.process_updates([])
                self.assertEqual(bot.load_offset(), 6)
            finally:
                bot.SUBSCRIBERS_FILE.unlink(missing_ok=True)
                bot.OFFSET_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
