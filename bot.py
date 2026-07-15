"""Publica os artigos mais recentes de um feed RSS do Substack em um canal do Telegram."""

import html
import json
import os
import re
import sys
from pathlib import Path

import feedparser
import requests
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
SUBSTACK_RSS_URL = os.getenv("SUBSTACK_RSS_URL")

POSTED_FILE = Path(__file__).parent / "posted.json"
SUMMARY_MAX_LENGTH = 500
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}/{method}"

TAG_RE = re.compile(r"<[^>]+>")
IMG_SRC_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)


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


def load_posted():
    if not POSTED_FILE.exists():
        return set()
    with open(POSTED_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))


def save_posted(posted_ids):
    with open(POSTED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(posted_ids), f, ensure_ascii=False, indent=2)


def entry_id(entry):
    return entry.get("id") or entry.get("link")


def send_telegram_message(text):
    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN, method="sendMessage")
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    response = requests.post(url, data=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def send_telegram_photo(photo_url, caption):
    url = TELEGRAM_API_BASE.format(token=TELEGRAM_BOT_TOKEN, method="sendPhoto")
    payload = {
        "chat_id": TELEGRAM_CHANNEL_ID,
        "photo": photo_url,
        "caption": caption,
        "parse_mode": "HTML",
    }
    response = requests.post(url, data=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def publish_entry(entry):
    caption = build_caption(entry)
    image_url = extract_image_url(entry)
    if image_url:
        send_telegram_photo(image_url, caption)
    else:
        send_telegram_message(caption)


def main():
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, SUBSTACK_RSS_URL]):
        print(
            "Erro: defina TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID e SUBSTACK_RSS_URL "
            "(no .env ou nas variáveis de ambiente).",
            file=sys.stderr,
        )
        sys.exit(1)

    feed = feedparser.parse(SUBSTACK_RSS_URL)
    if feed.bozo and not feed.entries:
        print(f"Erro ao ler o feed RSS: {feed.bozo_exception}", file=sys.stderr)
        sys.exit(1)

    posted_ids = load_posted()
    new_entries = [e for e in feed.entries if entry_id(e) not in posted_ids]

    if not new_entries:
        print("Nenhum artigo novo para publicar.")
        return

    # feeds RSS costumam vir do mais novo pro mais antigo; publicamos em ordem cronológica
    new_entries.reverse()

    for entry in new_entries:
        title = entry.get("title", "(sem título)")
        try:
            publish_entry(entry)
            posted_ids.add(entry_id(entry))
            save_posted(posted_ids)
            print(f"Publicado: {title}")
        except requests.RequestException as exc:
            print(f"Falha ao publicar '{title}': {exc}", file=sys.stderr)
            break


if __name__ == "__main__":
    main()
