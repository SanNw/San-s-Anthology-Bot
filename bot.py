"""Publica os artigos mais recentes de um feed RSS do Substack em um canal do Telegram
e nos assinantes que interagiram com o bot no privado, além de responder a comandos."""

import gzip
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

import rag

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
STATE_FILENAMES = ("posted.json", "subscribers.json", "update_offset.json")

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


def send_telegram_message(chat_id, text, reply_to_message_id=None, message_thread_id=None):
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


def send_telegram_photo(chat_id, photo_url, caption):
    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN, method="sendPhoto")
    payload = {
        "chat_id": chat_id,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML",
    }
    response = requests.post(url, data=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def set_webhook(url):
    """Registra o endpoint que o Telegram vai chamar (POST) a cada mensagem
    nova. Chamado uma vez ao subir o servidor em modo webhook."""
    api_url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN, method="setWebhook")
    payload = {"url": url, "allowed_updates": json.dumps(["message"])}
    if TELEGRAM_WEBHOOK_SECRET:
        payload["secret_token"] = TELEGRAM_WEBHOOK_SECRET
    response = requests.post(api_url, data=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def get_updates(offset, timeout=0):
    """Busca updates novos. Com timeout > 0, faz long polling: a chamada HTTP
    fica pendurada até chegar mensagem ou o timeout do Telegram expirar."""
    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN, method="getUpdates")
    params = {"timeout": timeout, "allowed_updates": json.dumps(["message"])}
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
# Processamento de comandos recebidos (getUpdates)
# ---------------------------------------------------------------------------

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
        answer = rag.answer_question(question, previous_answer=previous_answer)
    except Exception as exc:
        print(f"Falha ao responder pergunta via RAG: {exc}", file=sys.stderr)
        try:
            send_telegram_message(
                chat_id,
                "😕 Não consegui responder agora, tenta de novo em alguns minutos.",
                reply_to_message_id=message.get("message_id"),
                message_thread_id=message.get("message_thread_id"),
            )
        except requests.RequestException:
            pass
        return

    answer = truncate_summary(answer, max_length=TELEGRAM_TEXT_MAX_LENGTH)
    try:
        send_telegram_message(
            chat_id,
            html.escape(answer),
            reply_to_message_id=message.get("message_id"),
            message_thread_id=message.get("message_thread_id"),
        )
    except requests.RequestException as exc:
        print(f"Falha ao enviar resposta do RAG: {_error_detail(exc)}", file=sys.stderr)


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
        message = update.get("message")
        if not message:
            continue
        dispatch_message(message, bot_id, bot_username, feed_entries)

    save_offset(max_update_id + 1)


# ---------------------------------------------------------------------------
# Publicação de artigos novos
# ---------------------------------------------------------------------------

def broadcast_to_subscribers(subscribers, caption, image_url):
    """Envia o artigo para cada assinante; remove quem bloqueou o bot (HTTP 403)."""
    blocked = set()
    for chat_id in subscribers:
        try:
            if image_url:
                send_telegram_photo(chat_id, image_url, caption)
            else:
                send_telegram_message(chat_id, caption)
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

    for entry in new_entries:
        title = entry.get("title", "(sem título)")
        caption = build_caption(entry)
        image_url = extract_image_url(entry)
        try:
            if image_url:
                send_telegram_photo(TELEGRAM_CHANNEL_ID, image_url, caption)
            else:
                send_telegram_message(TELEGRAM_CHANNEL_ID, caption)
        except requests.RequestException as exc:
            print(f"Falha ao publicar '{title}' no canal: {_error_detail(exc)}", file=sys.stderr)
            break

        blocked = broadcast_to_subscribers(subscribers, caption, image_url)
        if blocked:
            subscribers -= blocked
            save_subscribers(subscribers)

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


class _WebhookRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            _refresh_feed_and_sync_state()
        except Exception as exc:
            print(f"Erro ao checar feed/sincronizar estado: {exc}", file=sys.stderr)
        self._respond(200, b"ok")

    def do_POST(self):
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
            message = update.get("message")
            if message:
                with _webhook_state_lock:
                    dispatch_message(message, _webhook_bot_id, _webhook_bot_username, _webhook_cached_entries)
        except Exception as exc:
            print(f"Erro processando update do webhook: {_error_detail(exc)}", file=sys.stderr)

        self._respond(200, b"ok")

    def _respond(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
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
