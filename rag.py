"""Chat com base nos artigos do Substack (RAG): busca por similaridade sobre
articles_index.json + geração de resposta com a API do Claude, com guardrail
de escopo (só responde com base no conteúdo indexado)."""

import json
import os
import re
from pathlib import Path

import anthropic
import numpy as np
import voyageai

VOYAGE_API_KEY = os.getenv("VOYAGE_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

BASE_DIR = Path(__file__).parent
ARTICLES_INDEX_FILE = BASE_DIR / "articles_index.json"

EMBEDDING_MODEL = "voyage-3"
CLAUDE_MODEL = "claude-sonnet-5"
TOP_K = 5

# Abaixo desse score de similaridade de cosseno, recusa sem chamar o Claude.
LIMIAR_RELEVANCIA = 0.35

REFUSAL_MESSAGE = (
    "Não tenho permissão para conversar sobre assuntos fora dos temas cobertos "
    "pelos meus artigos no Substack. Manda uma pergunta relacionada a algum "
    "deles que eu respondo! 📚"
)

SYSTEM_PROMPT = """Você é o assistente do Substack "San's Anthology", respondendo \
num chat do Telegram. Responda perguntas EXCLUSIVAMENTE com base nos trechos de \
artigos fornecidos abaixo como contexto.

Regras rígidas:
- Nunca use conhecimento externo ao contexto fornecido, mesmo que você "saiba" \
a resposta.
- Se o contexto não for suficiente para responder, ou se a pergunta (ou parte \
dela) fugir dos temas cobertos pelo contexto — mesmo no meio de uma conversa \
que começou dentro do tema — recuse educadamente, dizendo que não tem \
permissão para falar sobre assuntos fora desses tópicos. Não tente adivinhar \
ou complementar com informação de fora do contexto.
- Cite, quando fizer sentido, de qual artigo a informação vem, mas de forma \
natural dentro da frase — não como nota de rodapé. Sempre que citar um \
artigo, use a URL dele do próprio contexto e escreva a citação como um link \
Markdown: [Título do Artigo](url). Nunca invente uma URL — use exatamente a \
que aparece no contexto para aquele trecho.
- Responda em português.

Estilo de escrita — isto é uma conversa de chat, não um relatório:
- Escreva em prosa corrida, como se estivesse batendo papo com alguém que fez \
uma pergunta interessante. Não organize a resposta em lista numerada, tópicos \
ou títulos, a menos que a pergunta peça uma lista explicitamente.
- Use **negrito** com moderação — no máximo 1 ou 2 termos centrais em toda a \
resposta, nunca para abrir cada parágrafo.
- Use *itálico* para termos estrangeiros, técnicos ou citados.
- Varie o ritmo e o tamanho das frases; evite começar toda resposta com a \
mesma estrutura ("Com base nos artigos...", "De acordo com...").
- Seja direto, mas com voz de gente, não de verbete de enciclopédia.

Contexto (trechos dos artigos):
{context}"""


def _voyage_client():
    return voyageai.Client(api_key=VOYAGE_API_KEY)


def _anthropic_client():
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def load_articles_index():
    if not ARTICLES_INDEX_FILE.exists():
        return []
    with open(ARTICLES_INDEX_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def embed_query(text, client=None):
    client = client or _voyage_client()
    result = client.embed([text], model=EMBEDDING_MODEL, input_type="query")
    return result.embeddings[0]


def cosine_similarity(a, b):
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def search(query_embedding, index, top_k=TOP_K):
    """Retorna até top_k chunks do índice, ordenados por similaridade decrescente."""
    scored = [
        (cosine_similarity(query_embedding, chunk["embedding"]), chunk)
        for chunk in index
    ]
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return scored[:top_k]


def build_context(scored_chunks):
    blocks = []
    for score, chunk in scored_chunks:
        blocks.append(f"[{chunk.get('titulo', '')}]({chunk.get('url', '')})\n{chunk.get('texto', '')}")
    return "\n\n---\n\n".join(blocks)


def _build_messages(question, previous_answer):
    messages = []
    if previous_answer:
        messages.append({"role": "assistant", "content": previous_answer})
    messages.append({"role": "user", "content": question})
    return messages


def ask_claude(question, scored_chunks, previous_answer=None, client=None):
    client = client or _anthropic_client()
    system = SYSTEM_PROMPT.format(context=build_context(scored_chunks))
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=system,
        messages=_build_messages(question, previous_answer),
    )
    return "".join(block.text for block in response.content if block.type == "text").strip()


def stream_answer(question, scored_chunks, previous_answer=None, client=None):
    """Como ask_claude, mas via streaming da API do Claude: cada yield é o
    texto acumulado da resposta até aquele ponto (não só o pedaço novo),
    pensado pra alimentar atualizações de sendRichMessageDraft em bot.py."""
    client = client or _anthropic_client()
    system = SYSTEM_PROMPT.format(context=build_context(scored_chunks))

    accumulated = ""
    with client.messages.stream(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        system=system,
        messages=_build_messages(question, previous_answer),
    ) as stream:
        for delta in stream.text_stream:
            accumulated += delta
            yield accumulated


def _relevant_chunks_or_none(question, index, voyage_client=None):
    """Busca os chunks mais relevantes pra pergunta contra o índice; retorna
    None se a melhor similaridade ficar abaixo de LIMIAR_RELEVANCIA (recusa
    sem gastar uma chamada à API do Claude)."""
    query_embedding = embed_query(question, client=voyage_client)
    scored_chunks = search(query_embedding, index)
    best_score = scored_chunks[0][0] if scored_chunks else 0.0
    if best_score < LIMIAR_RELEVANCIA:
        return None
    return scored_chunks


def answer_question(question, index=None, previous_answer=None, voyage_client=None, anthropic_client=None):
    """Orquestra: embeda a pergunta, busca contexto, aplica o guardrail de
    relevância e, se aprovado, chama o Claude. Retorna o texto da resposta."""
    index = load_articles_index() if index is None else index
    if not index:
        return REFUSAL_MESSAGE

    scored_chunks = _relevant_chunks_or_none(question, index, voyage_client)
    if scored_chunks is None:
        return REFUSAL_MESSAGE

    return ask_claude(question, scored_chunks, previous_answer=previous_answer, client=anthropic_client)


def answer_question_stream(question, index=None, previous_answer=None, voyage_client=None, anthropic_client=None):
    """Como answer_question, mas via generator: pra perguntas elegíveis (o
    guardrail de relevância passou), cada yield é o texto acumulado da
    resposta até aquele ponto, conforme o Claude vai gerando. Uma recusa
    (sem índice, ou pergunta fora do escopo) produz um único yield com
    REFUSAL_MESSAGE, sem streaming de verdade — não há o que transmitir aos
    poucos numa resposta fixa."""
    index = load_articles_index() if index is None else index
    if not index:
        yield REFUSAL_MESSAGE
        return

    scored_chunks = _relevant_chunks_or_none(question, index, voyage_client)
    if scored_chunks is None:
        yield REFUSAL_MESSAGE
        return

    yield from stream_answer(question, scored_chunks, previous_answer=previous_answer, client=anthropic_client)


# ---------------------------------------------------------------------------
# Regras de quando responder (privado sempre; grupo só se mencionado ou reply)
# ---------------------------------------------------------------------------

def is_command(text):
    return text.strip().startswith("/")


def is_mentioned(text, bot_username):
    if not bot_username or not text:
        return False
    return f"@{bot_username}".lower() in text.lower()


def is_reply_to_bot(message, bot_id):
    reply = message.get("reply_to_message")
    if not reply or bot_id is None:
        return False
    return reply.get("from", {}).get("id") == bot_id


def should_respond(message, bot_username, bot_id):
    """Privado: sempre (exceto comandos, já tratados antes de chamar isto).
    Grupo/supergrupo: só se mencionado ou reply direto a uma mensagem do bot."""
    text = message.get("text") or ""
    if is_command(text):
        return False

    chat_type = message.get("chat", {}).get("type", "private")
    if chat_type == "private":
        return bool(text.strip())

    return is_mentioned(text, bot_username) or is_reply_to_bot(message, bot_id)


def strip_mention(text, bot_username):
    """Remove a menção ao bot do início/fim do texto antes de mandar pro RAG."""
    if not bot_username:
        return text.strip()
    mention = f"@{bot_username}"
    return text.replace(mention, "").strip()


# ---------------------------------------------------------------------------
# Chunking (usado pelo index_articles.py)
# ---------------------------------------------------------------------------

CHUNK_MIN_SIZE = 500
CHUNK_MAX_SIZE = 1000


def chunk_text(text, min_size=CHUNK_MIN_SIZE, max_size=CHUNK_MAX_SIZE):
    """Divide o texto em blocos de ~min_size–max_size caracteres, respeitando
    parágrafos (nunca corta uma frase no meio, exceto se um único parágrafo
    já for maior que max_size, quando cai pra divisão por frases)."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks = []
    current = ""

    for paragraph in paragraphs:
        pieces = [paragraph] if len(paragraph) <= max_size else _split_long_paragraph(paragraph, max_size)
        for piece in pieces:
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= max_size:
                current = candidate
                continue
            if current:
                chunks.append(current)
            current = piece

    if current:
        chunks.append(current)

    return chunks


def _split_long_paragraph(paragraph, max_size):
    """Divide um parágrafo longo demais em frases, agrupando até max_size."""
    sentences = re.split(r"(?<=[.!?])\s+", paragraph)
    pieces = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) <= max_size:
            current = candidate
        else:
            if current:
                pieces.append(current)
            current = sentence
    if current:
        pieces.append(current)
    return pieces
