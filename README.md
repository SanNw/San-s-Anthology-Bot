# San's Anthology Bot

Bot que lê o feed RSS de uma publicação do Substack e publica automaticamente os artigos novos em um canal do Telegram, do mais antigo pro mais novo, sem repetir artigo já publicado.

## Como funciona

- Lê o RSS configurado em `SUBSTACK_RSS_URL`.
- Compara os artigos do feed com os IDs guardados em `posted.json` (criado automaticamente na raiz do projeto).
- Publica no Telegram cada artigo novo: título em negrito, resumo em texto puro (até ~500 caracteres), imagem de capa (quando existir) e um link "Leia o artigo completo →".
- Usa `sendPhoto` quando há imagem de capa e `sendMessage` quando não há, sempre com `parse_mode HTML`.

## 1. Criar o bot no @BotFather

1. No Telegram, abra uma conversa com [@BotFather](https://t.me/BotFather).
2. Envie `/newbot` e siga as instruções (nome e username do bot).
3. O BotFather vai te dar um token no formato `123456789:ABCdef...` — isso é o `TELEGRAM_BOT_TOKEN`.
4. Adicione o bot como **administrador** do canal onde ele vai postar (com permissão de enviar mensagens).

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
SUBSTACK_RSS_URL=https://seudominio.substack.com/feed
```

O `.env` está no `.gitignore` — nunca é commitado.

## 4. Rodar localmente

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

Na primeira execução, todos os artigos do feed são considerados "novos" e publicados (do mais antigo pro mais novo). As próximas execuções só publicam o que ainda não estiver em `posted.json`.

Para rodar os testes:

```bash
python -m unittest test_bot.py -v
```

## 5. Configurar o GitHub Actions

O workflow em `.github/workflows/telegram-substack.yml` roda o bot a cada hora (`cron: "0 * * * *"`) e também pode ser disparado manualmente pela aba **Actions** do repositório (`workflow_dispatch`).

Para funcionar, cadastre os três Secrets no repositório:

1. No GitHub, vá em **Settings → Secrets and variables → Actions → New repository secret**.
2. Crie:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHANNEL_ID`
   - `SUBSTACK_RSS_URL`

O workflow já está configurado com `permissions: contents: write` para poder commitar o `posted.json` atualizado de volta no repositório após cada execução, mantendo o histórico de artigos já publicados entre uma execução e outra.

## Estrutura do projeto

```
bot.py                                  # script principal
test_bot.py                             # testes das funções de parsing
requirements.txt                        # dependências (feedparser, requests, python-dotenv)
.env.example                            # modelo das variáveis de ambiente
posted.json                             # gerado automaticamente (ignorado no git local, commitado pelo Actions)
.github/workflows/telegram-substack.yml # automação horária via GitHub Actions
```
