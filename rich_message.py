"""Converte o HTML de um artigo do Substack (vindo do feed RSS) para o
subconjunto de HTML que o Telegram entende no método sendRichMessage
(Bot API 10.1/10.2, https://core.telegram.org/bots/api#rich-html-style).

Este módulo é intencionalmente independente do bot.py (nada de token, nada de
chamada HTTP) — só texto entrando, texto saindo — pra ficar fácil de testar
e não criar import circular (bot.py é quem sabe falar com a API do Telegram)."""

import html
import re
from html.parser import HTMLParser

# Tags que o Telegram aceita em InputRichMessage.html (ver "Rich HTML style"
# na doc). Qualquer tag fora dessa lista é descartada na conversão, mas o
# texto de dentro dela continua no resultado (ex: <div>, <span> viram texto
# solto) — exceto as de DROP_CONTENT_TAGS, cujo conteúdo também é descartado.
ALLOWED_TAGS = {
    "a", "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "code", "mark", "sub", "sup", "tg-spoiler",
    "h1", "h2", "h3", "h4", "h5", "h6", "p", "pre", "footer", "hr", "br",
    "ul", "ol", "li", "input",
    "blockquote", "aside", "cite",
    "img", "video", "audio", "figure", "figcaption",
    "table", "tr", "td", "th", "caption",
    "details", "summary",
    "tg-map", "tg-time", "tg-math", "tg-math-block", "tg-reference", "tg-emoji",
}

# Tags que nunca fazem sentido como texto solto (scripts, embeds de vídeo
# via iframe etc.) — o conteúdo delas é descartado inteiro, não só a tag.
DROP_CONTENT_TAGS = {"script", "style", "iframe", "noscript", "svg", "object", "embed", "form"}

# Tags que não têm par de fechamento (não empilham no _open_stack).
VOID_TAGS = {"img", "hr", "br", "input", "tg-map"}

ATTR_WHITELIST = {
    "a": {"href", "name"},
    "img": {"src"},
    "video": {"src"},
    "audio": {"src"},
    "ol": {"start", "type", "reversed"},
    "li": {"value", "type"},
    "input": {"type", "checked"},
    "table": {"bordered", "striped"},
    "td": {"colspan", "rowspan", "align", "valign"},
    "th": {"colspan", "rowspan", "align", "valign"},
    "details": {"open"},
    "tg-map": {"lat", "long", "zoom"},
    "tg-time": {"unix", "format"},
    "tg-emoji": {"emoji-id"},
    "tg-reference": {"name"},
}

# Esquemas de link aceitos pelo Telegram em <a href>; qualquer outra coisa
# (javascript:, data:, etc.) faz o atributo ser descartado.
_SAFE_HREF_RE = re.compile(r"^(https?://|mailto:|tel:|tg://|#)", re.IGNORECASE)


class _RichHtmlSanitizer(HTMLParser):
    """Reescreve o HTML de entrada mantendo só as tags/atributos que o
    sendRichMessage do Telegram suporta. Tags desconhecidas (ex: <div> de
    wrapper do Substack) são removidas, mas o conteúdo dentro delas continua
    fluindo — exceto para DROP_CONTENT_TAGS, onde o conteúdo some junto."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._drop_depth = 0
        self._open_stack = []

    def handle_starttag(self, tag, attrs):
        self._open(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs, self_closing=True)

    def _open(self, tag, attrs, self_closing):
        if self._drop_depth:
            if tag in DROP_CONTENT_TAGS:
                self._drop_depth += 1
            return
        if tag in DROP_CONTENT_TAGS:
            self._drop_depth += 1
            return
        if tag not in ALLOWED_TAGS:
            return
        attr_str = "".join(
            f' {name}="{html.escape(value, quote=True)}"'
            for name, value in self._filter_attrs(tag, attrs)
        )
        self.out.append(f"<{tag}{attr_str}>")
        if not self_closing and tag not in VOID_TAGS:
            self._open_stack.append(tag)

    def _filter_attrs(self, tag, attrs):
        allowed = ATTR_WHITELIST.get(tag, set())
        kept = []
        for name, value in attrs:
            if name not in allowed or value is None:
                continue
            if tag == "a" and name == "href" and not _SAFE_HREF_RE.match(value):
                continue
            if tag in ("img", "video", "audio") and name == "src" and not value.lower().startswith(("http://", "https://")):
                continue
            kept.append((name, value))
        return kept

    def handle_endtag(self, tag):
        if self._drop_depth:
            if tag in DROP_CONTENT_TAGS:
                self._drop_depth -= 1
            return
        if tag not in ALLOWED_TAGS or tag not in self._open_stack:
            return
        if self._open_stack[-1] == tag:
            self._open_stack.pop()
            self.out.append(f"</{tag}>")
        else:
            # HTML mal-formado (comum em conteúdo exportado de editores tipo
            # Substack): fecha na marra todas as tags abertas depois dela.
            idx = len(self._open_stack) - 1 - self._open_stack[::-1].index(tag)
            for open_tag in reversed(self._open_stack[idx:]):
                self.out.append(f"</{open_tag}>")
            del self._open_stack[idx:]

    def handle_data(self, data):
        if self._drop_depth:
            return
        # Só &, < e > precisam de escape para o parser do Telegram (ver
        # markdown_to_telegram_html em bot.py — mesmo raciocínio de não
        # escapar aspas à toa).
        self.out.append(html.escape(data, quote=False))

    def get_html(self):
        for open_tag in reversed(self._open_stack):
            self.out.append(f"</{open_tag}>")
        self._open_stack.clear()
        return "".join(self.out)


def sanitize_article_html(raw_html):
    """Converte o HTML bruto de um artigo (campo content/summary do feed)
    para o subconjunto suportado por InputRichMessage.html."""
    parser = _RichHtmlSanitizer()
    parser.feed(raw_html or "")
    return parser.get_html()


# Limite real do Telegram é 32768 caracteres; a margem é pra sobrar espaço
# pro título, pro link final e pro aviso de truncamento sem passar do limite.
ARTICLE_HTML_MAX_LENGTH = 32000


def build_full_article_html(title, link, raw_body_html):
    """Monta o HTML final (título + corpo sanitizado + link pro Substack)
    pra mandar via sendRichMessage. Se passar do limite de caracteres, corta
    o corpo (nunca o título/link) e avisa que o artigo continua no Substack."""
    title_html = html.escape(title or "", quote=False)
    body_html = sanitize_article_html(raw_body_html)
    footer = f'<p><a href="{html.escape(link, quote=True)}">Ler no Substack →</a></p>' if link else ""

    full = f"<h1>{title_html}</h1>\n{body_html}\n{footer}"
    if len(full) <= ARTICLE_HTML_MAX_LENGTH:
        return full

    truncated_note = (
        f'<p>(artigo truncado — <a href="{html.escape(link, quote=True)}">'
        "continue lendo no Substack →</a>)</p>" if link else "<p>(artigo truncado)</p>"
    )
    fixed_length = len(f"<h1>{title_html}</h1>\n") + len(f"\n{truncated_note}")
    body_html = body_html[: max(0, ARTICLE_HTML_MAX_LENGTH - fixed_length)]
    return f"<h1>{title_html}</h1>\n{body_html}\n{truncated_note}"
