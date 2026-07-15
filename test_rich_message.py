"""Testes do sanitizador de HTML pra sendRichMessage (rich_message.py)."""

import unittest

import rich_message


class SanitizeArticleHtmlTest(unittest.TestCase):
    def test_keeps_allowed_tags(self):
        raw = "<h2>Título</h2><p>Um <strong>parágrafo</strong> com <em>ênfase</em>.</p>"
        result = rich_message.sanitize_article_html(raw)
        self.assertEqual(result, raw)

    def test_strips_div_wrapper_but_keeps_children(self):
        raw = '<div class="captioned-image-container"><figure><img src="https://x.com/a.jpg"/><figcaption>Legenda</figcaption></figure></div>'
        result = rich_message.sanitize_article_html(raw)
        self.assertNotIn("<div", result)
        self.assertIn('<figure><img src="https://x.com/a.jpg"><figcaption>Legenda</figcaption></figure>', result)

    def test_drops_iframe_and_its_content_entirely(self):
        raw = "<p>Antes</p><iframe src=\"https://youtube.com/embed/x\">texto interno do iframe</iframe><p>Depois</p>"
        result = rich_message.sanitize_article_html(raw)
        self.assertNotIn("iframe", result)
        self.assertNotIn("texto interno do iframe", result)
        self.assertIn("<p>Antes</p>", result)
        self.assertIn("<p>Depois</p>", result)

    def test_keeps_list_and_blockquote(self):
        raw = "<ul><li>Item 1</li><li>Item 2</li></ul><blockquote>Uma citação</blockquote>"
        result = rich_message.sanitize_article_html(raw)
        self.assertEqual(result, raw)

    def test_strips_javascript_href(self):
        raw = '<a href="javascript:alert(1)">clique</a>'
        result = rich_message.sanitize_article_html(raw)
        self.assertNotIn("javascript:", result)
        self.assertIn("<a>clique</a>", result)

    def test_keeps_http_href(self):
        raw = '<a href="https://san55.substack.com/p/artigo">link</a>'
        result = rich_message.sanitize_article_html(raw)
        self.assertIn('href="https://san55.substack.com/p/artigo"', result)

    def test_strips_non_http_image_src(self):
        raw = '<img src="data:image/png;base64,AAAA"/>'
        result = rich_message.sanitize_article_html(raw)
        self.assertNotIn("data:image", result)
        self.assertIn("<img>", result)

    def test_escapes_special_chars_in_text(self):
        raw = "<p>Menos que <isso> & mais.</p>"
        result = rich_message.sanitize_article_html("<p>Menos que &lt;isso&gt; &amp; mais.</p>")
        self.assertIn("&lt;isso&gt;", result)
        self.assertIn("&amp;", result)

    def test_closes_unclosed_open_tags(self):
        raw = "<p>Parágrafo sem fechar"
        result = rich_message.sanitize_article_html(raw)
        self.assertEqual(result, "<p>Parágrafo sem fechar</p>")


class BuildFullArticleHtmlTest(unittest.TestCase):
    def test_includes_title_body_and_substack_link(self):
        result = rich_message.build_full_article_html(
            title="Meu Artigo",
            link="https://san55.substack.com/p/meu-artigo",
            raw_body_html="<p>Corpo do artigo.</p>",
        )
        self.assertIn("<h1>Meu Artigo</h1>", result)
        self.assertIn("<p>Corpo do artigo.</p>", result)
        self.assertIn('href="https://san55.substack.com/p/meu-artigo"', result)

    def test_truncates_long_body_and_keeps_title_and_link(self):
        long_body = "<p>" + ("palavra " * 10000) + "</p>"
        result = rich_message.build_full_article_html(
            title="Artigo Longo",
            link="https://san55.substack.com/p/artigo-longo",
            raw_body_html=long_body,
        )
        self.assertLessEqual(len(result), rich_message.ARTICLE_HTML_MAX_LENGTH + 200)
        self.assertIn("<h1>Artigo Longo</h1>", result)
        self.assertIn("truncado", result)
        self.assertIn("https://san55.substack.com/p/artigo-longo", result)


if __name__ == "__main__":
    unittest.main()
