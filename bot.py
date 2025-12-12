# bot.py — Bot replicador multi-origem/multi-destino (fila + worker)
# Versão final com tratamento de erros, retries, e handler robusto.

import asyncio
import os
import logging
import json
from pathlib import Path
from typing import Dict, List, Tuple, Optional

from telegram import Update, Message
from telegram.error import TimedOut, RetryAfter, NetworkError
from telegram.ext import (
    ApplicationBuilder,
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== CONFIGURAÇÕES ==================
# Token do bot (já incluído conforme solicitado)
BOT_TOKEN = "8522495959:AAEsDCXOVmbK2okjVfaQGhZLDMBulN6ML7g"

# Delay (pode ser sobrescrito pela variável de ambiente FORWARD_DELAY_SECONDS)
FORWARD_DELAY_SECONDS = int(os.environ.get("FORWARD_DELAY_SECONDS", 30))

# Arquivo de persistência dos pares origem->destino
MAPPINGS_FILE = Path("mappings.json")

# ==================================================

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN não informado!")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Fila global: cada item é (Message, List[int])
message_queue: "asyncio.Queue[Tuple[Message, List[int]]]" = asyncio.Queue()


# ---------------- Persistência ----------------
def load_links() -> Dict[str, List[int]]:
    """Carrega pares origem->destino do arquivo mappings.json (se existir)."""
    if not MAPPINGS_FILE.exists():
        return {}
    try:
        with MAPPINGS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # garante chaves como string e valores como int
        links = {str(k): [int(x) for x in v] for k, v in data.get("links", {}).items()}
        return links
    except Exception as e:
        logger.error("Erro ao carregar mappings.json: %s", e)
        return {}


def save_links(links: Dict[str, List[int]]) -> None:
    """Salva pares origem->destino no arquivo mappings.json."""
    try:
        with MAPPINGS_FILE.open("w", encoding="utf-8") as f:
            json.dump({"links": links}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Erro ao salvar mappings.json: %s", e)


# ---------------- Worker (fila global) ----------------
async def forward_worker(app: Application):
    """Worker que consome a fila e encaminha UMA mensagem por vez, respeitando delay e retries."""
    logger.info("Worker de encaminhamento iniciado (fila global).")
    while True:
        try:
            msg, targets = await message_queue.get()
        except Exception as e:
            logger.exception("Erro ao obter item da fila: %s", e)
            await asyncio.sleep(1)
            continue

        try:
            # Delay global antes de cada mensagem encaminhada
            await asyncio.sleep(FORWARD_DELAY_SECONDS)

            for target_chat_id in targets:
                for attempt in range(3):  # até 3 tentativas por destino
                    try:
                        await app.bot.copy_message(
                            chat_id=target_chat_id,
                            from_chat_id=msg.chat.id,
                            message_id=getattr(msg, "message_id", None),
                        )
                        logger.info(
                            "Mensagem encaminhada: %s -> %s (msg_id=%s)",
                            msg.chat.id,
                            target_chat_id,
                            getattr(msg, "message_id", None),
                        )
                        break  # sucesso, sai do loop de tentativas

                    except RetryAfter as e:
                        wait = int(getattr(e, "retry_after", 5)) + 1
                        logger.warning(
                            "RetryAfter (%ss). Aguardando antes de tentar novamente...", wait
                        )
                        await asyncio.sleep(wait)

                    except TimedOut:
                        logger.warning(
                            "TimedOut ao encaminhar %s -> %s (msg_id=%s) tentativa %s/3",
                            msg.chat.id,
                            target_chat_id,
                            getattr(msg, "message_id", None),
                            attempt + 1,
                        )
                        if attempt == 2:
                            logger.error(
                                "Falha após 3 timeouts: %s -> %s (msg_id=%s)",
                                msg.chat.id,
                                target_chat_id,
                                getattr(msg, "message_id", None),
                            )
                        else:
                            await asyncio.sleep(5)

                    except NetworkError as e:
                        logger.warning(
                            "NetworkError ao encaminhar %s -> %s (msg_id=%s): %s",
                            msg.chat.id, target_chat_id, getattr(msg, "message_id", None), e
                        )
                        await asyncio.sleep(5)

                    except Exception as e:
                        # Erro não recuperável para essa mensagem/destino
                        logger.error(
                            "Erro inesperado ao encaminhar %s -> %s (msg_id=%s): %s",
                            msg.chat.id,
                            target_chat_id,
                            getattr(msg, "message_id", None),
                            e,
                        )
                        break

        except Exception as e:
            logger.exception("Erro no worker ao processar item da fila: %s", e)
        finally:
            try:
                message_queue.task_done()
            except Exception:
                pass


# ---------------- Comandos ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🤖 BOT REPLICADOR MULTI-CANAIS/GRUPOS\n\n"
        "Comandos:\n"
        "/add_link ORIGEM DESTINO   - cria par origem->destino\n"
        "/remove_link ORIGEM DESTINO - remove par\n"
        "/list_links                - listar pares\n"
        "/on                        - iniciar encaminhamento\n"
        "/off                       - parar encaminhamento\n"
        "/status                    - status do bot\n"
        "/chatid                    - mostra ID deste chat\n\n"
        f"Delay atual entre envios: {FORWARD_DELAY_SECONDS} segundos\n"
        "Obs.: mensagens enviadas enquanto o bot estiver OFF não podem ser recuperadas pela API."
    )
    await update.effective_message.reply_text(text)


async def cmd_add_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.effective_message.reply_text("Uso: /add_link ORIGEM DESTINO")
        return
    try:
        src = int(context.args[0])
        dst = int(context.args[1])
    except Exception:
        await update.effective_message.reply_text("IDs inválidos. Use números (ex: -1001234567890).")
        return

    links: Dict[str, List[int]] = context.application.bot_data.get("links", {})
    arr = links.get(str(src), [])
    if dst not in arr:
        arr.append(dst)
    links[str(src)] = arr
    context.application.bot_data["links"] = links
    save_links(links)
    await update.effective_message.reply_text(f"Link adicionado: {src} -> {dst}")


async def cmd_remove_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        await update.effective_message.reply_text("Uso: /remove_link ORIGEM DESTINO")
        return
    try:
        src = int(context.args[0])
        dst = int(context.args[1])
    except Exception:
        await update.effective_message.reply_text("IDs inválidos.")
        return

    links: Dict[str, List[int]] = context.application.bot_data.get("links", {})
    if str(src) not in links:
        await update.effective_message.reply_text("Origem não cadastrada.")
        return

    if dst in links[str(src)]:
        links[str(src)].remove(dst)
        if not links[str(src)]:
            del links[str(src)]
        context.application.bot_data["links"] = links
        save_links(links)
        await update.effective_message.reply_text(f"Link removido: {src} -> {dst}")
    else:
        await update.effective_message.reply_text("Esse link não existe.")


async def cmd_list_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    links: Dict[str, List[int]] = context.application.bot_data.get("links", {})
    if not links:
        await update.effective_message.reply_text("Nenhum link cadastrado.")
        return
    lines = ["Pares cadastrados:"]
    for src, arr in links.items():
        for dst in arr:
            lines.append(f"{src} -> {dst}")
    await update.effective_message.reply_text("\n".join(lines))


async def cmd_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["forwarding"] = True
    await update.effective_message.reply_text("Encaminhamento LIGADO.")


async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["forwarding"] = False
    await update.effective_message.reply_text("Encaminhamento DESLIGADO.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bd = context.application.bot_data
    links = bd.get("links", {})
    fwd = bd.get("forwarding", False)
    fila = message_queue.qsize()
    text = (
        f"STATUS DO BOT\n\n"
        f"Encaminhamento: {'ON' if fwd else 'OFF'}\n"
        f"Pares cadastrados: {sum(len(v) for v in links.values())}\n"
        f"Mensagens na fila: {fila}\n"
        f"Delay: {FORWARD_DELAY_SECONDS} segundos\n"
    )
    await update.effective_message.reply_text(text)


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is not None:
        await update.effective_message.reply_text(f"ID deste chat: {chat.id}")
    else:
        await update.effective_message.reply_text("Não foi possível obter o ID deste chat.")


# ---------------- collect_messages (robusto) ----------------
async def collect_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handler robusto para coletar mensagens:
    - trata message, channel_post, edited_message, edited_channel_post
    - ignora outros tipos de update
    - protege contra msg/chat None
    """
    try:
        msg: Optional[Message] = None

        # tenta obter a "message-like" object
        if update.message is not None:
            msg = update.message
        elif update.channel_post is not None:
            msg = update.channel_post
        elif update.edited_message is not None:
            msg = update.edited_message
        elif update.edited_channel_post is not None:
            msg = update.edited_channel_post
        else:
            # update de outro tipo (callback_query, inline_query, etc.) — ignorar
            logger.debug("Update recebido sem message-like, ignorando: %s", update)
            return

        # proteção extra
        if msg is None or getattr(msg, "chat", None) is None:
            logger.debug("Mensagem ou chat ausente — ignorando update.")
            return

        bd = context.application.bot_data
        if not bd.get("forwarding", False):
            return

        src = str(msg.chat.id)
        links: Dict[str, List[int]] = bd.get("links", {})

        if src not in links:
            return

        targets = links[src]

        # adiciona à fila para o worker processar
        await message_queue.put((msg, targets))
        logger.info(
            "Mensagem adicionada à fila: origem=%s msg_id=%s destinos=%s (fila=%s)",
            msg.chat.id,
            getattr(msg, "message_id", None),
            targets,
            message_queue.qsize(),
        )

    except Exception as e:
        logger.exception("Erro no collect_messages: %s", e)
        return


# ---------------- Global error handler ----------------
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registra exceções não tratadas no application."""
    logger.exception("Exceção não tratada na aplicação: %s", context.error)


# ---------------- Main ----------------
def main():
    # Python 3.13/3.14: criar e registrar loop manualmente
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # inicializa bot_data
    app.bot_data["links"] = load_links()
    app.bot_data["forwarding"] = False  # iniciar desligado por segurança

    # handlers de comando
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add_link", cmd_add_link))
    app.add_handler(CommandHandler("remove_link", cmd_remove_link))
    app.add_handler(CommandHandler("list_links", cmd_list_links))
    app.add_handler(CommandHandler("on", cmd_on))
    app.add_handler(CommandHandler("off", cmd_off))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("chatid", cmd_chatid))

    # handler para mensagens (tudo que não é comando)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, collect_messages))

    # handler global de erros
    app.add_error_handler(global_error_handler)

    # inicia worker da fila no mesmo loop
    loop.create_task(forward_worker(app))

    logger.info("🤖 Bot replicador iniciado!")
    app.run_polling()


if __name__ == "__main__":
    main()
