import json
import os
import sys
from pathlib import Path

import psycopg
import telebot
from dotenv import load_dotenv
from telebot import util
from telebot.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

load_dotenv(sys.argv[1] if len(sys.argv) > 1 else ".env")

DATABASE_URL = os.environ.get("DATABASE_URL", "")

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_CHAT_ID = int(os.environ["OWNER_CHAT_ID"])
START_TEXT = os.environ.get(
    "START_TEXT",
    "Доброго дня! Залиште будь ласка вашу заявку.",
)
WELCOME_TEXT = os.environ.get(
    "WELCOME_TEXT",
    "Дякуємо за повідомлення! Ми відповімо якнайшвидше.",
)
FINISH_TEXT = os.environ.get(
    "FINISH_TEXT",
    "Дякуємо за звернення! Якщо буде потрібно — просто напишіть знову.",
)
SITE_URL = os.environ.get(
    "SITE_URL", "https://vitalik19970527-prog.github.io/CatMan-Pro/"
)

START_LABEL = "Розпочати чат"
FINISH_LABEL = "Завершити"
CLEAR_LABEL = "Очистити чат"
SITE_LABEL = "Перейти на сайт"
SPECIAL_LABELS = (START_LABEL, FINISH_LABEL, CLEAR_LABEL, SITE_LABEL)

BOT_NAME = os.environ.get("BOT_NAME", "bot")
MAP_FILE = Path(__file__).parent / f"map_{BOT_NAME}.json"
STATE_FILE = Path(__file__).parent / f"state_{BOT_NAME}.json"
HISTORY_FILE = Path(__file__).parent / f"history_{BOT_NAME}.json"

bot = telebot.TeleBot(BOT_TOKEN)

site_button = KeyboardButton(SITE_LABEL)

visitor_markup = ReplyKeyboardMarkup(resize_keyboard=True)
visitor_markup.row(KeyboardButton(START_LABEL), KeyboardButton(FINISH_LABEL))
visitor_markup.row(KeyboardButton(CLEAR_LABEL), site_button)

owner_markup = ReplyKeyboardMarkup(resize_keyboard=True)
owner_markup.row(KeyboardButton(CLEAR_LABEL), site_button)


def markup_for(chat_id) -> ReplyKeyboardMarkup:
    return owner_markup if chat_id == OWNER_CHAT_ID else visitor_markup


def load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


reply_map = load_json(MAP_FILE)
session_state = load_json(STATE_FILE)
history = load_json(HISTORY_FILE)


def log_message(chat_id, message_id) -> None:
    history.setdefault(str(chat_id), []).append(message_id)
    save_json(HISTORY_FILE, history)


def log_conversation(tg_user_id, tg_username, tg_full_name, direction, text) -> None:
    """Mirror a real visitor/owner message into public.bot_conversations so the
    Superadmin dashboard / CatMan SupAdm can show and reply to it. Never lets a
    DB hiccup break the Telegram flow — same tolerance as handle_clear's delete."""
    if not DATABASE_URL:
        return
    try:
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.bot_conversations
                    (bot_name, tg_user_id, tg_username, tg_full_name, direction, message_text)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (BOT_NAME, tg_user_id, tg_username, tg_full_name, direction, text),
            )
    except Exception:
        pass


def send(chat_id, text, **kwargs):
    msg = bot.send_message(chat_id, text, **kwargs)
    log_message(chat_id, msg.message_id)
    return msg


@bot.message_handler(func=lambda m: True, content_types=util.content_type_media)
def log_all_messages(message):
    log_message(message.chat.id, message.message_id)
    return telebot.ContinueHandling()


@bot.message_handler(commands=["start"])
def handle_start(message):
    session_state[str(message.from_user.id)] = "greeted"
    save_json(STATE_FILE, session_state)
    send(message.chat.id, START_TEXT, reply_markup=markup_for(message.chat.id))


@bot.message_handler(
    func=lambda m: m.chat.id != OWNER_CHAT_ID and m.text == START_LABEL,
    content_types=["text"],
)
def handle_visitor_start_button(message):
    session_state[str(message.from_user.id)] = "greeted"
    save_json(STATE_FILE, session_state)
    send(message.chat.id, START_TEXT, reply_markup=visitor_markup)


@bot.message_handler(
    func=lambda m: m.chat.id != OWNER_CHAT_ID and m.text == FINISH_LABEL,
    content_types=["text"],
)
def handle_visitor_finish_button(message):
    session_state[str(message.from_user.id)] = "finished"
    save_json(STATE_FILE, session_state)
    send(message.chat.id, FINISH_TEXT, reply_markup=visitor_markup)


@bot.message_handler(func=lambda m: m.text == SITE_LABEL, content_types=["text"])
def handle_site_button(message):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("Відкрити у браузері", url=SITE_URL))
    send(message.chat.id, "Натисніть кнопку нижче:", reply_markup=markup)


@bot.message_handler(func=lambda m: m.text == CLEAR_LABEL, content_types=["text"])
def handle_clear(message):
    chat_id = message.chat.id
    ids = history.pop(str(chat_id), [])
    save_json(HISTORY_FILE, history)
    for mid in ids:
        try:
            bot.delete_message(chat_id, mid)
        except Exception:
            pass
    if chat_id != OWNER_CHAT_ID:
        session_state[str(message.from_user.id)] = "new"
        save_json(STATE_FILE, session_state)


@bot.message_handler(
    func=lambda m: m.chat.id == OWNER_CHAT_ID and m.reply_to_message is not None
)
def handle_owner_reply(message):
    target_id = reply_map.get(str(message.reply_to_message.message_id))
    if target_id is None:
        send(
            message.chat.id,
            "Не бачу, кому це переслати — напишіть людині напряму в Telegram.",
        )
        return
    reply_text = message.text or message.caption or ""
    send(target_id, reply_text, reply_markup=visitor_markup)
    log_conversation(target_id, None, None, "out", reply_text)
    send(message.chat.id, "Надіслано.")


@bot.message_handler(
    func=lambda m: m.chat.id != OWNER_CHAT_ID and m.text not in SPECIAL_LABELS,
    content_types=["text"],
)
def handle_visitor_message(message):
    sender = message.from_user
    state = session_state.get(str(sender.id), "new")

    if state in ("new", "finished"):
        session_state[str(sender.id)] = "greeted"
        save_json(STATE_FILE, session_state)
        send(message.chat.id, START_TEXT, reply_markup=visitor_markup)
        return

    name = " ".join(filter(None, [sender.first_name, sender.last_name]))
    username = f"@{sender.username}" if sender.username else "без username"
    forwarded = send(
        OWNER_CHAT_ID,
        f"Від: {name} ({username}, id {sender.id})\n\n{message.text}",
        reply_markup=owner_markup,
    )
    reply_map[str(forwarded.message_id)] = sender.id
    save_json(MAP_FILE, reply_map)
    log_conversation(sender.id, sender.username, name, "in", message.text)

    if state == "greeted":
        send(message.chat.id, WELCOME_TEXT, reply_markup=visitor_markup)
        session_state[str(sender.id)] = "active"
        save_json(STATE_FILE, session_state)


bot.infinity_polling()
