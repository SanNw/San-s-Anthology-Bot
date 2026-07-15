"""Testes básicos das funções de parsing do bot (sem chamar a API do Telegram)."""

import gzip
import hashlib
import hmac
import html
import http.client
import io
import json
import socket
import threading
import time
import unittest
import urllib.error
import zlib
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlencode

import requests

import bot
import rich_message


def _build_init_data(fields, bot_token):
    """Monta uma string Telegram.WebApp.initData genuína (mesmo algoritmo
    de miniapp.validate_init_data) pra testar o endpoint de chat do Mini App."""
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": computed_hash})


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


class MarkdownToTelegramHtmlTest(unittest.TestCase):
    def test_converts_bold_italic_and_code(self):
        result = bot.markdown_to_telegram_html("**negrito** e *itálico* e `código`")
        self.assertEqual(result, "<b>negrito</b> e <i>itálico</i> e <code>código</code>")

    def test_converts_header_to_bold(self):
        result = bot.markdown_to_telegram_html("# Título\n\nTexto normal.")
        self.assertEqual(result, "<b>Título</b>\n\nTexto normal.")

    def test_converts_fenced_code_block(self):
        result = bot.markdown_to_telegram_html("```\nprint(1)\n```")
        self.assertEqual(result, "<pre>print(1)\n</pre>")

    def test_escapes_html_special_chars_outside_markdown(self):
        result = bot.markdown_to_telegram_html("Menos que <isso> & mais.")
        self.assertEqual(result, "Menos que &lt;isso&gt; &amp; mais.")

    def test_unmatched_marker_stays_literal_instead_of_broken_tag(self):
        result = bot.markdown_to_telegram_html("Isso ficou **truncado no meio")
        self.assertEqual(result, "Isso ficou **truncado no meio")
        self.assertNotIn("<b>", result)

    def test_converts_markdown_link_citation_to_anchor_tag(self):
        result = bot.markdown_to_telegram_html(
            "Como visto em [O Verbo e a Criação](https://san55.substack.com/p/o-verbo), o cosmos..."
        )
        self.assertIn('<a href="https://san55.substack.com/p/o-verbo">O Verbo e a Criação</a>', result)

    def test_ignores_non_http_link_scheme(self):
        result = bot.markdown_to_telegram_html("[clique](javascript:alert(1))")
        self.assertNotIn("<a", result)


class ApplyOffTopicLockTest(unittest.TestCase):
    def setUp(self):
        bot._off_topic_streaks.clear()

    def test_real_answer_passes_through_and_resets_streak(self):
        bot._off_topic_streaks[555] = 2
        result = bot._apply_off_topic_lock(555, "Resposta de verdade sobre os artigos.")
        self.assertEqual(result, "Resposta de verdade sobre os artigos.")
        self.assertNotIn(555, bot._off_topic_streaks)

    def test_refusals_within_the_limit_pass_through_unchanged(self):
        for _ in range(bot.OFF_TOPIC_STREAK_LIMIT):
            result = bot._apply_off_topic_lock(555, bot.rag.REFUSAL_MESSAGE)
            self.assertEqual(result, bot.rag.REFUSAL_MESSAGE)

    def test_lock_message_fires_exactly_once_when_limit_is_crossed(self):
        for _ in range(bot.OFF_TOPIC_STREAK_LIMIT):
            bot._apply_off_topic_lock(555, bot.rag.REFUSAL_MESSAGE)

        result = bot._apply_off_topic_lock(555, bot.rag.REFUSAL_MESSAGE)
        self.assertEqual(result, bot.OFF_TOPIC_LOCK_MESSAGE)

    def test_stays_silent_after_the_lock_message_already_fired(self):
        for _ in range(bot.OFF_TOPIC_STREAK_LIMIT + 1):
            bot._apply_off_topic_lock(555, bot.rag.REFUSAL_MESSAGE)

        result = bot._apply_off_topic_lock(555, bot.rag.REFUSAL_MESSAGE)
        self.assertIsNone(result)

    def test_on_topic_answer_unlocks_and_resets(self):
        for _ in range(bot.OFF_TOPIC_STREAK_LIMIT + 2):
            bot._apply_off_topic_lock(555, bot.rag.REFUSAL_MESSAGE)

        result = bot._apply_off_topic_lock(555, "Voltou a ser sobre os artigos.")
        self.assertEqual(result, "Voltou a ser sobre os artigos.")

        # depois de destravar, uma nova sequência de recusas recomeça do zero
        result = bot._apply_off_topic_lock(555, bot.rag.REFUSAL_MESSAGE)
        self.assertEqual(result, bot.rag.REFUSAL_MESSAGE)

    def test_streaks_are_tracked_independently_per_chat(self):
        for _ in range(bot.OFF_TOPIC_STREAK_LIMIT + 1):
            bot._apply_off_topic_lock(555, bot.rag.REFUSAL_MESSAGE)

        result = bot._apply_off_topic_lock(999, bot.rag.REFUSAL_MESSAGE)
        self.assertEqual(result, bot.rag.REFUSAL_MESSAGE)


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


class CommandMessageBuildersTest(unittest.TestCase):
    def test_categories_messages_list_topics_as_links(self):
        messages = bot.build_categories_messages()
        combined = "\n".join(messages)
        nome, slug = bot.SUBSTACK_TOPICS[0]
        self.assertIn(nome, combined)
        self.assertIn(f"/t/{slug}", combined)

    def test_categories_messages_include_every_topic(self):
        combined = "\n".join(bot.build_categories_messages())
        for nome, _slug in bot.SUBSTACK_TOPICS:
            self.assertIn(html.escape(nome), combined)

    def test_categories_messages_each_stay_within_telegram_limit(self):
        for message in bot.build_categories_messages():
            self.assertLessEqual(len(message), bot.TELEGRAM_TEXT_MAX_LENGTH)

    def test_categories_messages_split_into_more_than_one_when_too_long(self):
        # A lista completa com link (63 categorias) passa do limite de uma
        # mensagem só — bug real corrigido: antes vinha tudo numa string,
        # e o sendMessage falhava (400, mensagem longa demais) sem avisar.
        self.assertGreater(len(bot.build_categories_messages()), 1)

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
        """Chat privado (o caso comum nesses testes) passa pelo caminho de
        streaming — rag.answer_question_stream + sendRichMessageDraft/
        sendRichMessage — então mocka os dois lados: o antigo (send_telegram_message,
        pra comandos e pro caminho de grupo sem streaming) e o novo (send_rich_message).
        classify_send_intent também é mockado como False (pergunta normal) por
        padrão — sem isso, uma mensagem ambígua como "pergunta" (sem gatilho
        de envio nem "?") cairia na chamada real ao Claude pra classificar."""
        with patch.object(bot, "SUBSCRIBERS_FILE", Path("test_subscribers_tmp.json")), \
             patch.object(bot, "OFFSET_FILE", Path("test_offset_tmp.json")), \
             patch.object(bot, "get_updates", return_value=updates), \
             patch.object(bot, "get_me", return_value=(999, "meubot")), \
             patch.object(bot.rag, "should_respond", return_value=should_respond), \
             patch.object(bot.rag, "classify_send_intent", return_value=False), \
             patch.object(bot.rag, "answer_question", return_value=rag_answer), \
             patch.object(bot.rag, "answer_question_stream", return_value=iter([rag_answer])), \
             patch.object(bot, "send_rich_message_draft"), \
             patch.object(bot, "send_rich_message") as mock_send_rich, \
             patch.object(bot, "send_telegram_message") as mock_send:
            try:
                if initial_subscribers is not None:
                    bot.save_subscribers(initial_subscribers)
                bot.process_updates([])
                return mock_send, mock_send_rich, bot.load_subscribers()
            finally:
                bot.SUBSCRIBERS_FILE.unlink(missing_ok=True)
                bot.OFFSET_FILE.unlink(missing_ok=True)

    def test_start_command_subscribes_user(self):
        updates = [{"update_id": 1, "message": {"chat": {"id": 111}, "text": "/start"}}]
        mock_send, _, subscribers = self._run(updates)
        self.assertEqual(subscribers, {111})
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args[0][0], 111)

    def test_stop_command_unsubscribes_user(self):
        updates = [{"update_id": 2, "message": {"chat": {"id": 111}, "text": "/stop"}}]
        mock_send, _, subscribers = self._run(updates, initial_subscribers={111})
        self.assertEqual(subscribers, set())
        mock_send.assert_called_once()

    def test_unknown_text_does_not_subscribe_or_reply_when_not_eligible_for_chat(self):
        updates = [{"update_id": 3, "message": {"chat": {"id": 999}, "text": "oi tudo bem?"}}]
        mock_send, mock_send_rich, subscribers = self._run(updates, should_respond=False)
        self.assertEqual(subscribers, set())
        mock_send.assert_not_called()
        mock_send_rich.assert_not_called()

    def test_dispatches_eligible_message_to_rag_and_replies(self):
        updates = [{
            "update_id": 4,
            "message": {
                "message_id": 42,
                "chat": {"id": 555, "type": "private"},
                "text": "qual o tema do blog?",
            },
        }]
        _, mock_send_rich, _ = self._run(updates, should_respond=True, rag_answer="Resposta gerada.")
        mock_send_rich.assert_called_once()
        args, kwargs = mock_send_rich.call_args
        self.assertEqual(args[0], 555)
        self.assertIn("Resposta gerada.", args[1])
        self.assertEqual(kwargs.get("reply_to_message_id"), 42)

    def test_chat_reply_html_escapes_the_answer(self):
        updates = [{
            "update_id": 4,
            "message": {"message_id": 42, "chat": {"id": 555, "type": "private"}, "text": "pergunta"},
        }]
        _, mock_send_rich, _ = self._run(updates, should_respond=True, rag_answer="Menos que <isso> & mais.")
        args, _ = mock_send_rich.call_args
        self.assertIn("&lt;isso&gt;", args[1])
        self.assertIn("&amp;", args[1])

    def test_chat_reply_converts_markdown_to_telegram_html(self):
        updates = [{
            "update_id": 4,
            "message": {"message_id": 42, "chat": {"id": 555, "type": "private"}, "text": "pergunta"},
        }]
        _, mock_send_rich, _ = self._run(
            updates, should_respond=True,
            rag_answer="**Título**\n\nUm ponto em *itálico* e `código`.",
        )
        args, _ = mock_send_rich.call_args
        self.assertIn("<b>Título</b>", args[1])
        self.assertIn("<i>itálico</i>", args[1])
        self.assertIn("<code>código</code>", args[1])
        self.assertNotIn("**", args[1])

    def test_long_rag_answer_is_truncated_before_sending(self):
        # Chat em grupo (não privado) continua no caminho antigo, sem
        # streaming — sendMessage direto com o limite de TELEGRAM_TEXT_MAX_LENGTH.
        long_answer = "palavra " * 1000  # bem acima do limite de envio do Telegram
        updates = [{
            "update_id": 4,
            "message": {"message_id": 42, "chat": {"id": 555, "type": "group"}, "text": "@meubot pergunta"},
        }]
        mock_send, _, _ = self._run(updates, should_respond=True, rag_answer=long_answer)
        args, _ = mock_send.call_args
        self.assertLessEqual(len(args[1]), bot.TELEGRAM_TEXT_MAX_LENGTH + 10)
        self.assertTrue(args[1].endswith("..."))

    def test_long_rag_answer_is_truncated_in_streaming_path(self):
        # Chat privado: streaming usa o limite bem maior do rich message
        # (32.000 caracteres), não o TELEGRAM_TEXT_MAX_LENGTH do sendMessage.
        long_answer = "palavra " * 5000
        updates = [{
            "update_id": 4,
            "message": {"message_id": 42, "chat": {"id": 555, "type": "private"}, "text": "pergunta"},
        }]
        _, mock_send_rich, _ = self._run(updates, should_respond=True, rag_answer=long_answer)
        args, _ = mock_send_rich.call_args
        self.assertLessEqual(len(args[1]), rich_message.ARTICLE_HTML_MAX_LENGTH + 10)
        self.assertTrue(args[1].endswith("..."))

    def test_send_failure_after_rag_answer_does_not_crash(self):
        updates = [{
            "update_id": 4,
            "message": {"message_id": 42, "chat": {"id": 555, "type": "private"}, "text": "pergunta"},
        }]
        with patch.object(bot, "SUBSCRIBERS_FILE", Path("test_subscribers_tmp.json")), \
             patch.object(bot, "OFFSET_FILE", Path("test_offset_tmp.json")), \
             patch.object(bot, "get_updates", return_value=updates), \
             patch.object(bot, "get_me", return_value=(999, "meubot")), \
             patch.object(bot.rag, "should_respond", return_value=True), \
             patch.object(bot.rag, "classify_send_intent", return_value=False), \
             patch.object(bot.rag, "answer_question_stream", return_value=iter(["resposta"])), \
             patch.object(bot, "send_rich_message", side_effect=requests.HTTPError("400 Client Error")), \
             patch.object(bot, "send_telegram_message"):
            try:
                bot.process_updates([])
            finally:
                bot.SUBSCRIBERS_FILE.unlink(missing_ok=True)
                bot.OFFSET_FILE.unlink(missing_ok=True)

    def test_rag_failure_does_not_crash_and_warns_user(self):
        updates = [{
            "update_id": 4,
            "message": {"message_id": 42, "chat": {"id": 555, "type": "private"}, "text": "pergunta"},
        }]
        with patch.object(bot, "SUBSCRIBERS_FILE", Path("test_subscribers_tmp.json")), \
             patch.object(bot, "OFFSET_FILE", Path("test_offset_tmp.json")), \
             patch.object(bot, "get_updates", return_value=updates), \
             patch.object(bot, "get_me", return_value=(999, "meubot")), \
             patch.object(bot.rag, "should_respond", return_value=True), \
             patch.object(bot.rag, "classify_send_intent", return_value=False), \
             patch.object(bot.rag, "answer_question_stream", side_effect=Exception("boom")), \
             patch.object(bot, "send_telegram_message") as mock_send:
            try:
                bot.process_updates([])
                mock_send.assert_called_once()
                args, kwargs = mock_send.call_args
                self.assertEqual(args[0], 555)
                self.assertEqual(kwargs.get("reply_to_message_id"), 42)
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


class HandleCallbackQueryTest(unittest.TestCase):
    def _callback(self, data):
        return {
            "id": "cbq1",
            "data": data,
            "message": {"chat": {"id": 555}, "message_id": 42},
        }

    def test_sends_rich_article_and_answers_callback_on_success(self):
        with patch.object(bot, "ARTICLE_CONTENT_FILE", Path("test_article_content_tmp.json")), \
             patch.object(bot, "send_rich_message") as mock_rich, \
             patch.object(bot, "answer_callback_query") as mock_answer:
            try:
                bot.save_article_content({"abc123": {"html": "<h1>Título</h1>", "link": "https://x.com/p/a"}})
                bot.handle_callback_query(self._callback("art:abc123"))
            finally:
                bot.ARTICLE_CONTENT_FILE.unlink(missing_ok=True)
        mock_rich.assert_called_once_with(555, "<h1>Título</h1>")
        mock_answer.assert_called_once_with("cbq1")

    def test_falls_back_to_link_message_when_rich_send_fails(self):
        with patch.object(bot, "ARTICLE_CONTENT_FILE", Path("test_article_content_tmp.json")), \
             patch.object(bot, "send_rich_message", side_effect=requests.HTTPError("400 Client Error")), \
             patch.object(bot, "send_telegram_message") as mock_send, \
             patch.object(bot, "answer_callback_query") as mock_answer:
            try:
                bot.save_article_content({"abc123": {"html": "<h1>Título</h1>", "link": "https://x.com/p/a"}})
                bot.handle_callback_query(self._callback("art:abc123"))
            finally:
                bot.ARTICLE_CONTENT_FILE.unlink(missing_ok=True)
        mock_send.assert_called_once()
        self.assertIn("https://x.com/p/a", mock_send.call_args[0][1])
        mock_answer.assert_called_once()
        self.assertTrue(mock_answer.call_args.kwargs.get("show_alert"))

    def test_answers_with_alert_when_short_id_unknown(self):
        with patch.object(bot, "ARTICLE_CONTENT_FILE", Path("test_article_content_tmp.json")), \
             patch.object(bot, "send_rich_message") as mock_rich, \
             patch.object(bot, "answer_callback_query") as mock_answer:
            try:
                bot.handle_callback_query(self._callback("art:nao-existe"))
            finally:
                bot.ARTICLE_CONTENT_FILE.unlink(missing_ok=True)
        mock_rich.assert_not_called()
        mock_answer.assert_called_once()
        self.assertTrue(mock_answer.call_args.kwargs.get("show_alert"))

    def test_ignores_unrelated_callback_data(self):
        with patch.object(bot, "send_rich_message") as mock_rich, \
             patch.object(bot, "answer_callback_query") as mock_answer:
            bot.handle_callback_query(self._callback("outra_coisa"))
        mock_rich.assert_not_called()
        mock_answer.assert_called_once_with("cbq1")


class FetchPostTest(unittest.TestCase):
    def test_slug_from_url_takes_last_path_segment(self):
        self.assertEqual(bot.slug_from_url("https://x.substack.com/p/meu-slug"), "meu-slug")
        self.assertEqual(bot.slug_from_url("https://x.substack.com/p/meu-slug/"), "meu-slug")

    def test_prefers_substack_api_when_available(self):
        api_response = json.dumps({
            "title": "Título da API", "body_html": "<p>Corpo via API.</p>",
            "cover_image": "https://substackcdn.com/capa.jpg",
        }).encode()
        with patch.object(bot, "_fetch_raw", return_value=(api_response, "")) as mock_fetch:
            title, body_html, cover_image = bot.fetch_post("https://x.substack.com", "https://x.substack.com/p/meu-slug")
        mock_fetch.assert_called_once_with("https://x.substack.com/api/v1/posts/meu-slug")
        self.assertEqual(title, "Título da API")
        self.assertEqual(body_html, "<p>Corpo via API.</p>")
        self.assertEqual(cover_image, "https://substackcdn.com/capa.jpg")

    def test_falls_back_to_scraping_html_page_when_api_fails(self):
        html_page = (
            b"<html><head><title>Titulo da Pagina</title></head><body>"
            b'<div class="available-content"><p>Corpo raspado.</p></div></body></html>'
        )
        with patch.object(bot, "_fetch_raw", side_effect=[OSError("api fora do ar"), (html_page, "")]):
            title, body_html, cover_image = bot.fetch_post("https://x.substack.com", "https://x.substack.com/p/meu-slug")
        self.assertEqual(title, "Titulo da Pagina")
        self.assertIn("Corpo raspado.", body_html)
        self.assertEqual(cover_image, "")


class ExtractCitedArticleTest(unittest.TestCase):
    def test_extracts_title_and_url_from_text_link_entity(self):
        titulo = "A Quietude e a Bem-Aventurança"
        text = f"Isso aparece em {titulo}, que fala sobre isso."
        message = {
            "text": text,
            "entities": [{
                "type": "text_link", "offset": text.index(titulo), "length": len(titulo),
                "url": "https://san55.substack.com/p/a-quietude",
            }],
        }
        result = bot.extract_cited_article(message)
        self.assertEqual(result, {
            "titulo": titulo,
            "url": "https://san55.substack.com/p/a-quietude",
        })

    def test_returns_none_without_link_entity(self):
        self.assertIsNone(bot.extract_cited_article({"text": "sem link nenhum aqui", "entities": []}))
        self.assertIsNone(bot.extract_cited_article(None))


class ResolveArticleForSendTest(unittest.TestCase):
    def test_resolves_via_semantic_search_when_query_long_enough(self):
        article = {"titulo": "A Quietude e a Bem-Aventurança", "url": "https://x.com/p/a-quietude"}
        with patch.object(bot.rag, "find_matching_article", return_value=article) as mock_find:
            result = bot._resolve_article_for_send(
                "Mande-me o artigo A Realidade é Tecida de Felicidade.", reply_to=None, bot_id=999,
            )
        self.assertEqual(result, article)
        mock_find.assert_called_once_with("A Realidade é Tecida de Felicidade")

    def test_falls_back_to_cited_article_when_query_too_short(self):
        titulo = "A Quietude e a Bem-Aventurança"
        reply_to = {
            "from": {"id": 999},
            "text": titulo,
            "entities": [{"type": "text_link", "offset": 0, "length": len(titulo), "url": "https://x.com/p/a-quietude"}],
        }
        with patch.object(bot.rag, "find_matching_article") as mock_find:
            result = bot._resolve_article_for_send("manda ele", reply_to=reply_to, bot_id=999)
        mock_find.assert_not_called()
        self.assertEqual(result, {"titulo": "A Quietude e a Bem-Aventurança", "url": "https://x.com/p/a-quietude"})

    def test_returns_none_when_nothing_resolves(self):
        with patch.object(bot.rag, "find_matching_article", return_value=None):
            result = bot._resolve_article_for_send(
                "Mande-me o artigo Um Título Que Não Existe De Verdade", reply_to=None, bot_id=999,
            )
        self.assertIsNone(result)


class SendArticleToChatTest(unittest.TestCase):
    ARTICLE = {"titulo": "A Quietude e a Bem-Aventurança", "url": "https://x.com/p/a-quietude"}

    def test_uses_cached_html_when_available(self):
        with patch.object(bot, "load_article_content", return_value={
                 "abc": {"html": "<h1>Cache</h1>", "link": self.ARTICLE["url"]}}), \
             patch.object(bot, "_fetch_article_html_on_demand") as mock_fetch, \
             patch.object(bot, "send_rich_message") as mock_send_rich:
            bot.send_article_to_chat(555, self.ARTICLE)
        mock_fetch.assert_not_called()
        mock_send_rich.assert_called_once()
        self.assertEqual(mock_send_rich.call_args[0][1], "<h1>Cache</h1>")

    def test_fetches_on_demand_when_not_cached(self):
        with patch.object(bot, "load_article_content", return_value={}), \
             patch.object(bot, "fetch_post", return_value=("Título", "<p>Corpo</p>", "")), \
             patch.object(bot, "substack_base_url", return_value="https://x.com"), \
             patch.object(bot, "send_rich_message") as mock_send_rich:
            bot.send_article_to_chat(555, self.ARTICLE)
        mock_send_rich.assert_called_once()
        self.assertIn("Corpo", mock_send_rich.call_args[0][1])

    def test_falls_back_to_plain_link_message_when_everything_fails(self):
        with patch.object(bot, "load_article_content", return_value={}), \
             patch.object(bot, "fetch_post", side_effect=OSError("fora do ar")), \
             patch.object(bot, "substack_base_url", return_value="https://x.com"), \
             patch.object(bot, "send_rich_message") as mock_send_rich, \
             patch.object(bot, "send_telegram_message") as mock_send:
            bot.send_article_to_chat(555, self.ARTICLE)
        mock_send_rich.assert_not_called()
        mock_send.assert_called_once()
        args, _ = mock_send.call_args
        self.assertEqual(args[0], 555)
        self.assertIn("A Quietude e a Bem-Aventurança", args[1])
        self.assertIn(self.ARTICLE["url"], args[1])


class HandleChatMessageArticleSendTest(unittest.TestCase):
    """Testa a ramificação em handle_chat_message: pedido de envio de
    artigo vs. pergunta normal (via process_updates, ponta a ponta)."""

    def _run(self, text, reply_to_message=None):
        message = {"message_id": 42, "chat": {"id": 555, "type": "private"}, "text": text}
        if reply_to_message:
            message["reply_to_message"] = reply_to_message
        updates = [{"update_id": 4, "message": message}]
        with patch.object(bot, "SUBSCRIBERS_FILE", Path("test_subscribers_tmp.json")), \
             patch.object(bot, "OFFSET_FILE", Path("test_offset_tmp.json")), \
             patch.object(bot, "get_updates", return_value=updates), \
             patch.object(bot, "get_me", return_value=(999, "meubot")), \
             patch.object(bot.rag, "should_respond", return_value=True), \
             patch.object(bot, "send_article_to_chat") as mock_send_article, \
             patch.object(bot.rag, "answer_question_stream", return_value=iter(["resposta normal"])), \
             patch.object(bot, "send_rich_message_draft"), \
             patch.object(bot, "send_rich_message") as mock_send_rich, \
             patch.object(bot, "send_telegram_message") as mock_send:
            try:
                bot.process_updates([])
                return mock_send_article, mock_send_rich, mock_send
            finally:
                bot.SUBSCRIBERS_FILE.unlink(missing_ok=True)
                bot.OFFSET_FILE.unlink(missing_ok=True)

    def test_clear_send_request_triggers_article_send_not_normal_answer(self):
        with patch.object(bot.rag, "find_matching_article", return_value={"titulo": "T", "url": "https://x.com/p/t"}):
            mock_send_article, mock_send_rich, mock_send = self._run("Mande-me o artigo sobre estoicismo.")
        mock_send_article.assert_called_once()
        mock_send_rich.assert_not_called()

    def test_normal_question_does_not_trigger_article_send(self):
        mock_send_article, mock_send_rich, mock_send = self._run("qual o tema do blog?")
        mock_send_article.assert_not_called()
        mock_send_rich.assert_called_once()

    def test_intent_classification_failure_falls_back_to_normal_answer(self):
        with patch.object(bot.rag, "classify_send_intent", side_effect=Exception("api fora do ar")):
            mock_send_article, mock_send_rich, mock_send = self._run("pergunta ambígua qualquer")
        mock_send_article.assert_not_called()
        mock_send_rich.assert_called_once()


class OffTopicLockIntegrationTest(unittest.TestCase):
    """Testa a trava ponta a ponta via process_updates, tanto no caminho de
    grupo (bloqueante) quanto no de privado (streaming)."""

    def setUp(self):
        bot._off_topic_streaks.clear()

    def test_group_chat_locks_after_streak_limit_and_stays_silent(self):
        updates = [
            {"update_id": i, "message": {
                "message_id": i, "chat": {"id": 777, "type": "group"},
                "text": "@meubot pergunta fora do tema",
            }}
            for i in range(1, bot.OFF_TOPIC_STREAK_LIMIT + 3)
        ]
        with patch.object(bot, "SUBSCRIBERS_FILE", Path("test_subscribers_tmp.json")), \
             patch.object(bot, "OFFSET_FILE", Path("test_offset_tmp.json")), \
             patch.object(bot, "get_updates", return_value=updates), \
             patch.object(bot, "get_me", return_value=(999, "meubot")), \
             patch.object(bot.rag, "should_respond", return_value=True), \
             patch.object(bot.rag, "classify_send_intent", return_value=False), \
             patch.object(bot.rag, "answer_question", return_value=bot.rag.REFUSAL_MESSAGE), \
             patch.object(bot, "send_telegram_message") as mock_send:
            try:
                bot.process_updates([])
            finally:
                bot.SUBSCRIBERS_FILE.unlink(missing_ok=True)
                bot.OFFSET_FILE.unlink(missing_ok=True)

        texts_sent = [call.args[1] for call in mock_send.call_args_list]
        self.assertEqual(texts_sent.count(bot.rag.REFUSAL_MESSAGE), bot.OFF_TOPIC_STREAK_LIMIT)
        self.assertEqual(texts_sent.count(bot.OFF_TOPIC_LOCK_MESSAGE), 1)
        self.assertEqual(len(texts_sent), bot.OFF_TOPIC_STREAK_LIMIT + 1)

    def test_private_chat_locks_after_streak_limit_and_stays_silent(self):
        updates = [
            {"update_id": i, "message": {
                "message_id": i, "chat": {"id": 555, "type": "private"},
                "text": "pergunta fora do tema",
            }}
            for i in range(1, bot.OFF_TOPIC_STREAK_LIMIT + 3)
        ]
        with patch.object(bot, "SUBSCRIBERS_FILE", Path("test_subscribers_tmp.json")), \
             patch.object(bot, "OFFSET_FILE", Path("test_offset_tmp.json")), \
             patch.object(bot, "get_updates", return_value=updates), \
             patch.object(bot, "get_me", return_value=(999, "meubot")), \
             patch.object(bot.rag, "should_respond", return_value=True), \
             patch.object(bot.rag, "classify_send_intent", return_value=False), \
             patch.object(bot.rag, "answer_question_stream", side_effect=lambda *a, **kw: iter([bot.rag.REFUSAL_MESSAGE])), \
             patch.object(bot, "send_rich_message_draft"), \
             patch.object(bot, "send_rich_message") as mock_send_rich:
            try:
                bot.process_updates([])
            finally:
                bot.SUBSCRIBERS_FILE.unlink(missing_ok=True)
                bot.OFFSET_FILE.unlink(missing_ok=True)

        texts_sent = [call.args[1] for call in mock_send_rich.call_args_list]
        self.assertEqual(texts_sent.count(bot.rag.REFUSAL_MESSAGE), bot.OFF_TOPIC_STREAK_LIMIT)
        self.assertEqual(texts_sent.count(bot.OFF_TOPIC_LOCK_MESSAGE), 1)
        self.assertEqual(len(texts_sent), bot.OFF_TOPIC_STREAK_LIMIT + 1)


class GetUpdatesTest(unittest.TestCase):
    def _mock_response(self):
        response = Mock()
        response.json.return_value = {"result": []}
        return response

    def test_default_call_does_not_long_poll(self):
        with patch.object(bot.requests, "get", return_value=self._mock_response()) as mock_get:
            bot.get_updates(0)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["timeout"], 0)
        self.assertEqual(kwargs["timeout"], 10)

    def test_long_poll_timeout_widens_the_request_timeout(self):
        with patch.object(bot.requests, "get", return_value=self._mock_response()) as mock_get:
            bot.get_updates(0, timeout=25)
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["timeout"], 25)
        self.assertEqual(kwargs["timeout"], 35)


class FetchAndPublishTest(unittest.TestCase):
    def test_returns_none_when_feed_fetch_fails(self):
        with patch.object(bot, "fetch_feed", side_effect=OSError("boom")):
            self.assertIsNone(bot.fetch_and_publish())

    def test_returns_entries_without_publishing_when_nothing_new(self):
        entry = FakeEntry(title="Já publicado", link="https://example.com/velho")
        entry["id"] = entry["link"]
        feed = bot.feedparser.FeedParserDict(bozo=False, entries=[entry])
        with patch.object(bot, "POSTED_FILE", Path("test_posted_tmp.json")), \
             patch.object(bot, "ARTICLE_CONTENT_FILE", Path("test_article_content_tmp.json")), \
             patch.object(bot, "fetch_feed", return_value=feed), \
             patch.object(bot, "send_telegram_message") as mock_send:
            try:
                bot.save_posted({entry["id"]})
                result = bot.fetch_and_publish()
            finally:
                bot.POSTED_FILE.unlink(missing_ok=True)
                bot.ARTICLE_CONTENT_FILE.unlink(missing_ok=True)
        self.assertEqual(result, [entry])
        mock_send.assert_not_called()

    def test_publishes_new_entry_and_returns_entries_for_caching(self):
        entry = FakeEntry(title="Artigo Novo", link="https://example.com/novo")
        entry["id"] = entry["link"]
        entry["content"] = [{"value": "<p>Corpo do artigo novo.</p>"}]
        feed = bot.feedparser.FeedParserDict(bozo=False, entries=[entry])
        with patch.object(bot, "POSTED_FILE", Path("test_posted_tmp.json")), \
             patch.object(bot, "SUBSCRIBERS_FILE", Path("test_subscribers_tmp.json")), \
             patch.object(bot, "ARTICLE_CONTENT_FILE", Path("test_article_content_tmp.json")), \
             patch.object(bot, "fetch_feed", return_value=feed), \
             patch.object(bot, "send_telegram_message") as mock_send:
            try:
                result = bot.fetch_and_publish()
                posted = bot.load_posted()
                article_content = bot.load_article_content()
            finally:
                bot.POSTED_FILE.unlink(missing_ok=True)
                bot.SUBSCRIBERS_FILE.unlink(missing_ok=True)
                bot.ARTICLE_CONTENT_FILE.unlink(missing_ok=True)
        self.assertEqual(result, [entry])
        self.assertIn(entry["id"], posted)
        mock_send.assert_called_once()
        self.assertEqual(mock_send.call_args[0][0], bot.TELEGRAM_CHANNEL_ID)

        # O post ganhou o botão "Ler artigo completo", e o HTML do artigo foi
        # persistido sob o mesmo short_id usado no callback_data do botão.
        _, kwargs = mock_send.call_args
        short_id = kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"].removeprefix("art:")
        self.assertIn(short_id, article_content)
        self.assertIn("Corpo do artigo novo.", article_content[short_id]["html"])
        self.assertEqual(article_content[short_id]["link"], entry["link"])


class ErrorDetailTest(unittest.TestCase):
    def test_includes_response_body_for_http_error(self):
        response = Mock(text='{"description": "Bad Request: message is too long"}')
        exc = requests.HTTPError("400 Client Error", response=response)
        self.assertIn("message is too long", bot._error_detail(exc))

    def test_plain_exception_returns_str(self):
        self.assertEqual(bot._error_detail(ValueError("boom")), "boom")


class SyncStateToGitTest(unittest.TestCase):
    def test_noop_without_github_token(self):
        with patch.object(bot, "GITHUB_TOKEN", ""), \
             patch.object(bot.subprocess, "run") as mock_run:
            bot.sync_state_to_git()
        mock_run.assert_not_called()

    @staticmethod
    def _git_side_effect(status_output):
        def side_effect(args, **kwargs):
            if args[:2] == ["git", "remote"]:
                return Mock(stdout="https://github.com/example/repo.git\n")
            if args[:2] == ["git", "status"]:
                return Mock(stdout=status_output)
            return Mock(stdout="")
        return side_effect

    def test_skips_commit_when_nothing_changed(self):
        with patch.object(bot, "GITHUB_TOKEN", "tok"), \
             patch.object(bot, "_git_remote_configured", False), \
             patch.object(bot.subprocess, "run", side_effect=self._git_side_effect("")) as mock_run:
            bot.sync_state_to_git()
        called = [call.args[0] for call in mock_run.call_args_list]
        self.assertFalse(any(args[:2] == ["git", "commit"] for args in called))

    def test_commits_and_pushes_when_state_changed(self):
        with patch.object(bot, "GITHUB_TOKEN", "tok"), \
             patch.object(bot, "_git_remote_configured", False), \
             patch.object(bot.subprocess, "run", side_effect=self._git_side_effect(" M posted.json\n")) as mock_run:
            bot.sync_state_to_git()
        called = [call.args[0] for call in mock_run.call_args_list]
        self.assertTrue(any(args[:2] == ["git", "commit"] for args in called))
        self.assertIn(["git", "push"], called)


class WebhookServerTest(unittest.TestCase):
    def setUp(self):
        self.port = self._free_port()
        self.server = bot.ThreadingHTTPServer(("127.0.0.1", self.port), bot._WebhookRequestHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    @staticmethod
    def _free_port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def test_get_triggers_feed_refresh_and_returns_ok(self):
        with patch.object(bot, "_refresh_feed_and_sync_state") as mock_refresh:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", "/")
            response = conn.getresponse()
            body = response.read()
            conn.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(body, b"ok")
        mock_refresh.assert_called_once()

    def test_post_to_wrong_path_returns_404(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", "/not-the-webhook", body=b"{}")
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 404)

    def test_post_with_wrong_secret_returns_403(self):
        with patch.object(bot, "TELEGRAM_WEBHOOK_SECRET", "s3cr3t"):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request(
                "POST", bot.WEBHOOK_PATH, body=b"{}",
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            )
            response = conn.getresponse()
            response.read()
            conn.close()
        self.assertEqual(response.status, 403)

    def test_post_dispatches_message_to_handler(self):
        update = {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/substack"}}
        with patch.object(bot, "dispatch_message") as mock_dispatch:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            body = json.dumps(update).encode("utf-8")
            conn.request("POST", bot.WEBHOOK_PATH, body=body, headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            response.read()
            conn.close()
        self.assertEqual(response.status, 200)
        mock_dispatch.assert_called_once()
        args = mock_dispatch.call_args[0]
        self.assertEqual(args[0], update["message"])

    def test_get_miniapp_serves_the_page_html(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", bot.MINIAPP_PATH)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.getheader("Content-Type"))
        self.assertIn(b"telegram-web-app.js", body)
        self.assertIn(b"Noto+Serif", body)  # tipografia com suporte a diacriticos
        self.assertIn(b"article-grid", body)  # vitrine com capa de artigo

    def test_get_miniapp_articles_serves_the_catalog(self):
        with patch.object(bot, "load_articles_catalog", return_value=[{"titulo": "T", "url": "https://x.com/p/t"}]):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", bot.MINIAPP_ARTICLES_PATH)
            response = conn.getresponse()
            body = response.read()
            conn.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(body), [{"titulo": "T", "url": "https://x.com/p/t"}])

    def test_post_miniapp_chat_rejects_invalid_init_data(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps({"initData": "hash=adulterado&auth_date=123", "question": "oi"}).encode()
        conn.request("POST", bot.MINIAPP_CHAT_PATH, body=payload, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        body = json.loads(response.read())
        conn.close()
        self.assertEqual(response.status, 401)
        self.assertIn("error", body)

    def test_post_miniapp_chat_returns_answer_for_valid_request(self):
        init_data = _build_init_data(
            {"auth_date": str(int(time.time())), "user": json.dumps({"id": 1})},
            bot.TELEGRAM_BOT_TOKEN,
        )
        payload = json.dumps({"initData": init_data, "question": "qual o tema do blog?"}).encode()
        with patch.object(bot.rag, "answer_question", return_value="Resposta do RAG.") as mock_answer:
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("POST", bot.MINIAPP_CHAT_PATH, body=payload, headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            body = json.loads(response.read())
            conn.close()
        self.assertEqual(response.status, 200)
        self.assertEqual(body, {"answer": "Resposta do RAG."})
        mock_answer.assert_called_once()

    def test_post_miniapp_chat_rejects_empty_question(self):
        init_data = _build_init_data(
            {"auth_date": str(int(time.time())), "user": json.dumps({"id": 1})},
            bot.TELEGRAM_BOT_TOKEN,
        )
        payload = json.dumps({"initData": init_data, "question": "   "}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("POST", bot.MINIAPP_CHAT_PATH, body=payload, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        body = json.loads(response.read())
        conn.close()
        self.assertEqual(response.status, 400)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
