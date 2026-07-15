# San's Anthology Bot

Bot que lê o feed RSS de uma publicação do Substack e publica automaticamente os artigos novos em um canal do Telegram e no privado de quem se inscrever, do mais antigo pro mais novo, sem repetir artigo já publicado. Também responde a comandos e conversa sobre o conteúdo dos artigos (RAG).

## Como funciona

- Lê o RSS configurado em `SUBSTACK_RSS_URL`.
- Compara os artigos do feed com os IDs guardados em `posted.json` (criado automaticamente na raiz do projeto).
- Publica cada artigo novo no canal (`TELEGRAM_CHANNEL_ID`) **e** no privado de cada assinante em `subscribers.json`: título em negrito, resumo em texto puro (até ~500 caracteres), imagem de capa (quando existir) e um link "Leia o artigo completo →".
- Usa `sendPhoto` quando há imagem de capa e `sendMessage` quando não há, sempre com `parse_mode HTML`.
- A cada execução, também busca mensagens novas (`getUpdates`) e responde aos comandos abaixo **ou**, se a mensagem não for um comando, decide se deve responder via chat (RAG) com base no conteúdo dos artigos.

### Chat sobre os artigos (RAG)

Além dos comandos, o bot responde perguntas com base **exclusivamente** no conteúdo indexado dos artigos do Substack:

- **Em chat privado com o bot:** sempre responde.
- **Em grupos:** só responde se for **mencionado** (`@usuario_do_bot`) ou se a mensagem for **reply direto a uma mensagem do próprio bot**. Qualquer outra mensagem em grupo é ignorada.
- **Guardrail de escopo:** se a pergunta fugir dos temas cobertos pelos artigos — mesmo no meio de uma conversa que começou dentro do tema — o bot recusa educadamente, avisando que não tem permissão para falar sobre assuntos fora desses tópicos. Cada mensagem é avaliada individualmente por relevância (busca por similaridade contra o índice de artigos); se a melhor similaridade encontrada ficar abaixo de `LIMIAR_RELEVANCIA` (constante em `rag.py`, padrão `0.35`), o bot recusa direto, sem gastar uma chamada à API do Claude.
- Funciona a partir do índice gerado por `index_articles.py` (veja a seção "Indexar os artigos" abaixo) — sem indexação, o bot sempre recusa por falta de conteúdo.

### Comandos do bot

| Comando | O que faz |
|---|---|
| `/start` | Inscreve o usuário para receber os artigos novos no privado |
| `/stop` | Cancela a inscrição |
| `/categorias` | Lista as categorias/tags encontradas nos artigos do feed |
| `/recentes` | Lista os 5 artigos mais recentes com link |
| `/substack` | Envia o link para assinar o Substack |
| `/sugestao` | Avisa que sugestões de assunto podem ser mandadas diretamente numa mensagem |

O bot roda como um processo contínuo (Background Worker no Render) e fica em long polling com o Telegram — comandos e respostas do chat chegam quase instantaneamente. O feed RSS é reconsultado a cada `FEED_CHECK_INTERVAL_SECONDS` (padrão 300s = 5 min; veja a seção "Fazer deploy no Render").

## 1. Criar o bot no @BotFather

1. No Telegram, abra uma conversa com [@BotFather](https://t.me/BotFather).
2. Envie `/newbot` e siga as instruções (nome e username do bot).
3. O BotFather vai te dar um token no formato `123456789:ABCdef...` — isso é o `TELEGRAM_BOT_TOKEN`.
4. Adicione o bot como **administrador** do canal onde ele vai postar (com permissão de enviar mensagens).
5. (Opcional, recomendado) Envie `/setcommands` ao BotFather e cadastre os comandos do bot, para aparecerem no menu do Telegram:
   ```
   start - Inscrever para receber os artigos no privado
   stop - Cancelar inscrição
   categorias - Categorias de artigos
   recentes - Últimos artigos recentes
   substack - Link para se inscrever no Substack
   sugestao - Link para você dar sugestão de assuntos
   ```
6. (Opcional) Envie `/setdescription` ao BotFather para definir o texto que aparece antes do usuário mandar a primeira mensagem, por exemplo: "Mande /start para receber os artigos mais recentes de San's Anthology aqui no privado 📚".

> Trate o token como uma senha: não o compartilhe, não o cole em issues/PRs públicos, e nunca o commite no repositório.

## 2. Descobrir o `TELEGRAM_CHANNEL_ID`

- **Canal público** (tem `@usuario`): use o próprio username, ex. `@meucanal`.
- **Canal privado**: o ID é numérico e começa com `-100`. Para descobrir:
  1. Encaminhe (forward) uma mensagem do canal para o bot [@userinfobot](https://t.me/userinfobot) ou [@JsonDumpBot](https://t.me/JsonDumpBot), ou
  2. Adicione o bot [@RawDataBot](https://t.me/RawDataBot) ao canal temporariamente e veja o `chat.id` que aparece nas mensagens encaminhadas por ele.
  3. O valor final será algo como `-1001234567890`.

## 3. Preencher o `.env`

Copie o exemplo e edite com seus valores:

```bash
cp .env.example .env
```

```env
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ
TELEGRAM_CHANNEL_ID=@meucanal
SUBSTACK_RSS_URL=https://san55.substack.com/feed
ANTHROPIC_API_KEY=sk-ant-api03-...
VOYAGE_API_KEY=pa-...
```

> A URL do feed deste projeto é `https://san55.substack.com/feed`. Ela não é um dado sensível, mas ainda assim é cadastrada como Secret abaixo — só o token e o channel ID exigiriam sigilo, mas manter as três juntas como Secrets simplifica a configuração do Actions.
>
> `ANTHROPIC_API_KEY` (console.anthropic.com) e `VOYAGE_API_KEY` (dash.voyageai.com) são usadas só pelo chat/RAG — sem elas o bot continua publicando artigos normalmente, mas qualquer pergunta feita a ele vai falhar.

O `.env` está no `.gitignore` — nunca é commitado.

## 4. Rodar localmente

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

`python bot.py` roda em loop contínuo (o mesmo processo que sobe no Render) — pare com Ctrl+C. Na primeira execução, todos os artigos do feed são considerados "novos" e publicados (do mais antigo pro mais novo). As próximas execuções só publicam o que ainda não estiver em `posted.json`. Localmente os arquivos de estado ficam na raiz do projeto; em produção, a env var `DATA_DIR` aponta pro Disk persistente do Render (veja a seção "Fazer deploy no Render").

Para rodar os testes:

```bash
python -m unittest test_bot.py test_rag.py -v
```

## 5. Indexar os artigos (RAG)

Antes do chat funcionar, é preciso indexar o conteúdo completo dos artigos. Isso não faz parte do workflow automático — roda sob demanda, localmente:

```bash
python index_articles.py
```

O script:
1. Busca o sitemap do seu Substack pra listar todas as URLs de posts (não depende do RSS, que só traz os mais recentes).
2. Busca o texto completo de cada artigo.
3. Divide em blocos (chunks) de ~500–1000 caracteres, respeitando parágrafos.
4. Gera embeddings de cada chunk via Voyage AI (`voyage-3`) e salva tudo em `articles_index.json`.

É incremental: se `articles_index.json` já existe, artigos cujo `url` já está lá são pulados — só o que for novo é indexado (não recomputa embeddings do que já existe). Depois de publicar um artigo novo no Substack, rode o script de novo pra ele entrar no chat.

Pra ajustar o quão exigente o bot é antes de recusar uma pergunta por falta de relevância, edite a constante `LIMIAR_RELEVANCIA` em `rag.py` (padrão `0.35`, escala de -1 a 1 — quanto maior, mais rigoroso).

Depois de gerar/atualizar o `articles_index.json`, **commite o arquivo** — ele precisa estar no repositório pra o bot rodando no GitHub Actions conseguir usá-lo.

## 6. Fazer deploy no Render (Web Service free + webhook)

O bot roda no plano **free** do Render como **Web Service**, no modo **webhook**: em vez de ficar perguntando ao Telegram por mensagens novas (long polling), o Telegram empurra cada mensagem via HTTP `POST` direto pro bot. Isso existe especificamente por causa das limitações do plano free:

- Web Service free **dorme depois de ~15 min sem tráfego HTTP** e não tem Disk persistente. Um Background Worker (processo sempre ligado, com long polling e Disk) seria o desenho mais simples, mas não tem tier gratuito no Render.
- Pra compensar, um serviço externo e gratuito (ex: [cron-job.org](https://cron-job.org) ou [UptimeRobot](https://uptimerobot.com)) precisa bater periodicamente (a cada ~10 min) num endpoint HTTP do bot — isso mantém o serviço acordado. Esse mesmo "ping" é aproveitado internamente pra checar o feed RSS e sincronizar o estado com o GitHub (veja abaixo), então **a frequência do ping define a latência de publicação de artigos novos** — pings a cada 10 min ≈ artigos novos aparecem em até ~10 min.
- Sem Disk, `posted.json`, `subscribers.json` e `update_offset.json` não sobrevivem a um redeploy/reinício se ficassem só no disco efêmero do container. Por isso o bot **commita e dá push desses três arquivos de volta pro GitHub** periodicamente (`GITHUB_TOKEN`, veja o passo 4) — é a mesma ideia que o workflow do GitHub Actions já fazia, só que disparada pelo processo em vez de pelo Actions.

Se no futuro você migrar para um plano pago, o worker com long polling + Disk (sem depender de ping externo nem de commitar estado no Git) é a opção mais simples e robusta — peça pra eu voltar a montar esse desenho.

O repositório já inclui um `render.yaml` (Blueprint) com a configuração pronta. Passo a passo:

1. **Gere um GitHub Personal Access Token** com permissão de escrita neste repositório: [github.com/settings/tokens](https://github.com/settings/tokens) → **Fine-grained tokens** → selecione só este repositório → permissão **Contents: Read and write**. Trate como uma senha (mesmas regras dos outros secrets).
2. No [dashboard do Render](https://dashboard.render.com), **New → Blueprint** e aponte para este repositório (ele detecta o `render.yaml` automaticamente). Ou crie manualmente um **New → Web Service** apontando pro repo, com Build Command `pip install -r requirements.txt`, Start Command `python bot.py` e plano **Free**.
3. Em **Environment**, cadastre os seis secrets (o Blueprint já declara as chaves com `sync: false`, então o Render vai pedir os valores na primeira vez):
   - `TELEGRAM_BOT_TOKEN` (gerado pelo @BotFather)
   - `TELEGRAM_CHANNEL_ID` (veja a seção 2 acima)
   - `SUBSTACK_RSS_URL` → `https://san55.substack.com/feed`
   - `ANTHROPIC_API_KEY` (console.anthropic.com) — usada pelo chat/RAG
   - `VOYAGE_API_KEY` (dash.voyageai.com) — usada pelo chat/RAG para embeddings de busca
   - `GITHUB_TOKEN` — o token do passo 1, usado só pra commitar de volta os arquivos de estado
4. Opcional (recomendado): defina `TELEGRAM_WEBHOOK_SECRET` com um valor aleatório — o bot passa isso pro Telegram ao registrar o webhook e rejeita qualquer `POST` que não venha com esse mesmo segredo no header, evitando que outra pessoa forje mensagens pro seu bot.
5. Opcional: ajuste `FEED_CHECK_INTERVAL_SECONDS` (padrão `300`) e `STATE_SYNC_INTERVAL_SECONDS` (padrão `60`) como env vars se quiser mudar a frequência de checagem do feed ou do commit de estado.
6. Faça o deploy. O Render vai te dar uma URL pública (`https://algo.onrender.com`) — ela já é usada automaticamente pelo bot pra se registrar como webhook no Telegram (via a env var `RENDER_EXTERNAL_URL`, que o Render define sozinho).
7. Configure o keep-alive: em [cron-job.org](https://cron-job.org) (ou similar), crie uma tarefa que faça `GET` na URL do serviço a cada 5–10 minutos.

> Importante: rode o bot em **um único lugar por vez**. Rodar local (`python bot.py`, modo polling) ao mesmo tempo que o webhook no Render está ativo faz o Telegram brigar sobre pra onde mandar as mensagens. O workflow antigo do GitHub Actions (`.github/workflows/telegram-substack.yml`) foi removido deste repositório por esse motivo.

Depois de gerar/atualizar o `articles_index.json` localmente (seção 5), **commite e faça push** — o Render reconstrói a partir do repositório a cada deploy, então o índice precisa estar versionado pra o chat/RAG funcionar em produção.

## Estrutura do projeto

```
bot.py                    # script principal: modo webhook (Render) ou polling (local), publica artigos, comandos e dispatch do chat/RAG
rag.py                    # busca por similaridade, guardrail de escopo e chamada à API do Claude
index_articles.py         # indexação (sob demanda) do texto completo dos artigos pro RAG
test_bot.py                # testes das funções de parsing, comandos, dispatch de chat e do servidor webhook
test_rag.py                # testes de chunking, similaridade de cosseno e guardrail do RAG
requirements.txt           # dependências (feedparser, requests, python-dotenv, anthropic, voyageai, numpy)
.env.example                # modelo das variáveis de ambiente
render.yaml                 # Blueprint do Render (Web Service free)
posted.json                 # artigos já publicados (versionado; o bot commita as mudanças em produção)
subscribers.json            # chat_ids inscritos para receber artigos no privado (idem)
update_offset.json          # só usado no modo polling local; irrelevante no webhook (idem)
articles_index.json         # chunks + embeddings dos artigos, gerado por index_articles.py (versionado)
```
