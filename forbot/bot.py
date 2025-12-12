import asyncio
import os
import logging
import json
from pathlib import Path
from typing import Dict, List, Tuple

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

# ============================================================
# 🔧 CONFIGURAÇÕES DO BOT
# ============================================================

# Token já incluído conforme solicitado:
BOT_TOKEN = "8522495959:AAEsDCXOVmbK2okjVfaQGhZLDMBulN6ML7g"

# Se quiser deixar o Railway controlar o delay:
FORWARD_DELAY_SECONDS = int(os.environ.get("FORWARD_DELAY_SECONDS", 30))

# Arquivo onde os pares origem→destino ficam salvos
MAPPINGS_FILE = Path("mappings.json")

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# FILA DE MENSAGENS
message_queue: "asyncio.Queue[Tuple[Message, List[int]]]" = asyncio.Queue()


# ============================================================
# 🔧 Persistência dos links
# ============================================================

def load_links() -> Dict[str, List[int]]:
    if not MAPPINGS_FILE.exists():
        return {}

    try:
        with open(MAPPINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        links = {str(k): [int(x) for x in v] for k, v in data.get("links", {}).items()}
        return links

    except Exception as e:
        logger.error(f"Erro ao carregar mappings.json: {e}")
        return {}


def save_links(links: Dict[str, List[int]]) -> None:
    try:
        with open(MAPPINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({"links": links}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Erro ao salvar mappings.json: {e}")


# ============================================================
# 🔧 Worker — encaminhamento pela FILA, uma mensagem por vez
# ============================================================

async def forward_worker(app: Application):
    logger.info("Worker de encaminhamento iniciado...")

    while True:
        msg, targets = await message_queue.get()

        try:
            # delay global entre envios
            await asyncio.sleep(FORWARD_DELAY_SECONDS)

            for target in targets:

                for attempt in range(3):  # até 3 tentativas
                    try:
                        await app.bot.copy_message(
                            chat_id=target,
                            from_chat_id=msg.chat_id,
                            message_id=msg.message_id
                        )

                        logger.info(f"Mensagem enviada {msg.chat_id} -> {target}")
                        break

                    except RetryAfter as e:
                        wait = int(getattr(e, "retry_after", 5))
                        logger.warning(f"RetryAfter: aguardando {wait}s...")
                        await asyncio.sleep(wait)

                    except TimedOut:
                        logger.warning(f"Timeout ao enviar. Tentativa {attempt+1}/3")
                        if attempt == 2:
                            logger.error("Falhou após 3 timeouts.")
                        else:
                            await asyncio.sleep(5)

                    except NetworkError as e:
                        logger.warning(f"NetworkError: {e}")
                        await asyncio.sleep(5)

                    except Exception as e:
                        logger.error(f"Erro inesperado ao encaminhar: {e}")
                        break

        except Exception as e:
            logger.error(f"Erro no worker: {e}")

        finally:
            message_queue.task_done()


# ============================================================
# 🔧 Comandos do bot
# ============================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 BOT REPLICADOR ATIVO\n\n"
        "Comandos disponíveis:\n"
        "/add_link ORIGEM DESTINO\n"
        "/remove_link ORIGEM DESTINO\n"
        "/list_links\n"
        "/on\n"
        "/off\n"
        "/status\n"
        "/chatid\n"
        f"\nDelay atual: {FORWARD_DELAY_SECONDS} segundos"
    )


async def cmd_add_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        return await update.message.reply_text("Uso correto:\n/add_link ORIGEM DESTINO")

    try:
        src = int(context.args[0])
        dst = int(context.args[1])
    except:
        return await update.message.reply_text("IDs inválidos.")

    links = context.application.bot_data["links"]
    arr = links.get(str(src), [])

    if dst not in arr:
        arr.append(dst)

    links[str(src)] = arr
    save_links(links)

    await update.message.reply_text(f"Par adicionado:\n{src} → {dst}")


async def cmd_remove_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 2:
        return await update.message.reply_text("Uso:\n/remove_link ORIGEM DESTINO")

    try:
        src = int(context.args[0])
        dst = int(context.args[1])
    except:
        return await update.message.reply_text("IDs inválidos.")

    links = context.application.bot_data["links"]

    if str(src) in links and dst in links[str(src)]:
        links[str(src)].remove(dst)

        if not links[str(src)]:
            del links[str(src)]

        save_links(links)
        return await update.message.reply_text(f"Removido:\n{src} → {dst}")

    await update.message.reply_text("Par não encontrado.")


async def cmd_list_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    links = context.application.bot_data["links"]

    if not links:
        return await update.message.reply_text("Nenhum par cadastrado.")

    text = "🔗 PARES CADASTRADOS:\n\n"
    for src, arr in links.items():
        for dst in arr:
            text += f"{src} → {dst}\n"

    await update.message.reply_text(text)


async def cmd_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["forwarding"] = True
    await update.message.reply_text("Encaminhamento ATIVADO.")


async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["forwarding"] = False
    await update.message.reply_text("Encaminhamento DESATIVADO.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bd = context.application.bot_data
    await update.message.reply_text(
        "📊 STATUS DO BOT\n\n"
        f"Encaminhamento: {'ON' if bd['forwarding'] else 'OFF'}\n"
        f"Pares ativos: {sum(len(v) for v in bd['links'].values())}\n"
        f"Mensagens na fila: {message_queue.qsize()}\n"
        f"Delay: {FORWARD_DELAY_SECONDS}s"
    )


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"ID deste chat: {update.effective_chat.id}")


# ============================================================
# 🔧 Receber mensagens e jogar na fila
# ============================================================

async def collect_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bd = context.application.bot_data

    if not bd["forwarding"]:
        return

    msg = update.message
    src = str(msg.chat_id)

    if src not in bd["links"]:
        return

    targets = bd["links"][src]
    await message_queue.put((msg, targets))

    logger.info(
        f"Mensagem adicionada à fila: origem={msg.chat_id}, fila={message_queue.qsize()}"
    )


# ============================================================
# 🔧 MAIN — inicializa o bot
# ============================================================

def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.bot_data["links"] = load_links()
    app.bot_data["forwarding"] = False  # inicia desligado

    # Comandos
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add_link", cmd_add_link))
    app.add_handler(CommandHandler("remove_link", cmd_remove_link))
    app.add_handler(CommandHandler("list_links", cmd_list_links))
    app.add_handler(CommandHandler("on", cmd_on))
    app.add_handler(CommandHandler("off", cmd_off))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("chatid", cmd_chatid))

    # Captura todas as mensagens normais
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, collect_messages))

    # Inicia o worker da fila
    loop.create_task(forward_worker(app))

    logger.info("🤖 Bot replicador inicializado no Railway!")
    app.run_polling()


if __name__ == "__main__":
    main()
