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
# do próprio SDK do Telegram) — duas abas: vitrine de artigos com busca no
# cliente, e chat com o mesmo RAG do bot. As cores usam as variáveis que o
# telegram-web-app.js injeta (--tg-theme-*), então o Mini App já nasce no
# tema (claro/escuro) que a pessoa usa no Telegram, sem lógica extra.
PAGE_HTML = """<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>San's Anthology</title>
<script src="https://telegram.org/js/telegram-web-app.js?63"></script>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: var(--tg-theme-bg-color, #ffffff);
    color: var(--tg-theme-text-color, #000000);
  }
  header {
    padding: 16px;
    text-align: center;
    border-bottom: 1px solid var(--tg-theme-hint-color, #ccc);
  }
  header h1 { margin: 0; font-size: 20px; }
  .tabs { display: flex; border-bottom: 1px solid var(--tg-theme-hint-color, #ccc); }
  .tab {
    flex: 1; text-align: center; padding: 12px; cursor: pointer;
    color: var(--tg-theme-hint-color, #888);
    border-bottom: 2px solid transparent;
  }
  .tab.active {
    color: var(--tg-theme-link-color, #2481cc);
    border-bottom-color: var(--tg-theme-link-color, #2481cc);
    font-weight: 600;
  }
  .panel { display: none; padding: 12px; }
  .panel.active { display: block; }
  #search {
    width: 100%; padding: 10px 12px; margin-bottom: 12px;
    border: 1px solid var(--tg-theme-hint-color, #ccc); border-radius: 8px;
    background: var(--tg-theme-secondary-bg-color, #f5f5f5);
    color: var(--tg-theme-text-color, #000);
    font-size: 15px;
  }
  .article {
    padding: 12px; margin-bottom: 8px; border-radius: 8px;
    background: var(--tg-theme-secondary-bg-color, #f5f5f5);
    cursor: pointer;
  }
  .article:active { opacity: 0.7; }
  #chat-log { padding-bottom: 90px; }
  .msg { margin-bottom: 14px; }
  .msg .who { font-size: 12px; color: var(--tg-theme-hint-color, #888); margin-bottom: 3px; }
  .msg .bubble {
    padding: 10px 12px; border-radius: 10px; white-space: pre-wrap;
    background: var(--tg-theme-secondary-bg-color, #f5f5f5);
  }
  .msg.user .bubble { background: var(--tg-theme-button-color, #2481cc); color: var(--tg-theme-button-text-color, #fff); }
  #chat-form {
    position: fixed; bottom: 0; left: 0; right: 0; display: flex; gap: 8px;
    padding: 10px 12px; background: var(--tg-theme-bg-color, #fff);
    border-top: 1px solid var(--tg-theme-hint-color, #ccc);
  }
  #chat-input {
    flex: 1; padding: 10px 12px; border-radius: 20px;
    border: 1px solid var(--tg-theme-hint-color, #ccc);
    background: var(--tg-theme-secondary-bg-color, #f5f5f5);
    color: var(--tg-theme-text-color, #000);
    font-size: 15px;
  }
  #chat-send {
    padding: 10px 18px; border-radius: 20px; border: none;
    background: var(--tg-theme-button-color, #2481cc);
    color: var(--tg-theme-button-text-color, #fff);
    font-weight: 600;
  }
  .empty, .loading { color: var(--tg-theme-hint-color, #888); text-align: center; padding: 24px; }
</style>
</head>
<body>
<header><h1>📚 San's Anthology</h1></header>
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
if (tg) { tg.ready(); tg.expand(); }

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
  container.innerHTML = "";
  list.forEach((article) => {
    const div = document.createElement("div");
    div.className = "article";
    div.textContent = article.titulo;
    div.addEventListener("click", () => {
      if (tg && tg.openLink) tg.openLink(article.url);
      else window.open(article.url, "_blank");
    });
    container.appendChild(div);
  });
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

function appendMessage(who, text) {
  const log = document.getElementById("chat-log");
  const wrapper = document.createElement("div");
  wrapper.className = "msg " + who;
  wrapper.innerHTML = '<div class="who">' + (who === "user" ? "Você" : "Bot") + '</div><div class="bubble"></div>';
  wrapper.querySelector(".bubble").textContent = text;
  log.appendChild(wrapper);
  log.scrollIntoView({ block: "end" });
  return wrapper;
}

document.getElementById("chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = document.getElementById("chat-input");
  const question = input.value.trim();
  if (!question) return;
  input.value = "";
  appendMessage("user", question);
  const pending = appendMessage("bot", "Pensando...");

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
      const text = data.answer || data.error || "Não consegui responder agora.";
      pending.querySelector(".bubble").textContent = text;
      if (data.answer) previousAnswer = data.answer;
    })
    .catch(() => {
      pending.querySelector(".bubble").textContent = "Não consegui responder agora.";
    });
});
</script>
</body>
</html>
"""
