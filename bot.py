"""Publica os artigos mais recentes de um feed RSS do Substack em um canal do Telegram
e nos assinantes que interagiram com o bot no privado, além de responder a comandos."""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
SUBSTACK_RSS_URL = os.getenv("SUBSTACK_RSS_URL")

BASE_DIR = Path(__file__).parent
POSTED_FILE = BASE_DIR / "posted.json"
SUBSCRIBERS_FILE = BASE_DIR / "subscribers.json"
OFFSET_FILE = BASE_DIR / "update_offset.json"

SUMMARY_MAX_LENGTH = 500
RECENT_ARTICLES_COUNT = 5
SUBSTACK_SUBSCRIBE_URL = "https://san55.substack.com/subscribe"

TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"

TAG_RE = re.compile(r"<[^>]+>")
IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)

# Caracteres de controle proibidos pela especificação XML 1.0; feeds do Substack
# às vezes carregam algum (colado de Word/Google Docs) e quebram o parser estrito.
INVALID_XML_CHARS_RE = re.compile(rb"[\x00-\x08\x0B\x0C\x0E-\x1F]")

# Mesmo User-Agent que o feedparser usa por padrão ao buscar URLs diretamente —
# um User-Agent customizado levou o Substack a responder 403 Forbidden.
USER_AGENT = getattr(feedparser, "USER_AGENT", "feedparser/6.0.11 +https://github.com/kurtmckee/feedparser/")


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


def fetch_feed(url):
    """Busca o RSS manualmente (via urllib, como o próprio feedparser faz) e
    sanitiza o XML antes de repassar pro feedparser."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read()
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


# ---------------------------------------------------------------------------
# Chamadas à Bot API do Telegram
# ---------------------------------------------------------------------------

def send_telegram_message(chat_id, text):
    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN, method="sendMessage")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    response = requests.post(url, data=payload, timeout=30)
    response.raise_for_status()
    return response.json()


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


def get_updates(offset):
    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN, method="getUpdates")
    params = {"timeout": 0, "allowed_updates": json.dumps(["message"])}
    if offset:
        params["offset"] = offset
    response = requests.get(url, params=params, timeout=30)
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

def process_updates(feed_entries):
    """Busca mensagens novas desde a última execução e responde aos comandos."""
    offset = load_offset()
    updates = get_updates(offset)
    if not updates:
        return

    subscribers = load_subscribers()
    max_update_id = offset - 1

    for update in updates:
        max_update_id = max(max_update_id, update.get("update_id", max_update_id))
        message = update.get("message")
        if not message:
            continue

        chat_id = message["chat"]["id"]
        text = (message.get("text") or "").strip()
        command = text.split()[0].split("@")[0] if text else ""

        if command == "/stop":
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
            subscribers.add(chat_id)
            save_subscribers(subscribers)
            send_telegram_message(chat_id, build_start_message())

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


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, SUBSTACK_RSS_URL]):
        print(
            "Erro: defina TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID e SUBSTACK_RSS_URL "
            "(no .env ou nas variáveis de ambiente).",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        feed = fetch_feed(SUBSTACK_RSS_URL)
    except (urllib.error.URLError, OSError) as exc:
        print(f"Erro ao buscar o feed RSS: {exc}", file=sys.stderr)
        sys.exit(1)

    if feed.bozo and not feed.entries:
        print(f"Erro ao ler o feed RSS: {feed.bozo_exception}", file=sys.stderr)
        sys.exit(1)

    process_updates(feed.entries)

    posted_ids = load_posted()
    subscribers = load_subscribers()
    new_entries = [e for e in feed.entries if entry_id(e) not in posted_ids]

    if not new_entries:
        print("Nenhum artigo novo para publicar.")
        return

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
            print(f"Falha ao publicar '{title}' no canal: {exc}", file=sys.stderr)
            break

        blocked = broadcast_to_subscribers(subscribers, caption, image_url)
        if blocked:
            subscribers -= blocked
            save_subscribers(subscribers)

        posted_ids.add(entry_id(entry))
        save_posted(posted_ids)
        print(f"Publicado: {title}")


if __name__ == "__main__":
    main()
