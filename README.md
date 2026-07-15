# San's Anthology Bot

Bot que lê o feed RSS de uma publicação do Substack e publica automaticamente os artigos novos em um canal do Telegram e no privado de quem se inscrever, do mais antigo pro mais novo, sem repetir artigo já publicado. Também responde a comandos.

## Como funciona

- Lê o RSS configurado em `SUBSTACK_RSS_URL`.
- Compara os artigos do feed com os IDs guardados em `posted.json` (criado automaticamente na raiz do projeto).
- Publica cada artigo novo no canal (`TELEGRAM_CHANNEL_ID`) **e** no privado de cada assinante em `subscribers.json`: título em negrito, resumo em texto puro (até ~500 caracteres), imagem de capa (quando existir) e um link "Leia o artigo completo →".
- Usa `sendPhoto` quando há imagem de capa e `sendMessage` quando não há, sempre com `parse_mode HTML`.
- A cada execução, também busca mensagens novas (`getUpdates`) e responde aos comandos abaixo.

### Comandos do bot

| Comando | O que faz |
|---|---|
| `/start` | Inscreve o usuário para receber os artigos novos no privado |
| `/stop` | Cancela a inscrição |
| `/categorias` | Lista as categorias/tags encontradas nos artigos do feed |
| `/recentes` | Lista os 5 artigos mais recentes com link |
| `/substack` | Envia o link para assinar o Substack |
| `/sugestao` | Avisa que sugestões de assunto podem ser mandadas diretamente numa mensagem |

Como o bot roda via GitHub Actions (sem servidor 24/7), os comandos são processados a cada execução do workflow — não instantaneamente. O cron está configurado para rodar a cada 10 minutos.

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
```

> A URL do feed deste projeto é `https://san55.substack.com/feed`. Ela não é um dado sensível, mas ainda assim é cadastrada como Secret abaixo — só o token e o channel ID exigiriam sigilo, mas manter as três juntas como Secrets simplifica a configuração do Actions.

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

O workflow em `.github/workflows/telegram-substack.yml` roda o bot a cada 10 minutos (`cron: "*/10 * * * *"`) e também pode ser disparado manualmente pela aba **Actions** do repositório (`workflow_dispatch`).

Para funcionar, cadastre os três Secrets no repositório:

1. No GitHub, vá em **Settings → Secrets and variables → Actions → New repository secret**.
2. Crie:
   - `TELEGRAM_BOT_TOKEN` (gerado pelo @BotFather)
   - `TELEGRAM_CHANNEL_ID` (veja a seção 2 acima)
   - `SUBSTACK_RSS_URL` → `https://san55.substack.com/feed`

O workflow já está configurado com `permissions: contents: write` para poder commitar de volta no repositório, após cada execução, os arquivos de estado (`posted.json`, `subscribers.json`, `update_offset.json`) — isso mantém o histórico de artigos publicados, a lista de assinantes e o progresso do processamento de comandos entre uma execução e outra.

## Estrutura do projeto

```
bot.py                                  # script principal
test_bot.py                             # testes das funções de parsing e dos comandos
requirements.txt                        # dependências (feedparser, requests, python-dotenv)
.env.example                            # modelo das variáveis de ambiente
posted.json                             # artigos já publicados (ignorado no git local, commitado pelo Actions)
subscribers.json                        # chat_ids inscritos para receber artigos no privado (idem)
update_offset.json                      # último update_id do Telegram já processado (idem)
.github/workflows/telegram-substack.yml # automação via GitHub Actions (a cada 10 min)
```
