"""Mini App do bot (https://core.telegram.org/bots/webapps): valida os dados
que o Telegram manda pro Mini App (initData) e guarda o HTML/CSS/JS da
página — tudo num arquivo só, autocontido, servido direto por bot.py sem
precisar de um servidor de arquivos estáticos à parte.

Este módulo não depende de bot.py nem faz chamada de rede — só validação
(stdlib pura) e uma string de HTML, pelo mesmo motivo de rich_message.py:
fácil de testar, sem import circular."""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

# Tempo máximo que um initData é aceito depois de gerado (auth_date) — evita
# que um initData antigo (ex: vazado em log) continue valendo pra sempre.
INIT_DATA_MAX_AGE_SECONDS = 24 * 60 * 60


def validate_init_data(init_data, bot_token, max_age_seconds=INIT_DATA_MAX_AGE_SECONDS):
    """Valida a string Telegram.WebApp.initData (ver "Validating data
    received via the Mini App" na doc) via HMAC-SHA-256 contra o token do
    bot. Devolve o dict do usuário do Telegram se for genuína e recente, ou
    None se estiver ausente, adulterada ou expirada."""
    if not init_data or not bot_token:
        return None

    data = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = data.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    try:
        auth_date = int(data.get("auth_date", ""))
    except ValueError:
        return None
    if max_age_seconds is not None and time.time() - auth_date > max_age_seconds:
        return None

    try:
        return json.loads(data.get("user", "{}"))
    except json.JSONDecodeError:
        return None


# HTML/CSS/JS num arquivo só (sem build step, sem dependência externa além
# do SDK do Telegram e do Google Fonts) — duas abas: vitrine de artigos com
# capa + busca no cliente, e chat com o mesmo RAG do bot.
#
# Paleta extraída de verdade do próprio san55.substack.com (pergaminho
# quente #fffbeb, dourado #9b7e00, cinzas mornos) em vez de inventada — ver
# discussão no README. Tipografia é Noto Serif: cobre Latin Extended
# Additional (o bloco usado por transliterações tipo IAST de sânscrito —
# ā, ī, ū, ṛ, ś, ṣ, ṭ, ḍ, ṇ, ṅ — que aparecem o tempo todo nos artigos) sem
# "tofu" (caixinhas de glifo faltando), com um corte Display pro título.
# O tema claro/escuroségue o Telegram.WebApp.colorScheme (evento
# themeChanged), mas as CORES em si são as do Substack, não as genéricas
# --tg-theme-* do cliente.
PAGE_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>San's Anthology</title>
<script src="https://telegram.org/js/telegram-web-app.js?63"></script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif:ital,wght@0,400;0,500;0,600;0,700;1,400&family=Noto+Serif+Display:wght@600;700&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #fffbeb;
    --bg-elevated: #ffffff;
    --card: #f5f0dd;
    --card-hover: #ece4c9;
    --border: #ddd9cb;
    --text: #2b2620;
    --text-muted: #78715f;
    --accent: #8a6d00;
    --accent-strong: #6b5400;
    --on-accent: #fffbeb;
    --shadow: 0 1px 3px rgba(43, 38, 32, 0.08), 0 1px 2px rgba(43, 38, 32, 0.06);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #1c1710; --bg-elevated: #221b12; --card: #2a2116; --card-hover: #352a1b;
      --border: #40331f; --text: #f2ead6; --text-muted: #a89a7d;
      --accent: #d9b64c; --accent-strong: #ecca6a; --on-accent: #1c1710;
      --shadow: 0 1px 3px rgba(0, 0, 0, 0.35), 0 1px 2px rgba(0, 0, 0, 0.3);
    }
  }
  :root[data-theme="light"] {
    --bg: #fffbeb; --bg-elevated: #ffffff; --card: #f5f0dd; --card-hover: #ece4c9;
    --border: #ddd9cb; --text: #2b2620; --text-muted: #78715f;
    --accent: #8a6d00; --accent-strong: #6b5400; --on-accent: #fffbeb;
    --shadow: 0 1px 3px rgba(43, 38, 32, 0.08), 0 1px 2px rgba(43, 38, 32, 0.06);
  }
  :root[data-theme="dark"] {
    --bg: #1c1710; --bg-elevated: #221b12; --card: #2a2116; --card-hover: #352a1b;
    --border: #40331f; --text: #f2ead6; --text-muted: #a89a7d;
    --accent: #d9b64c; --accent-strong: #ecca6a; --on-accent: #1c1710;
    --shadow: 0 1px 3px rgba(0, 0, 0, 0.35), 0 1px 2px rgba(0, 0, 0, 0.3);
  }

  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    font-family: "Noto Serif", Georgia, serif;
    background: var(--bg);
    color: var(--text);
    -webkit-font-smoothing: antialiased;
  }

  header {
    padding: 22px 16px 16px;
    text-align: center;
  }
  header h1 {
    margin: 0;
    font-family: "Noto Serif Display", "Noto Serif", serif;
    font-weight: 700;
    font-size: 23px;
    letter-spacing: 0.01em;
    color: var(--text);
  }
  header .tagline {
    margin: 4px 0 0;
    font-size: 13px;
    font-style: italic;
    color: var(--text-muted);
  }

  .tabs {
    display: flex;
    gap: 4px;
    padding: 0 12px;
    border-bottom: 1px solid var(--border);
  }
  .tab {
    flex: 1;
    text-align: center;
    padding: 10px 4px 12px;
    cursor: pointer;
    font-family: "Noto Serif", serif;
    font-weight: 600;
    font-size: 14.5px;
    color: var(--text-muted);
    border-bottom: 2px solid transparent;
    transition: color 0.15s ease;
  }
  .tab.active { color: var(--accent-strong); border-bottom-color: var(--accent); }

  .panel { display: none; padding: 14px 12px 24px; max-width: 720px; margin: 0 auto; }
  .panel.active { display: block; }

  #search {
    width: 100%;
    padding: 11px 14px;
    margin-bottom: 14px;
    border: 1px solid var(--border);
    border-radius: 10px;
    background: var(--bg-elevated);
    color: var(--text);
    font-family: "Noto Serif", serif;
    font-size: 15px;
  }
  #search:focus { outline: none; border-color: var(--accent); }
  #search::placeholder { color: var(--text-muted); }

  .article-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 12px;
  }
  .article-card {
    background: var(--card);
    border-radius: 12px;
    overflow: hidden;
    cursor: pointer;
    box-shadow: var(--shadow);
    transition: transform 0.12s ease, background 0.12s ease;
  }
  .article-card:active { transform: scale(0.97); background: var(--card-hover); }
  .article-cover {
    width: 100%;
    aspect-ratio: 4 / 3;
    object-fit: cover;
    display: block;
    background: linear-gradient(135deg, var(--card) 0%, var(--border) 100%);
  }
  .article-cover.placeholder {
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: "Noto Serif Display", serif;
    font-size: 26px;
    color: var(--accent);
    opacity: 0.55;
  }
  .article-title {
    padding: 10px 11px 12px;
    font-size: 13.5px;
    line-height: 1.35;
    font-weight: 500;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }

  #chat-log { padding-bottom: 96px; }
  .msg { margin-bottom: 16px; display: flex; flex-direction: column; }
  .msg.user { align-items: flex-end; }
  .msg.bot { align-items: flex-start; }
  .msg .who { font-size: 11px; color: var(--text-muted); margin-bottom: 4px; padding: 0 2px; }
  .msg .bubble {
    padding: 11px 14px;
    border-radius: 14px;
    max-width: 88%;
    line-height: 1.5;
    font-size: 15px;
    white-space: pre-wrap;
    overflow-wrap: break-word;
    background: var(--card);
    box-shadow: var(--shadow);
  }
  .msg .bubble a { color: var(--accent-strong); }
  .msg.user .bubble {
    background: var(--accent);
    color: var(--on-accent);
    border-bottom-right-radius: 4px;
  }
  .msg.bot .bubble { border-bottom-left-radius: 4px; }
  .msg.bot .bubble.pending { color: var(--text-muted); font-style: italic; }

  #chat-form {
    position: fixed; bottom: 0; left: 0; right: 0;
    display: flex; gap: 8px;
    padding: 10px 14px calc(10px + env(safe-area-inset-bottom));
    background: var(--bg);
    border-top: 1px solid var(--border);
  }
  #chat-input {
    flex: 1;
    padding: 11px 16px;
    border-radius: 22px;
    border: 1px solid var(--border);
    background: var(--bg-elevated);
    color: var(--text);
    font-family: "Noto Serif", serif;
    font-size: 15px;
  }
  #chat-input:focus { outline: none; border-color: var(--accent); }
  #chat-send {
    padding: 0 20px;
    border-radius: 22px;
    border: none;
    background: var(--accent);
    color: var(--on-accent);
    font-family: "Noto Serif", serif;
    font-weight: 600;
    font-size: 14.5px;
    cursor: pointer;
  }
  #chat-send:active { background: var(--accent-strong); }

  .empty, .loading {
    color: var(--text-muted);
    text-align: center;
    padding: 40px 20px;
    font-style: italic;
    font-size: 14px;
  }
</style>
</head>
<body>
<header>
  <h1>San&#8217;s Anthology</h1>
  <p class="tagline">artigos &amp; conversas sobre o que é perene</p>
</header>
<div class="tabs">
  <div class="tab active" data-panel="articles">Artigos</div>
  <div class="tab" data-panel="chat">Chat</div>
</div>

<div id="panel-articles" class="panel active">
  <input id="search" type="text" placeholder="Buscar por título...">
  <div id="article-list" class="loading">Carregando artigos...</div>
</div>

<div id="panel-chat" class="panel">
  <div id="chat-log"></div>
  <form id="chat-form">
    <input id="chat-input" type="text" placeholder="Pergunte sobre os artigos..." autocomplete="off">
    <button id="chat-send" type="submit">Enviar</button>
  </form>
</div>

<script>
const tg = window.Telegram && window.Telegram.WebApp;

function applyTheme() {
  if (!tg) return;
  document.documentElement.dataset.theme = tg.colorScheme === "dark" ? "dark" : "light";
}
if (tg) {
  tg.ready();
  tg.expand();
  applyTheme();
  tg.onEvent("themeChanged", applyTheme);
}

// --- Abas ---
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById("panel-" + tab.dataset.panel).classList.add("active");
  });
});

// --- Vitrine de artigos ---
let allArticles = [];

function renderArticles(list) {
  const container = document.getElementById("article-list");
  if (!list.length) {
    container.innerHTML = '<div class="empty">Nenhum artigo encontrado.</div>';
    return;
  }
  container.className = "article-grid";
  container.innerHTML = "";
  list.forEach((article) => {
    const card = document.createElement("div");
    card.className = "article-card";

    if (article.capa) {
      const img = document.createElement("img");
      img.className = "article-cover";
      img.loading = "lazy";
      img.src = article.capa;
      img.alt = "";
      img.onerror = () => { img.replaceWith(placeholderCover()); };
      card.appendChild(img);
    } else {
      card.appendChild(placeholderCover());
    }

    const title = document.createElement("div");
    title.className = "article-title";
    title.textContent = article.titulo;
    card.appendChild(title);

    card.addEventListener("click", () => {
      if (tg && tg.openLink) tg.openLink(article.url);
      else window.open(article.url, "_blank");
    });
    container.appendChild(card);
  });
}

function placeholderCover() {
  const div = document.createElement("div");
  div.className = "article-cover placeholder";
  div.textContent = "S";
  return div;
}

fetch("/miniapp/articles.json")
  .then((r) => r.json())
  .then((data) => { allArticles = data; renderArticles(allArticles); })
  .catch(() => {
    document.getElementById("article-list").innerHTML =
      '<div class="empty">Não consegui carregar os artigos agora.</div>';
  });

document.getElementById("search").addEventListener("input", (e) => {
  const term = e.target.value.trim().toLowerCase();
  renderArticles(!term ? allArticles : allArticles.filter((a) => a.titulo.toLowerCase().includes(term)));
});

// --- Chat ---
let previousAnswer = null;

function escapeHtml(str) {
  return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// Mesma ideia de markdown_to_telegram_html em bot.py: o RAG devolve
// **negrito**, *itálico* e [texto](url) — sem isso, os marcadores
// apareceriam literalmente na bolha do chat.
function formatAnswer(text) {
  let out = escapeHtml(text);
  out = out.replace(/\\*\\*(.+?)\\*\\*/gs, "<strong>$1</strong>");
  out = out.replace(/(?<!\\*)\\*([^*\\n]+?)\\*(?!\\*)/g, "<em>$1</em>");
  out = out.replace(/\\[([^\\[\\]]+)\\]\\((https?:\\/\\/[^\\s()]+)\\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return out;
}

function appendMessage(who, text) {
  const log = document.getElementById("chat-log");
  const wrapper = document.createElement("div");
  wrapper.className = "msg " + who;
  const label = document.createElement("div");
  label.className = "who";
  label.textContent = who === "user" ? "Você" : "Bot";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = text;
  wrapper.appendChild(label);
  wrapper.appendChild(bubble);
  log.appendChild(wrapper);
  wrapper.scrollIntoView({ block: "end", behavior: "smooth" });
  return bubble;
}

document.getElementById("chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = document.getElementById("chat-input");
  const question = input.value.trim();
  if (!question) return;
  input.value = "";
  appendMessage("user", question);
  const bubble = appendMessage("bot", "Pensando...");
  bubble.classList.add("pending");

  fetch("/miniapp/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      initData: tg ? tg.initData : "",
      question: question,
      previous_answer: previousAnswer,
    }),
  })
    .then((r) => r.json())
    .then((data) => {
      bubble.classList.remove("pending");
      if (data.answer) {
        bubble.innerHTML = formatAnswer(data.answer);
        previousAnswer = data.answer;
      } else {
        bubble.textContent = data.error || "Não consegui responder agora.";
      }
    })
    .catch(() => {
      bubble.classList.remove("pending");
      bubble.textContent = "Não consegui responder agora.";
    });
});
</script>
</body>
</html>
"""
