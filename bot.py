"""Publica os artigos mais recentes de um feed RSS do Substack em um canal do Telegram
e nos assinantes que interagiram com o bot no privado, além de responder a comandos."""

import gzip
import hashlib
import html
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv

import miniapp
import rag
import rich_message

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
SUBSTACK_RSS_URL = os.getenv("SUBSTACK_RSS_URL")

BASE_DIR = Path(__file__).parent
# No plano free do Render não dá pra anexar um Disk persistente, então os
# arquivos de estado voltam a viver dentro do repo (ver sync_state_to_git) e
# só saem daqui se DATA_DIR for setada explicitamente (ex: deploy num plano
# pago com Disk).
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
POSTED_FILE = DATA_DIR / "posted.json"
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
OFFSET_FILE = DATA_DIR / "update_offset.json"
# Guarda o HTML já pronto (sanitizado pro sendRichMessage) de cada artigo
# publicado, indexado pelo short_id usado no callback_data do botão "Ler
# artigo completo" — callback_data tem limite de 64 bytes, então não dá pra
# usar a URL do artigo direto. Cresce um pouco a cada post, mas cada entrada
# é só texto (nada de embeddings), então é bem mais leve que articles_index.json.
ARTICLE_CONTENT_FILE = DATA_DIR / "article_content.json"
STATE_FILENAMES = ("posted.json", "subscribers.json", "update_offset.json", "article_content.json")
# Catálogo leve (título+URL) da vitrine de artigos do Mini App — gerado por
# index_articles.py e versionado no repo, como articles_index.json; não é
# "estado" do bot, então não entra em STATE_FILENAMES.
ARTICLES_CATALOG_FILE = BASE_DIR / "articles_catalog.json"

SUMMARY_MAX_LENGTH = 500
RECENT_ARTICLES_COUNT = 5
SUBSTACK_SUBSCRIBE_URL = "https://san55.substack.com/subscribe"

# Web Service free no Render dorme sem tráfego HTTP e não tem long polling
# 24/7 de verdade, então o bot roda em modo webhook lá: Telegram empurra cada
# mensagem via POST, e um GET (do keep-alive externo) aproveita pra checar o
# feed RSS. RENDER_EXTERNAL_URL é setada automaticamente pelo Render em Web
# Services; sem ela (dev local), cai no polling de sempre.
PUBLIC_URL = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL")
WEBHOOK_PATH = f"/webhook/{TELEGRAM_BOT_TOKEN}"
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")

# GITHUB_TOKEN (Personal Access Token com permissão de push neste repo) é a
# persistência usada no free tier, que não tem Disk: os arquivos de estado
# são commitados de volta pro GitHub em vez de ficarem só no disco efêmero
# do container.
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
STATE_SYNC_INTERVAL_SECONDS = int(os.getenv("STATE_SYNC_INTERVAL_SECONDS", "60"))

TELEGRAM_POLL_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_POLL_TIMEOUT_SECONDS", "25"))
FEED_CHECK_INTERVAL_SECONDS = int(os.getenv("FEED_CHECK_INTERVAL_SECONDS", "300"))

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"

TAG_RE = re.compile(r"<[^>]+>")
IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)

# Caracteres de controle proibidos pela especificação XML 1.0; feeds do Substack
# às vezes carregam algum (colado de Word/Google Docs) e quebram o parser estrito.
INVALID_XML_CHARS_RE = re.compile(rb"[\x00-\x08\x0B\x0C\x0E-\x1F]")

# Mesmos headers que o feedparser usa por padrão ao buscar URLs diretamente.
# Faltar Accept/Accept-encoding/A-IM (mesmo com o User-Agent certo) fez o
# Substack responder 403 Forbidden a um pedido "genérico" demais.
USER_AGENT = getattr(feedparser, "USER_AGENT", "feedparser/6.0.11 +https://github.com/kurtmckee/feedparser/")
try:
    from feedparser.http import ACCEPT_HEADER
except ImportError:
    ACCEPT_HEADER = (
        "application/atom+xml,application/rdf+xml,application/rss+xml,"
        "application/x-netcdf,application/xml;q=0.9,text/xml;q=0.2,*/*;q=0.1"
    )
FETCH_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": ACCEPT_HEADER,
    "Accept-encoding": "gzip, deflate",
    "A-IM": "feed",
}

# Cloudflare (usado pelo Substack) bloqueia por reputação de IP/ASN os ranges
# de datacenter das runners do GitHub Actions com 403, mesmo com headers
# idênticos aos de um navegador (testado: proxies genéricos de CORS como
# allorigins.win/codetabs/corsproxy.io são instáveis demais pra depender
# sozinhos). Nesse caso específico, buscamos o mesmo feed via rss2json.com
# (que faz a requisição a partir da própria infra dele) e adaptamos o JSON
# pro mesmo formato de entries que o feedparser produziria.
PROXY_FETCH_URL = "https://api.rss2json.com/v1/api.json?rss_url={url}"


# ---------------------------------------------------------------------------
# Parsing de artigos
# ---------------------------------------------------------------------------

def strip_html(raw_html):
    """Remove tags HTML de um texto e decodifica entidades (&amp;, &quot; etc)."""
    if not raw_html:
        return ""
    text = TAG_RE.sub("", raw_html)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate_summary(text, max_length=SUMMARY_MAX_LENGTH):
    """Corta o texto em max_length caracteres, terminando em uma palavra inteira."""
    text = text.strip()
    if len(text) <= max_length:
        return text
    truncated = text[:max_length].rsplit(" ", 1)[0]
    return truncated.rstrip(".,;:") + "..."


def extract_image_url(entry):
    """Tenta achar a imagem de capa de uma entry de feed em vários formatos possíveis."""
    media_content = entry.get("media_content") or []
    for media in media_content:
        if media.get("url"):
            return media["url"]

    media_thumbnail = entry.get("media_thumbnail") or []
    for media in media_thumbnail:
        if media.get("url"):
            return media["url"]

    for enclosure in entry.get("enclosures") or []:
        enclosure_type = enclosure.get("type", "")
        if enclosure_type.startswith("image/") and enclosure.get("href"):
            return enclosure["href"]

    for field in ("content", "summary"):
        value = entry.get(field)
        if field == "content" and value:
            value = value[0].get("value", "")
        if value:
            match = IMG_SRC_RE.search(value)
            if match:
                return match.group(1)

    return None


def build_caption(entry):
    """Monta o texto (HTML) do post: título em negrito, resumo e link final."""
    title = html.escape(strip_html(entry.get("title", "")))
    link = entry.get("link", "")

    raw_summary = entry.get("summary", "")
    if not raw_summary and entry.get("content"):
        raw_summary = entry["content"][0].get("value", "")

    summary = truncate_summary(strip_html(raw_summary))
    summary = html.escape(summary)

    caption = f"<b>{title}</b>\n\n{summary}"
    if link:
        caption += f'\n\n<a href="{html.escape(link)}">Leia o artigo completo →</a>'
    return caption


def extract_categories(entries):
    """Lista as categorias (tags) únicas presentes nas entries do feed, na ordem em que aparecem."""
    categories = []
    seen = set()
    for entry in entries:
        for tag in entry.get("tags") or []:
            term = (tag.get("term") or "").strip()
            if term and term.lower() not in seen:
                seen.add(term.lower())
                categories.append(term)
    return categories


def entry_id(entry):
    return entry.get("id") or entry.get("link")


def sanitize_xml_bytes(data):
    """Remove bytes de controle inválidos em XML 1.0 que quebram o parser estrito."""
    return INVALID_XML_CHARS_RE.sub(b"", data)


def decompress_response(content, content_encoding):
    """Descompacta o corpo da resposta se o servidor mandou gzip/deflate."""
    content_encoding = (content_encoding or "").lower()
    if "gzip" in content_encoding:
        return gzip.GzipFile(fileobj=io.BytesIO(content)).read()
    if "deflate" in content_encoding:
        return zlib.decompress(content)
    return content


def _fetch_raw(url):
    request = urllib.request.Request(url, headers=FETCH_HEADERS)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(), response.headers.get("Content-Encoding", "")


def _proxy_item_to_entry(item):
    """Converte um item do JSON do rss2json.com pro mesmo formato de entry
    (campos acessados via .get) que o resto do código espera do feedparser."""
    enclosure = item.get("enclosure") or {}
    enclosures = []
    if enclosure.get("link"):
        enclosures.append({"href": enclosure["link"], "type": enclosure.get("type", "")})
    return {
        "title": item.get("title", ""),
        "link": item.get("link", ""),
        "id": item.get("guid") or item.get("link", ""),
        "summary": item.get("description", ""),
        "content": [{"value": item["content"]}] if item.get("content") else [],
        "enclosures": enclosures,
        "tags": [{"term": c} for c in item.get("categories") or []],
    }


def _fetch_via_proxy(url):
    proxy_url = PROXY_FETCH_URL.format(url=urllib.parse.quote(url, safe=""))
    content, content_encoding = _fetch_raw(proxy_url)
    content = decompress_response(content, content_encoding)
    data = json.loads(content)
    entries = [_proxy_item_to_entry(item) for item in data.get("items") or []]
    return feedparser.FeedParserDict(entries=entries, bozo=False)


def fetch_feed(url):
    """Busca o RSS manualmente (via urllib, com os mesmos headers que o
    feedparser usa) e sanitiza o XML antes de repassar pro feedparser.

    Se o Substack responder 403 (bloqueio de IP de datacenter, ver
    PROXY_FETCH_URL acima), busca o mesmo feed através do proxy."""
    try:
        content, content_encoding = _fetch_raw(url)
    except urllib.error.HTTPError as exc:
        if exc.code != 403:
            raise
        return _fetch_via_proxy(url)
    content = decompress_response(content, content_encoding)
    return feedparser.parse(sanitize_xml_bytes(content))


def substack_base_url():
    if not SUBSTACK_RSS_URL:
        raise RuntimeError("SUBSTACK_RSS_URL não configurada (.env).")
    return SUBSTACK_RSS_URL.rsplit("/feed", 1)[0].rstrip("/")


def slug_from_url(post_url):
    return post_url.rstrip("/").rsplit("/p/", 1)[-1]


def fetch_post(base_url, post_url):
    """Busca título + HTML completo + capa de um post via API pública do
    Substack (usada pelo próprio frontend), com fallback pra raspar a
    página HTML direto se a API não retornar o esperado. Mora em bot.py
    (não em index_articles.py, de onde foi movida) porque agora também é
    usada pelo chat pra buscar sob demanda o artigo completo quando alguém
    pede pra receber um post que ainda não está em article_content.json
    (ver _fetch_article_html_on_demand). Devolve o HTML bruto — quem indexa
    (index_articles.py) que decide extrair só o texto puro. Retorna
    (título, html, url_da_capa) — url_da_capa é "" quando cai no fallback
    de raspagem (a API não estruturada não tem um campo equivalente fácil
    de achar sem duplicar a lógica de extração de imagem do corpo)."""
    slug = slug_from_url(post_url)
    api_url = f"{base_url}/api/v1/posts/{slug}"
    try:
        content, content_encoding = _fetch_raw(api_url)
        content = decompress_response(content, content_encoding)
        data = json.loads(content)
        title = data.get("title", "")
        body_html = data.get("body_html") or ""
        if title and body_html:
            return title, body_html, data.get("cover_image") or ""
    except Exception as exc:
        print(f"Aviso: API falhou pra {post_url} ({exc}); tentando HTML direto.", file=sys.stderr)

    content, content_encoding = _fetch_raw(post_url)
    html_page = decompress_response(content, content_encoding).decode("utf-8", "ignore")
    title_match = re.search(r"<title>(.*?)</title>", html_page, re.IGNORECASE | re.DOTALL)
    title = strip_html(title_match.group(1)) if title_match else slug
    body_match = re.search(
        r'<div[^>]+class="[^"]*available-content[^"]*"[^>]*>(.*?)</div>\s*</div>',
        html_page,
        re.IGNORECASE | re.DOTALL,
    )
    body_html = body_match.group(1) if body_match else html_page
    return title, body_html, ""


# ---------------------------------------------------------------------------
# Persistência local (posted.json, subscribers.json, update_offset.json)
# ---------------------------------------------------------------------------

def _load_json(path, default):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_posted():
    return set(_load_json(POSTED_FILE, []))


def save_posted(posted_ids):
    _save_json(POSTED_FILE, sorted(posted_ids))


def load_subscribers():
    return set(_load_json(SUBSCRIBERS_FILE, []))


def save_subscribers(subscriber_ids):
    _save_json(SUBSCRIBERS_FILE, sorted(subscriber_ids))


def load_offset():
    return _load_json(OFFSET_FILE, {}).get("offset", 0)


def save_offset(offset):
    _save_json(OFFSET_FILE, {"offset": offset})


def load_article_content():
    return _load_json(ARTICLE_CONTENT_FILE, {})


def save_article_content(content_by_short_id):
    _save_json(ARTICLE_CONTENT_FILE, content_by_short_id)


def load_articles_catalog():
    return _load_json(ARTICLES_CATALOG_FILE, [])


def article_short_id(entry_identifier):
    """ID curto e estável pro callback_data do botão 'Ler artigo completo'
    (limite de 64 bytes do Telegram não deixa usar a URL do artigo direto)."""
    return hashlib.sha256(entry_identifier.encode("utf-8")).hexdigest()[:16]


_git_remote_configured = False


def _configure_git_remote_with_token():
    """Injeta o GITHUB_TOKEN na URL do remote 'origin' e configura uma
    identidade de commit. Só roda uma vez por processo."""
    global _git_remote_configured
    if _git_remote_configured:
        return
    remote_url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=BASE_DIR, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if remote_url.startswith("https://") and "@" not in remote_url:
        authed_url = remote_url.replace("https://", f"https://x-access-token:{GITHUB_TOKEN}@", 1)
        subprocess.run(["git", "remote", "set-url", "origin", authed_url], cwd=BASE_DIR, check=True)
    subprocess.run(["git", "config", "user.name", "sansanthology-bot"], cwd=BASE_DIR, check=True)
    subprocess.run(
        ["git", "config", "user.email", "sansanthology-bot@users.noreply.github.com"],
        cwd=BASE_DIR, check=True,
    )
    _git_remote_configured = True


def sync_state_to_git():
    """Commita e dá push nos arquivos de estado pro GitHub — é a persistência
    usada no plano free do Render, que não tem Disk. Chamada periodicamente
    (nunca a cada mensagem); falhas aqui não devem derrubar o bot."""
    if not GITHUB_TOKEN:
        return
    try:
        _configure_git_remote_with_token()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", *STATE_FILENAMES],
            cwd=BASE_DIR, capture_output=True, text=True, check=True,
        ).stdout
        if not status.strip():
            return
        subprocess.run(["git", "add", "-f", "--", *STATE_FILENAMES], cwd=BASE_DIR, check=True)
        subprocess.run(
            ["git", "commit", "-m", "Atualiza estado do bot [skip ci]"],
            cwd=BASE_DIR, check=True,
        )
        subprocess.run(["git", "push"], cwd=BASE_DIR, check=True)
        print("Estado sincronizado com o GitHub.")
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
        print(f"Falha ao sincronizar estado com o GitHub: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Chamadas à Bot API do Telegram
# ---------------------------------------------------------------------------

TELEGRAM_TEXT_MAX_LENGTH = 3500  # margem abaixo do limite de 4096 do Telegram; escapar HTML pode expandir um pouco o texto


def _error_detail(exc):
    """Descreve uma exceção incluindo o corpo da resposta HTTP, quando houver
    — um 400/403 da API do Telegram raramente diz o motivo só com str(exc)."""
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"{exc} | corpo: {exc.response.text[:500]}"
    return str(exc)


def send_telegram_message(chat_id, text, reply_to_message_id=None, message_thread_id=None, reply_markup=None):
    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN, method="sendMessage")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    response = requests.post(url, data=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def get_me():
    """Busca id e username do próprio bot (usado pra detectar menção/reply em grupos)."""
    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN, method="getMe")
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    result = response.json().get("result", {})
    return result.get("id"), result.get("username")


def send_telegram_photo(chat_id, photo_url, caption, reply_markup=None):
    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN, method="sendPhoto")
    payload = {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    response = requests.post(url, data=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def send_rich_message(chat_id, rich_html, reply_to_message_id=None, message_thread_id=None):
    """Manda uma mensagem rica via sendRichMessage (Bot API 10.1+) — usada
    tanto pro artigo completo (rich_message.build_full_article_html) quanto
    pra persistir a resposta final de um chat que foi transmitido aos poucos
    via sendRichMessageDraft (ver send_rich_message_draft/handle_chat_message).
    Diferente das outras chamadas aqui, manda o corpo como JSON puro (em vez
    de form-encoded): rich_message é um objeto aninhado, e o próprio Telegram
    aceita application/json no lugar de multipart pra métodos sem upload de
    arquivo — mais simples que serializar o objeto dentro de um campo de form."""
    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN, method="sendRichMessage")
    payload = {
        "chat_id": chat_id,
        "rich_message": {"html": rich_html},
    }
    if reply_to_message_id is not None:
        payload["reply_parameters"] = {"message_id": reply_to_message_id}
    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def send_rich_message_draft(chat_id, draft_id, rich_html, message_thread_id=None):
    """Transmite uma versão parcial (ainda sendo gerada) de uma resposta via
    sendRichMessageDraft. É efêmero — expira sozinho em 30s — por isso quem
    chama isso PRECISA terminar com um send_rich_message pra persistir a
    versão final (ver _stream_chat_reply). Só funciona em chat privado."""
    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN, method="sendRichMessageDraft")
    payload = {
        "chat_id": chat_id,
        "draft_id": draft_id,
        "rich_message": {"html": rich_html},
    }
    if message_thread_id is not None:
        payload["message_thread_id"] = message_thread_id
    response = requests.post(url, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def answer_callback_query(callback_query_id, text=None, show_alert=False):
    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN, method="answerCallbackQuery")
    payload = {"callback_query_id": callback_query_id}
    if text is not None:
        payload["text"] = text
        payload["show_alert"] = show_alert
    response = requests.post(url, data=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def set_webhook(url):
    """Registra o endpoint que o Telegram vai chamar (POST) a cada mensagem
    nova. Chamado uma vez ao subir o servidor em modo webhook."""
    api_url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN, method="setWebhook")
    payload = {"url": url, "allowed_updates": json.dumps(["message", "callback_query"])}
    if TELEGRAM_WEBHOOK_SECRET:
        payload["secret_token"] = TELEGRAM_WEBHOOK_SECRET
    response = requests.post(api_url, data=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def set_chat_menu_button(web_app_url):
    """Registra o botão de menu persistente (ícone ao lado do campo de
    mensagem, em chat privado) que abre o Mini App. Chamado uma vez ao
    subir o servidor, junto com o webhook."""
    api_url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN, method="setChatMenuButton")
    payload = {"menu_button": json.dumps({
        "type": "web_app", "text": "Artigos", "web_app": {"url": web_app_url},
    })}
    response = requests.post(api_url, data=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def get_updates(offset, timeout=0):
    """Busca updates novos. Com timeout > 0, faz long polling: a chamada HTTP
    fica pendurada até chegar mensagem ou o timeout do Telegram expirar."""
    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN, method="getUpdates")
    params = {"timeout": timeout, "allowed_updates": json.dumps(["message", "callback_query"])}
    if offset:
        params["offset"] = offset
    response = requests.get(url, params=params, timeout=timeout + 10)
    response.raise_for_status()
    return response.json().get("result", [])


# ---------------------------------------------------------------------------
# Textos dos comandos
# ---------------------------------------------------------------------------

def build_start_message():
    return (
        "🎉 Pronto! Agora você vai receber aqui, no privado, os artigos novos "
        "assim que forem publicados.\n\n"
        "Comandos disponíveis:\n"
        "/recentes — últimos artigos publicados\n"
        "/categorias — categorias disponíveis\n"
        "/substack — link para assinar o Substack\n"
        "/sugestao — sugerir um assunto\n"
        "/stop — cancelar sua inscrição"
    )


def build_stop_message():
    return "Você não vai mais receber os artigos por aqui. Se mudar de ideia, é só mandar /start de novo."


def build_substack_message():
    return f"📬 Inscreva-se no Substack para receber os artigos por e-mail:\n{SUBSTACK_SUBSCRIBE_URL}"


def build_sugestao_message():
    return "💡 Tem sugestão de assunto? Basta mandar aqui mesmo, é só me mandar uma mensagem!"


def build_categories_message(entries):
    categories = extract_categories(entries)
    if not categories:
        return "Nenhuma categoria encontrada no momento."
    bullets = "\n".join(f"• {html.escape(c)}" for c in categories)
    return f"📚 Categorias disponíveis:\n\n{bullets}"


def build_recent_articles_message(entries, count=RECENT_ARTICLES_COUNT):
    if not entries:
        return "Nenhum artigo encontrado no momento."
    lines = []
    for i, entry in enumerate(entries[:count], start=1):
        title = html.escape(strip_html(entry.get("title", "(sem título)")))
        link = html.escape(entry.get("link", ""))
        lines.append(f'{i}. <a href="{link}">{title}</a>')
    return "📰 Últimos artigos:\n\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Formatação da resposta do RAG pro Telegram
# ---------------------------------------------------------------------------
# O Claude devolve a resposta em Markdown (**negrito**, *itálico*, `código`,
# # títulos), mas o Telegram não entende Markdown nesse modo — ele é enviado
# com parse_mode HTML (ver send_telegram_message). Convertemos aqui pras tags
# que o Telegram suporta (https://core.telegram.org/bots/api#html-style) em
# vez de simplesmente escapar tudo, o que deixava os marcadores (**texto**)
# aparecendo literalmente na mensagem.

CODE_BLOCK_RE = re.compile(r"```(?:\w*\n)?(.*?)```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
HEADER_RE = re.compile(r"^#{1,6}[ \t]*(.+)$", re.MULTILINE)
BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
# Só http(s): o SYSTEM_PROMPT do rag.py pede citação como [Título](url) usando
# as URLs dos próprios artigos indexados, então não há motivo legítimo pra um
# esquema diferente aparecer aqui.
LINK_RE = re.compile(r"\[([^\[\]]+)\]\((https?://[^\s()]+)\)")


def markdown_to_telegram_html(text):
    """Converte o Markdown básico da resposta do Claude para as tags HTML
    que o Telegram entende. Escapa o texto primeiro (então & < > viram
    entidades) e só depois substitui os marcadores de Markdown — que não são
    caracteres especiais de HTML — pelas tags correspondentes. Marcadores sem
    par (ex: truncados no meio por truncate_summary) ficam como texto literal
    em vez de virar tag desbalanceada, porque as regexes exigem abertura E
    fechamento."""
    # quote=False: o Telegram só exige escapar &, < e > fora de tags; aspas e
    # apóstrofos não precisam (e virariam &quot;/&#x27; literais na tela, já
    # que o parser dele não promete decodificar entidades de aspas).
    text = html.escape(text, quote=False)
    text = CODE_BLOCK_RE.sub(lambda m: f"<pre>{m.group(1)}</pre>", text)
    text = INLINE_CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", text)
    text = HEADER_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = BOLD_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = ITALIC_RE.sub(lambda m: f"<i>{m.group(1)}</i>", text)
    text = LINK_RE.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    return text


# ---------------------------------------------------------------------------
# Processamento de comandos recebidos (getUpdates)
# ---------------------------------------------------------------------------

def _send_chat_fallback_error(chat_id, message):
    try:
        send_telegram_message(
            chat_id,
            "😕 Não consegui responder agora, tenta de novo em alguns minutos.",
            reply_to_message_id=message.get("message_id"),
            message_thread_id=message.get("message_thread_id"),
        )
    except requests.RequestException:
        pass


# sendRichMessageDraft é efêmero (expira em 30s) e feito pra transmitir texto
# sendo gerado aos poucos — mandar uma atualização a cada token seria
# excessivo (rate limit da API, e o usuário nem percebe diferença). Só manda
# uma atualização quando passar do intervalo mínimo E tiver texto novo
# suficiente desde a última.
STREAM_UPDATE_MIN_CHARS = 120
STREAM_UPDATE_MIN_INTERVAL_SECONDS = 0.7


# ---------------------------------------------------------------------------
# Trava de perguntas fora do tema em sequência
# ---------------------------------------------------------------------------
# O guardrail de relevância (rag.py) já nunca chama o Claude quando a
# pergunta foge do tema — mas continua respondendo a recusa de novo a cada
# mensagem, o que é chato pra quem insiste e também segue gastando a
# chamada de embedding a cada vez. Depois de OFF_TOPIC_STREAK_LIMIT recusas
# seguidas no mesmo chat, avisa uma vez e para de responder (silêncio total)
# até a pergunta voltar a ser sobre os artigos. É estado só em memória —
# não precisa sobreviver a um restart, o pior caso é a contagem zerar.
OFF_TOPIC_STREAK_LIMIT = 3
_off_topic_streaks = {}

OFF_TOPIC_LOCK_MESSAGE = (
    "😊 Percebi que as últimas perguntas fugiram dos temas dos meus artigos. "
    "Pra não ficar recusando à toa, vou ficar quietinho por aqui até você "
    "mandar algo relacionado a eles — pode perguntar quando quiser!"
)


def _apply_off_topic_lock(chat_id, answer_text):
    """Decide o que (se algo) mandar de volta, considerando o histórico de
    recusas por fugir do tema nesse chat. Retorna o texto a enviar, ou None
    se a trava estiver ativa e for pra ficar em silêncio."""
    if answer_text != rag.REFUSAL_MESSAGE:
        _off_topic_streaks.pop(chat_id, None)
        return answer_text

    streak = _off_topic_streaks.get(chat_id, 0) + 1
    _off_topic_streaks[chat_id] = streak

    if streak == OFF_TOPIC_STREAK_LIMIT + 1:
        return OFF_TOPIC_LOCK_MESSAGE
    if streak > OFF_TOPIC_STREAK_LIMIT + 1:
        return None
    return answer_text


def _stream_chat_reply(chat_id, question, previous_answer, draft_id, message_thread_id=None):
    """Transmite a resposta do RAG aos poucos via sendRichMessageDraft
    conforme o Claude gera o texto (só funciona em chat privado — daí ser
    usada só nesse caso em handle_chat_message), e persiste a versão final
    com sendRichMessage ao terminar, como a doc exige."""
    last_sent_length = 0
    last_sent_time = 0.0
    final_text = ""

    for accumulated in rag.answer_question_stream(question, previous_answer=previous_answer):
        final_text = accumulated
        if accumulated == rag.REFUSAL_MESSAGE:
            # Recusa é sempre um único yield já completo (ver docstring de
            # answer_question_stream) — não faz sentido "transmitir aos
            # poucos" isso, e mandar um draft aqui vazaria a recusa mesmo
            # que a trava abaixo decida ficar em silêncio.
            break
        now = time.monotonic()
        has_enough_new_text = len(accumulated) - last_sent_length >= STREAM_UPDATE_MIN_CHARS
        enough_time_passed = now - last_sent_time >= STREAM_UPDATE_MIN_INTERVAL_SECONDS
        if not (has_enough_new_text and enough_time_passed):
            continue
        try:
            send_rich_message_draft(chat_id, draft_id, markdown_to_telegram_html(accumulated), message_thread_id=message_thread_id)
            last_sent_length = len(accumulated)
            last_sent_time = now
        except requests.RequestException as exc:
            # uma atualização de draft falhar não é motivo pra abortar o
            # streaming — a próxima tentativa (ou a mensagem final) resolve.
            print(f"Falha ao atualizar rich message draft: {_error_detail(exc)}", file=sys.stderr)

    text_to_send = _apply_off_topic_lock(chat_id, final_text)
    if text_to_send is None:
        return

    text_to_send = truncate_summary(text_to_send, max_length=rich_message.ARTICLE_HTML_MAX_LENGTH)
    send_rich_message(
        chat_id, markdown_to_telegram_html(text_to_send),
        reply_to_message_id=draft_id, message_thread_id=message_thread_id,
    )


# ---------------------------------------------------------------------------
# Pedido de "me manda o artigo" (vs. pergunta/comentário sobre o tema)
# ---------------------------------------------------------------------------

ARTICLE_NOT_FOUND_MESSAGE = (
    "😕 Não encontrei nenhum artigo que bata com isso no meu índice. "
    "Tenta descrever melhor o título ou o assunto?"
)

# Abaixo disso ("manda o artigo", "manda ele"), o que sobra depois de tirar
# a frase-gatilho é curto/vago demais pra buscar sozinho — melhor tentar o
# artigo citado na conversa do que arriscar um match ruim.
MIN_ARTICLE_QUERY_LENGTH = 12


def extract_cited_article(message):
    """Extrai o artigo citado numa mensagem do bot a partir das entities do
    Telegram — o campo `text` sozinho não carrega a URL do link, só o texto
    visível (markdown_to_telegram_html manda a citação do RAG como <a href>,
    e o Telegram devolve isso via entities[].type == 'text_link', não no
    texto). Usado quando o pedido de envio é vago ('manda esse artigo') mas
    é uma reply a uma resposta do bot que já tinha citado um artigo."""
    if not message:
        return None
    text = message.get("text") or ""
    for entity in message.get("entities") or []:
        if entity.get("type") == "text_link" and entity.get("url"):
            offset, length = entity["offset"], entity["length"]
            return {"titulo": text[offset:offset + length], "url": entity["url"]}
    return None


def _resolve_article_for_send(question, reply_to, bot_id):
    """Descobre a qual artigo um pedido de envio se refere: busca pelo texto
    restante depois de tirar a frase-gatilho ('manda o artigo X' -> busca
    por 'X', que pode ser um título ou um trecho/citação de dentro do
    artigo — rag.find_matching_article é semântico, não exige título exato).
    Se isso não for possível (texto curto/vago demais), tenta o artigo
    citado na mensagem sendo respondida, quando é uma reply ao bot."""
    search_text = rag.strip_send_trigger(question)
    if len(search_text) >= MIN_ARTICLE_QUERY_LENGTH:
        article = rag.find_matching_article(search_text)
        if article:
            return article

    if reply_to and reply_to.get("from", {}).get("id") == bot_id:
        cited = extract_cited_article(reply_to)
        if cited:
            return cited

    return None


def _find_cached_article_html(article_url):
    """Procura o HTML já pronto de um artigo em article_content.json, pelo
    link — não pelo short_id, porque o short_id de lá é derivado do
    entry_id do feed (id ou link da entry), que pode não ser bit-a-bit
    igual à URL vinda do índice do RAG (ex: barra final, query string)."""
    for cached in load_article_content().values():
        if cached.get("link") == article_url:
            return cached.get("html")
    return None


def _fetch_article_html_on_demand(article):
    """Busca o HTML completo de um artigo direto do Substack — usado quando
    ele ainda não está em article_content.json (só entram lá os artigos
    publicados automaticamente depois que essa funcionalidade passou a
    existir; os demais 121 precisam ser buscados na hora)."""
    title, body_html, _cover_image = fetch_post(substack_base_url(), article["url"])
    return rich_message.build_full_article_html(title=title, link=article["url"], raw_body_html=body_html)


def send_article_to_chat(chat_id, article, reply_to_message_id=None, message_thread_id=None):
    """Manda o artigo identificado por _resolve_article_for_send pro chat:
    reaproveita o HTML já pronto se o bot já publicou esse artigo (cache em
    article_content.json), senão busca na hora. Se o rich message falhar por
    qualquer motivo (busca ou envio), cai pra uma mensagem comum com título
    + link do Substack — nunca deixa o pedido sem resposta nenhuma."""
    try:
        article_html = _find_cached_article_html(article["url"]) or _fetch_article_html_on_demand(article)
        send_rich_message(
            chat_id, article_html,
            reply_to_message_id=reply_to_message_id, message_thread_id=message_thread_id,
        )
        return
    except Exception as exc:
        print(f"Falha ao mandar artigo completo via chat: {_error_detail(exc)}", file=sys.stderr)

    title_html = html.escape(article.get("titulo", ""), quote=False)
    fallback_text = (
        f'📄 <b>{title_html}</b>\n\n'
        f'<a href="{html.escape(article["url"], quote=True)}">Leia no Substack →</a>'
    )
    try:
        send_telegram_message(
            chat_id, fallback_text,
            reply_to_message_id=reply_to_message_id, message_thread_id=message_thread_id,
        )
    except requests.RequestException as exc:
        print(f"Falha ao mandar fallback do artigo: {_error_detail(exc)}", file=sys.stderr)


def _handle_article_send_request(chat_id, question, message, reply_to, bot_id):
    article = _resolve_article_for_send(question, reply_to, bot_id)
    if not article:
        try:
            send_telegram_message(
                chat_id, ARTICLE_NOT_FOUND_MESSAGE,
                reply_to_message_id=message.get("message_id"),
                message_thread_id=message.get("message_thread_id"),
            )
        except requests.RequestException:
            pass
        return

    send_article_to_chat(
        chat_id, article,
        reply_to_message_id=message.get("message_id"),
        message_thread_id=message.get("message_thread_id"),
    )


def handle_chat_message(message, bot_id, bot_username):
    """Responde uma mensagem via RAG (chat sobre os artigos), se elegível
    pelas regras de privado/menção/reply. Erros não derrubam o processamento
    das demais mensagens do lote."""
    if not rag.should_respond(message, bot_username, bot_id):
        return

    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    question = rag.strip_mention(text, bot_username)
    if not question:
        return

    reply_to = message.get("reply_to_message")
    previous_answer = None
    if reply_to and reply_to.get("from", {}).get("id") == bot_id:
        previous_answer = reply_to.get("text")

    try:
        wants_article = rag.classify_send_intent(question)
    except Exception as exc:
        # Se a classificação falhar (ex: API fora do ar), segue como
        # pergunta normal — é o comportamento de sempre, mais seguro que
        # travar o pedido inteiro por causa de uma etapa auxiliar.
        print(f"Falha ao classificar intenção da mensagem (seguindo como pergunta): {exc}", file=sys.stderr)
        wants_article = False

    if wants_article:
        _handle_article_send_request(chat_id, question, message, reply_to, bot_id)
        return

    # sendRichMessageDraft só funciona em chat privado (ver doc do método);
    # em grupo (menção/reply) mantemos o fluxo de sempre, sem streaming.
    is_private = message.get("chat", {}).get("type", "private") == "private"
    if is_private:
        try:
            _stream_chat_reply(
                chat_id, question, previous_answer,
                draft_id=message["message_id"], message_thread_id=message.get("message_thread_id"),
            )
        except Exception as exc:
            print(f"Falha ao responder pergunta via RAG (streaming): {exc}", file=sys.stderr)
            _send_chat_fallback_error(chat_id, message)
        return

    try:
        answer = rag.answer_question(question, previous_answer=previous_answer)
    except Exception as exc:
        print(f"Falha ao responder pergunta via RAG: {exc}", file=sys.stderr)
        _send_chat_fallback_error(chat_id, message)
        return

    answer = _apply_off_topic_lock(chat_id, answer)
    if answer is None:
        return

    answer = truncate_summary(answer, max_length=TELEGRAM_TEXT_MAX_LENGTH)
    try:
        send_telegram_message(
            chat_id,
            markdown_to_telegram_html(answer),
            reply_to_message_id=message.get("message_id"),
            message_thread_id=message.get("message_thread_id"),
        )
    except requests.RequestException as exc:
        print(f"Falha ao enviar resposta do RAG: {_error_detail(exc)}", file=sys.stderr)


READ_FULL_ARTICLE_CALLBACK_PREFIX = "art:"
READ_FULL_ARTICLE_BUTTON_TEXT = "📄 Ler artigo completo"


def build_read_full_article_markup(short_id):
    return {"inline_keyboard": [[{
        "text": READ_FULL_ARTICLE_BUTTON_TEXT,
        "callback_data": f"{READ_FULL_ARTICLE_CALLBACK_PREFIX}{short_id}",
    }]]}


def handle_callback_query(callback_query):
    """Trata o clique no botão 'Ler artigo completo'. sendRichMessage é uma
    API muito nova (Bot API 10.1, dias de existência) — se falhar por
    qualquer motivo, cai pro sendMessage tradicional em vez de deixar o
    usuário sem resposta."""
    data = callback_query.get("data") or ""
    chat_id = callback_query["message"]["chat"]["id"]

    if not data.startswith(READ_FULL_ARTICLE_CALLBACK_PREFIX):
        answer_callback_query(callback_query["id"])
        return

    short_id = data[len(READ_FULL_ARTICLE_CALLBACK_PREFIX):]
    article = load_article_content().get(short_id)

    if article:
        try:
            send_rich_message(chat_id, article["html"])
            answer_callback_query(callback_query["id"])
            return
        except requests.RequestException as exc:
            print(f"Falha ao enviar rich message do artigo: {_error_detail(exc)}", file=sys.stderr)
            try:
                send_telegram_message(
                    chat_id,
                    f'😕 Não consegui montar o artigo formatado agora. '
                    f'<a href="{html.escape(article["link"], quote=True)}">Leia direto no Substack →</a>',
                )
            except requests.RequestException:
                pass

    answer_callback_query(
        callback_query["id"],
        text="Não encontrei mais esse artigo por aqui — tenta o link no post original.",
        show_alert=True,
    )


def dispatch_message(message, bot_id, bot_username, feed_entries):
    """Processa uma única mensagem: comandos ou, quando elegível, chat/RAG.
    Usada tanto pelo polling (em lote) quanto pelo webhook (uma por vez)."""
    chat_id = message["chat"]["id"]
    text = (message.get("text") or "").strip()
    command = text.split()[0].split("@")[0] if text else ""

    if command == "/stop":
        subscribers = load_subscribers()
        subscribers.discard(chat_id)
        save_subscribers(subscribers)
        send_telegram_message(chat_id, build_stop_message())
    elif command == "/categorias":
        send_telegram_message(chat_id, build_categories_message(feed_entries))
    elif command == "/recentes":
        send_telegram_message(chat_id, build_recent_articles_message(feed_entries))
    elif command == "/substack":
        send_telegram_message(chat_id, build_substack_message())
    elif command == "/sugestao":
        send_telegram_message(chat_id, build_sugestao_message())
    elif command == "/start":
        subscribers = load_subscribers()
        subscribers.add(chat_id)
        save_subscribers(subscribers)
        send_telegram_message(chat_id, build_start_message())
    else:
        handle_chat_message(message, bot_id, bot_username)


def process_updates(feed_entries, poll_timeout=0):
    """Modo polling (dev local, sem webhook): busca mensagens novas desde a
    última execução e despacha cada uma."""
    offset = load_offset()
    updates = get_updates(offset, timeout=poll_timeout)
    if not updates:
        return

    bot_id, bot_username = get_me()
    max_update_id = offset - 1

    for update in updates:
        max_update_id = max(max_update_id, update.get("update_id", max_update_id))
        callback_query = update.get("callback_query")
        if callback_query:
            handle_callback_query(callback_query)
            continue
        message = update.get("message")
        if not message:
            continue
        dispatch_message(message, bot_id, bot_username, feed_entries)

    save_offset(max_update_id + 1)


# ---------------------------------------------------------------------------
# Publicação de artigos novos
# ---------------------------------------------------------------------------

def broadcast_to_subscribers(subscribers, caption, image_url, reply_markup=None):
    """Envia o artigo para cada assinante; remove quem bloqueou o bot (HTTP 403)."""
    blocked = set()
    for chat_id in subscribers:
        try:
            if image_url:
                send_telegram_photo(chat_id, image_url, caption, reply_markup=reply_markup)
            else:
                send_telegram_message(chat_id, caption, reply_markup=reply_markup)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 403:
                blocked.add(chat_id)
                print(f"Assinante {chat_id} bloqueou o bot; removendo da lista.")
            else:
                print(f"Falha ao enviar para assinante {chat_id}: {exc}", file=sys.stderr)
        time.sleep(0.05)
    return blocked


def fetch_and_publish():
    """Busca o feed RSS e publica os artigos ainda não postados. Retorna as
    entries do feed (reaproveitadas pelos comandos /categorias e /recentes até
    a próxima checagem) ou None se a busca falhar."""
    try:
        feed = fetch_feed(SUBSTACK_RSS_URL)
    except (urllib.error.URLError, OSError) as exc:
        print(f"Erro ao buscar o feed RSS: {exc}", file=sys.stderr)
        return None

    if feed.bozo and not feed.entries:
        print(f"Erro ao ler o feed RSS: {feed.bozo_exception}", file=sys.stderr)
        return None

    posted_ids = load_posted()
    subscribers = load_subscribers()
    new_entries = [e for e in feed.entries if entry_id(e) not in posted_ids]

    if not new_entries:
        return feed.entries

    # feeds RSS costumam vir do mais novo pro mais antigo; publicamos em ordem cronológica
    new_entries.reverse()

    article_content = load_article_content()

    for entry in new_entries:
        title = entry.get("title", "(sem título)")
        caption = build_caption(entry)
        image_url = extract_image_url(entry)

        raw_body = ""
        if entry.get("content"):
            raw_body = entry["content"][0].get("value", "")
        raw_body = raw_body or entry.get("summary", "")
        article_html = rich_message.build_full_article_html(
            title=strip_html(title), link=entry.get("link", ""), raw_body_html=raw_body,
        )
        short_id = article_short_id(entry_id(entry))
        article_content[short_id] = {"html": article_html, "link": entry.get("link", "")}
        reply_markup = build_read_full_article_markup(short_id)

        try:
            if image_url:
                send_telegram_photo(TELEGRAM_CHANNEL_ID, image_url, caption, reply_markup=reply_markup)
            else:
                send_telegram_message(TELEGRAM_CHANNEL_ID, caption, reply_markup=reply_markup)
        except requests.RequestException as exc:
            print(f"Falha ao publicar '{title}' no canal: {_error_detail(exc)}", file=sys.stderr)
            break

        blocked = broadcast_to_subscribers(subscribers, caption, image_url, reply_markup=reply_markup)
        if blocked:
            subscribers -= blocked
            save_subscribers(subscribers)

        save_article_content(article_content)

        posted_ids.add(entry_id(entry))
        save_posted(posted_ids)
        print(f"Publicado: {title}")

    return feed.entries


def run_polling_loop():
    """Modo dev local (sem PUBLIC_URL/RENDER_EXTERNAL_URL): fica em long
    polling esperando mensagens do Telegram e, de tempos em tempos
    (FEED_CHECK_INTERVAL_SECONDS), reconsulta o feed RSS pra publicar artigos
    novos. Erros de uma iteração não derrubam o processo — ficam logados e o
    loop continua na próxima."""
    cached_entries = []
    last_feed_check = 0.0

    while True:
        try:
            now = time.monotonic()
            if not cached_entries or now - last_feed_check >= FEED_CHECK_INTERVAL_SECONDS:
                entries = fetch_and_publish()
                if entries is not None:
                    cached_entries = entries
                last_feed_check = now

            process_updates(cached_entries, poll_timeout=TELEGRAM_POLL_TIMEOUT_SECONDS)
        except Exception as exc:
            print(f"Erro inesperado no loop principal: {_error_detail(exc)}", file=sys.stderr)
            time.sleep(5)


# ---------------------------------------------------------------------------
# Modo webhook (Render Web Service, plano free)
# ---------------------------------------------------------------------------
# Web Service free dorme sem tráfego HTTP e não sustenta long polling 24/7, então
# aqui o Telegram empurra cada mensagem via POST no lugar de ficarmos perguntando
# por elas. Um GET (batido por um keep-alive externo, ex: cron-job.org) mantém o
# serviço acordado e, de quebra, aproveita pra checar o feed RSS e sincronizar o
# estado com o GitHub — não há um segundo loop rodando por conta própria.

_webhook_state_lock = threading.Lock()
_webhook_cached_entries = []
_webhook_last_feed_check = 0.0
_webhook_last_state_sync = 0.0
_webhook_bot_id = None
_webhook_bot_username = None


def _refresh_feed_and_sync_state(force=False):
    global _webhook_cached_entries, _webhook_last_feed_check, _webhook_last_state_sync
    with _webhook_state_lock:
        now = time.monotonic()
        if force or not _webhook_cached_entries or now - _webhook_last_feed_check >= FEED_CHECK_INTERVAL_SECONDS:
            entries = fetch_and_publish()
            if entries is not None:
                _webhook_cached_entries = entries
            _webhook_last_feed_check = now

        if GITHUB_TOKEN and now - _webhook_last_state_sync >= STATE_SYNC_INTERVAL_SECONDS:
            sync_state_to_git()
            _webhook_last_state_sync = now


MINIAPP_PATH = "/miniapp"
MINIAPP_ARTICLES_PATH = "/miniapp/articles.json"
MINIAPP_CHAT_PATH = "/miniapp/chat"


class _WebhookRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == MINIAPP_PATH:
            self._respond(200, miniapp.PAGE_HTML.encode("utf-8"), content_type="text/html; charset=utf-8")
            return
        if self.path == MINIAPP_ARTICLES_PATH:
            body = json.dumps(load_articles_catalog(), ensure_ascii=False).encode("utf-8")
            self._respond(200, body, content_type="application/json")
            return

        try:
            _refresh_feed_and_sync_state()
        except Exception as exc:
            print(f"Erro ao checar feed/sincronizar estado: {exc}", file=sys.stderr)
        self._respond(200, b"ok")

    def do_POST(self):
        if self.path == MINIAPP_CHAT_PATH:
            self._handle_miniapp_chat()
            return

        if self.path != WEBHOOK_PATH:
            self._respond(404, b"not found")
            return
        if TELEGRAM_WEBHOOK_SECRET and self.headers.get("X-Telegram-Bot-Api-Secret-Token") != TELEGRAM_WEBHOOK_SECRET:
            self._respond(403, b"forbidden")
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            update = json.loads(body or b"{}")
            callback_query = update.get("callback_query")
            message = update.get("message")
            if callback_query:
                handle_callback_query(callback_query)
            elif message:
                with _webhook_state_lock:
                    dispatch_message(message, _webhook_bot_id, _webhook_bot_username, _webhook_cached_entries)
        except Exception as exc:
            print(f"Erro processando update do webhook: {_error_detail(exc)}", file=sys.stderr)

        self._respond(200, b"ok")

    def _handle_miniapp_chat(self):
        """Endpoint de chat do Mini App: valida o initData (garante que a
        pergunta veio de dentro do Telegram, não de qualquer um batendo
        nesse endpoint) e devolve a resposta do RAG em JSON — sem streaming
        (o streaming via sendRichMessageDraft é só pro chat dentro do
        Telegram; aqui é uma requisição HTTP comum, pergunta -> resposta)."""
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self._respond_json(400, {"error": "corpo da requisição inválido"})
            return

        user = miniapp.validate_init_data(payload.get("initData", ""), TELEGRAM_BOT_TOKEN)
        if user is None:
            self._respond_json(401, {"error": "initData inválido ou expirado"})
            return

        question = (payload.get("question") or "").strip()
        if not question:
            self._respond_json(400, {"error": "pergunta vazia"})
            return

        try:
            answer = rag.answer_question(question, previous_answer=payload.get("previous_answer"))
        except Exception as exc:
            print(f"Falha ao responder pergunta do Mini App: {exc}", file=sys.stderr)
            self._respond_json(502, {"error": "não consegui responder agora"})
            return

        self._respond_json(200, {"answer": answer})

    def _respond_json(self, status, data):
        self._respond(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), content_type="application/json")

    def _respond(self, status, body, content_type="text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # silencia o log padrão de cada request no stdout


def run_webhook_server():
    """Modo produção no Render (Web Service free): registra o webhook no
    Telegram e sobe um servidor HTTP mínimo pra receber as mensagens (POST)
    e o ping de keep-alive (GET)."""
    global _webhook_bot_id, _webhook_bot_username
    _webhook_bot_id, _webhook_bot_username = get_me()

    webhook_url = f"{PUBLIC_URL.rstrip('/')}{WEBHOOK_PATH}"
    try:
        set_webhook(webhook_url)
        print(f"Webhook registrado: {webhook_url}")
    except requests.RequestException as exc:
        print(f"Falha ao registrar o webhook: {exc}", file=sys.stderr)

    try:
        miniapp_url = f"{PUBLIC_URL.rstrip('/')}{MINIAPP_PATH}"
        set_chat_menu_button(miniapp_url)
        print(f"Botão de menu do Mini App registrado: {miniapp_url}")
    except requests.RequestException as exc:
        print(f"Falha ao registrar o botão de menu do Mini App: {exc}", file=sys.stderr)

    _refresh_feed_and_sync_state(force=True)

    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _WebhookRequestHandler)
    print(f"Servindo webhook na porta {port}...")
    server.serve_forever()


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, SUBSTACK_RSS_URL]):
        print(
            "Erro: defina TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID e SUBSTACK_RSS_URL "
            "(no .env ou nas variáveis de ambiente).",
            file=sys.stderr,
        )
        sys.exit(1)

    if PUBLIC_URL:
        run_webhook_server()
    else:
        run_polling_loop()


if __name__ == "__main__":
    main()
