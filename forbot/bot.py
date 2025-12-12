# bot.py — Bot replicador multi-origem/multi-destino para rodar LOCALMENTE
# Usa uma FILA + WORKER para encaminhar mensagens uma por vez,
# com delay global e tentativas automáticas em caso de erro.

import asyncio
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
    ContextTypes,
    MessageHandler,
    filters,
)

# ========= CONFIG BÁSICA =========
BOT_TOKEN = "8522495959:AAEsDCXOVmbK2okjVfaQGhZLDMBulN6ML7g"
FORWARD_DELAY_SECONDS = 10
MAPPINGS_FILE = Path("mappings.json")
# ================================

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN está vazio!")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Fila global: cada item é (Message, [destinos])
message_queue: "asyncio.Queue[Tuple[Message, List[int]]]" = asyncio.Queue()


# ---------- Persistência ----------
def load_links() -> Dict[str, List[int]]:
    if not MAPPINGS_FILE.exists():
        return {}
    try:
        with MAPPINGS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        links = {str(k): [int(x) for x in v] for k, v in data.get("links", {}).items()}
        return links
    except Exception as e:
        logger.error("Erro ao carregar mappings.json: %s", e)
        return {}


def save_links(links: Dict[str, List[int]]) -> None:
    try:
        with MAPPINGS_FILE.open("w", encoding="utf-8") as f:
            json.dump({"links": links}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("Erro ao salvar mappings.json: %s", e)


# ---------- WORKER: pega da fila e encaminha UMA por vez ----------
async def forward_worker(app: Application) -> None:
    logger.info("Worker de encaminhamento iniciado (fila global).")
    while True:
        try:
            msg, targets = await message_queue.get()
            try:
                # Delay global antes de cada mensagem encaminhada
                await asyncio.sleep(FORWARD_DELAY_SECONDS)

                for target_chat_id in targets:
                    for attempt in range(3):  # até 3 tentativas por destino
                        try:
                            await app.bot.copy_message(
                                chat_id=target_chat_id,
                                from_chat_id=msg.chat_id,
                                message_id=msg.message_id,
                            )
                            logger.info(
                                "Mensagem encaminhada: %s -> %s (msg_id=%s)",
                                msg.chat_id,
                                target_chat_id,
                                msg.message_id,
                            )
                            break  # sucesso, sai do loop de tentativas

                        except RetryAfter as e:
                            espera = int(getattr(e, "retry_after", 5)) + 1
                            logger.warning(
                                "RetryAfter %ss antes de encaminhar novamente %s -> %s",
                                espera, msg.chat_id, target_chat_id
                            )
                            await asyncio.sleep(espera)

                        except TimedOut:
                            logger.warning(
                                "TimedOut ao encaminhar %s -> %s (msg_id=%s), tentativa %s/3",
                                msg.chat_id,
                                target_chat_id,
                                msg.message_id,
                                attempt + 1,
                            )
                            if attempt == 2:
                                logger.error(
                                    "Falha definitiva após 3 timeouts %s -> %s (msg_id=%s)",
                                    msg.chat_id,
                                    target_chat_id,
                                    msg.message_id,
                                )
                            else:
                                await asyncio.sleep(5)

                        except NetworkError as e:
                            logger.warning(
                                "NetworkError ao encaminhar %s -> %s (msg_id=%s): %s",
                                msg.chat_id, target_chat_id, msg.message_id, e
                            )
                            await asyncio.sleep(5)

                        except Exception as e:
                            logger.error(
                                "Erro ao encaminhar %s -> %s (msg_id=%s): %s",
                                msg.chat_id,
                                target_chat_id,
                                msg.message_id,
                                e,
                            )
                            break

            finally:
                message_queue.task_done()

        except Exception as e:
            logger.error("Erro inesperado no worker de encaminhamento: %s", e)
            # Pequena pausa pra evitar loop de erro sem fim
            await asyncio.sleep(5)


# ---------- Comandos ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "BOT REPLICADOR MULTI-CANAIS/GRUPOS (com FILA)\n\n"
        "Comandos:\n"
        "/add_link ID_ORIGEM ID_DESTINO  -> cria link origem -> destino\n"
        "/remove_link ID_ORIGEM ID_DESTINO  -> remove link\n"
        "/list_links  -> lista links cadastrados\n"
        "/on  -> liga encaminhamento\n"
        "/off -> desliga encaminhamento\n"
        "/status -> mostra status\n"
        "/chatid -> mostra o ID deste chat\n"
        f"\nDelay entre mensagens: {FORWARD_DELAY_SECONDS} segundos (fila global).\n"
        "Obs.: se entrar 100 mensagens, elas vão sair uma por vez, respeitando o delay."
    )
    await update.effective_message.reply_text(text)


async def cmd_add_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.effective_message.reply_text("Uso: /add_link ID_ORIGEM ID_DESTINO")
        return
    try:
        src = int(context.args[0])
        dst = int(context.args[1])
    except:
        await update.effective_message.reply_text("IDs inválidos.")
        return

    links = context.application.bot_data.get("links", {})
    arr = links.get(str(src), [])
    if dst not in arr:
        arr.append(dst)
    links[str(src)] = arr

    save_links(links)
    context.application.bot_data["links"] = links

    await update.effective_message.reply_text(f"Link criado: {src} -> {dst}")


async def cmd_remove_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.effective_message.reply_text("Uso: /remove_link ID_ORIGEM ID_DESTINO")
        return
    try:
        src = int(context.args[0])
        dst = int(context.args[1])
    except:
        await update.effective_message.reply_text("IDs inválidos.")
        return

    links = context.application.bot_data.get("links", {})
    if str(src) not in links:
        await update.effective_message.reply_text("Origem não cadastrada.")
        return

    if dst in links[str(src)]:
        links[str(src)].remove(dst)
        if not links[str(src)]:
            del links[str(src)]

        save_links(links)
        context.application.bot_data["links"] = links

        await update.effective_message.reply_text(f"Link removido: {src} -> {dst}")
    else:
        await update.effective_message.reply_text("Esse link não existe.")


async def cmd_list_links(update: Update, context: ContextTypes.DEFAULT_TYPE):
    links = context.application.bot_data.get("links", {})
    if not links:
        await update.effective_message.reply_text("Nenhum link cadastrado.")
        return

    lines = ["Links cadastrados:"]
    for src, arr in links.items():
        for dst in arr:
            lines.append(f"{src} -> {dst}")

    await update.effective_message.reply_text("\n".join(lines))


async def cmd_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["forwarding_enabled"] = True
    await update.effective_message.reply_text("Encaminhamento LIGADO.")


async def cmd_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["forwarding_enabled"] = False
    await update.effective_message.reply_text("Encaminhamento DESLIGADO.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bd = context.application.bot_data
    fwd = bd.get("forwarding_enabled", False)
    links = bd.get("links", {})
    total = sum(len(v) for v in links.values())
    fila = message_queue.qsize()
    text = (
        "STATUS DO BOT\n\n"
        f"Encaminhamento: {'LIGADO' if fwd else 'DESLIGADO'}\n"
        f"Pares cadastrados: {total}\n"
        f"Mensagens na fila: {fila}\n"
        f"Delay entre envios: {FORWARD_DELAY_SECONDS} segundos\n"
    )
    await update.effective_message.reply_text(text)


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"ID deste chat: {update.effective_chat.id}")


# ---------- Coleta de mensagens ----------
async def collect_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bd = context.application.bot_data
    if not bd.get("forwarding_enabled", False):
        return

    msg = update.effective_message
    if msg is None:
        return

    src = str(msg.chat_id)
    links = bd.get("links", {})

    if src not in links:
        return

    targets = links[src]

    await message_queue.put((msg, targets))
    logger.info(
        "Mensagem adicionada à fila: origem=%s msg_id=%s destinos=%s (tamanho fila=%s)",
        msg.chat_id, msg.message_id, targets, message_queue.qsize()
    )


# ---------- Main ----------
def main():
    # Python 3.14: criar e registrar loop manualmente
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    links = load_links()
    app.bot_data["links"] = links
    app.bot_data["forwarding_enabled"] = False

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add_link", cmd_add_link))
    app.add_handler(CommandHandler("remove_link", cmd_remove_link))
    app.add_handler(CommandHandler("list_links", cmd_list_links))
    app.add_handler(CommandHandler("on", cmd_on))
    app.add_handler(CommandHandler("off", cmd_off))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("chatid", cmd_chatid))

    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, collect_messages))

    # Inicia o worker da fila no mesmo loop
    loop.create_task(forward_worker(app))

    logger.info("Bot replicador iniciado com FILA global!")
    app.run_polling()


if __name__ == "__main__":
    main()
