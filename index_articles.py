"""Indexa o texto completo de todos os artigos do Substack pra uso no chat
RAG do bot. Roda sob demanda (não faz parte do workflow automático):

    python index_articles.py

É incremental: artigos cujo `url` já está em articles_index.json são
pulados (não recomputa embeddings do que já foi indexado)."""

import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin

from dotenv import load_dotenv
from voyageai.error import RateLimitError

import rag
from bot import (
    _fetch_raw,
    decompress_response,
    fetch_post,
    slug_from_url,
    strip_html,
    substack_base_url,
)

load_dotenv()

BASE_DIR = Path(__file__).parent
ARTICLES_INDEX_FILE = BASE_DIR / "articles_index.json"

SITEMAP_CANDIDATES = ["/sitemap.xml", "/sitemap/sitemap.xml"]
POST_URL_RE = re.compile(r"/p/[^/?#]+/?$")

# Namespace padrão do protocolo sitemaps.org
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

EMBED_BATCH_SIZE = 32

# Sem cartão cadastrado, a Voyage AI limita a conta a 3 requisições/minuto.
# Espaçamos as chamadas nesse ritmo (com margem) pra não estourar o limite,
# em vez de deixar a exceção de rate limit derrubar cada artigo na primeira
# tentativa.
EMBED_MIN_INTERVAL_SECONDS = 21
_last_embed_call_at = [0.0]


def _rate_limited_embed(embed_client, batch):
    wait = EMBED_MIN_INTERVAL_SECONDS - (time.monotonic() - _last_embed_call_at[0])
    if wait > 0:
        time.sleep(wait)
    try:
        result = embed_client.embed(batch, model=rag.EMBEDDING_MODEL, input_type="document")
    except RateLimitError:
        time.sleep(EMBED_MIN_INTERVAL_SECONDS)
        result = embed_client.embed(batch, model=rag.EMBEDDING_MODEL, input_type="document")
    finally:
        _last_embed_call_at[0] = time.monotonic()
    return result


def fetch_xml(url):
    content, content_encoding = _fetch_raw(url)
    content = decompress_response(content, content_encoding)
    return ET.fromstring(content)


def discover_post_urls(base_url):
    """Busca o sitemap (seguindo sitemap-index se houver) e retorna as URLs
    de posts (padrão /p/{slug}) encontradas."""
    for candidate in SITEMAP_CANDIDATES:
        try:
            root = fetch_xml(urljoin(base_url + "/", candidate.lstrip("/")))
            break
        except Exception:
            continue
    else:
        raise RuntimeError(
            f"Não consegui encontrar um sitemap em {base_url} "
            f"(tentei {SITEMAP_CANDIDATES})."
        )

    locs = [el.text for el in root.findall(".//sm:loc", SITEMAP_NS) if el.text]

    # Se for um sitemap-index (aponta pra outros sitemaps), segue cada um.
    if root.tag.endswith("sitemapindex"):
        post_urls = []
        for sub_sitemap_url in locs:
            try:
                sub_root = fetch_xml(sub_sitemap_url)
            except Exception as exc:
                print(f"Aviso: falha ao buscar {sub_sitemap_url}: {exc}", file=sys.stderr)
                continue
            post_urls.extend(el.text for el in sub_root.findall(".//sm:loc", SITEMAP_NS) if el.text)
        locs = post_urls

    return sorted({url for url in locs if POST_URL_RE.search(url)})


def already_indexed_urls(index):
    return {chunk["url"] for chunk in index}


def index_article(base_url, post_url, embed_client):
    title, body_html = fetch_post(base_url, post_url)
    full_text = strip_html(body_html)
    if not full_text:
        print(f"Aviso: artigo sem texto extraído, pulando: {post_url}", file=sys.stderr)
        return []

    chunks = rag.chunk_text(full_text)
    if not chunks:
        return []

    embeddings = []
    for start in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[start:start + EMBED_BATCH_SIZE]
        result = _rate_limited_embed(embed_client, batch)
        embeddings.extend(result.embeddings)

    slug = slug_from_url(post_url)
    return [
        {
            "id": f"{slug}#{i}",
            "titulo": title,
            "url": post_url,
            "texto": chunk,
            "embedding": embedding,
        }
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings))
    ]


def main():
    base_url = substack_base_url()
    index = rag.load_articles_index()
    indexed_urls = already_indexed_urls(index)

    print(f"Buscando lista de artigos em {base_url}...")
    post_urls = discover_post_urls(base_url)
    print(f"{len(post_urls)} artigos encontrados no sitemap.")

    new_urls = [url for url in post_urls if url not in indexed_urls]
    print(f"{len(new_urls)} artigos novos pra indexar ({len(indexed_urls)} já indexados).")

    if not new_urls:
        print("Nada novo pra fazer.")
        return

    embed_client = rag._voyage_client()

    for i, post_url in enumerate(new_urls, start=1):
        print(f"[{i}/{len(new_urls)}] Indexando {post_url}...")
        try:
            new_chunks = index_article(base_url, post_url, embed_client)
        except Exception as exc:
            print(f"Erro ao indexar {post_url}: {exc}", file=sys.stderr)
            continue
        index.extend(new_chunks)
        with open(ARTICLES_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False)

    print(f"Concluído. Índice agora tem {len(index)} chunks em {ARTICLES_INDEX_FILE}.")


if __name__ == "__main__":
    main()
