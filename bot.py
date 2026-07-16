# -*- coding: utf-8 -*-

import asyncio
import html
import logging
import os
import re
import uuid
from dotenv import load_dotenv

load_dotenv()
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

# ================= НАСТРОЙКИ =================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Группа модерации заявок водителей: VIP_driver_reg
MODERATION_CHAT_ID = int(os.getenv("MODERATION_CHAT_ID", "-5062249297"))

# Закрытая группа заказов: Vip_taxidriver
ORDERS_CHAT_ID = int(os.getenv("ORDERS_CHAT_ID", "-1003446115764"))

PERSISTENCE_PATH = os.getenv("PERSISTENCE_PATH", "bot_state.pickle")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ================= СОСТОЯНИЯ =================

(
    ORDER_NAME,
    ORDER_FROM,
    ORDER_TO,
    ORDER_TIME,
    ORDER_CLASS,
    ORDER_TARIFF,
    ORDER_PRICE,
    ORDER_COMMENT,
    ORDER_CONFIRM,
) = range(9)

(
    REG_NAME,
    REG_PHONE,
    REG_CAR,
    REG_YEAR,
    REG_PLATE,
    REG_CLASS,
    REG_PHOTOS,
    REG_CONFIRM,
) = range(20, 28)

CAR_CLASSES = {"Business", "First", "Минивэн"}
ORDER_CLASSES = CAR_CLASSES | {"Неважно"}

# ================= КЛАВИАТУРЫ =================

MAIN_KB = ReplyKeyboardMarkup(
    [
        ["🚖 Заказать поездку"],
        ["👨‍✈️ Стать водителем", "📋 Мой статус"],
    ],
    resize_keyboard=True,
)

LOCATION_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📍 Отправить геопозицию", request_location=True)],
        ["✍️ Ввести адрес"],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

TIME_KB = ReplyKeyboardMarkup(
    [["Сейчас"], ["Указать дату и время"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

TARIFF_KB = ReplyKeyboardMarkup(
    [["Разовая поездка"], ["Почасовая"], ["Аэропорт"], ["Бизнес-день"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

PRICE_KB = ReplyKeyboardMarkup(
    [["По договоренности"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


CLASS_KB = ReplyKeyboardMarkup(
    [["Business", "First"], ["Минивэн", "Неважно"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

DRIVER_CLASS_KB = ReplyKeyboardMarkup(
    [["Business", "First"], ["Минивэн"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

PHONE_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📱 Отправить мой номер", request_contact=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

DONE_KB = ReplyKeyboardMarkup(
    [["Готово"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

SKIP_KB = ReplyKeyboardMarkup(
    [["Пропустить"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

# ================= УТИЛИТЫ =================


def clean_text(text: str, max_length: int = 400) -> str:
    value = " ".join((text or "").strip().split())
    return value[:max_length]


def normalize_phone(text: str) -> Optional[str]:
    digits = re.sub(r"\D", "", text or "")

    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits

    if not 10 <= len(digits) <= 15:
        return None
    if len(set(digits)) == 1:
        return None

    return "+" + digits


def esc(value: object) -> str:
    return html.escape(str(value or "—"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_moscow() -> datetime:
    return datetime.now(timezone(timedelta(hours=3)))


def format_dt(value: datetime) -> str:
    return value.strftime("%d.%m.%Y %H:%M")


def current_time_label() -> str:
    return f"Сейчас — {format_dt(now_moscow())}"


MONTHS_RU = {
    "января": 1, "январь": 1,
    "февраля": 2, "февраль": 2,
    "марта": 3, "март": 3,
    "апреля": 4, "апрель": 4,
    "мая": 5, "май": 5,
    "июня": 6, "июнь": 6,
    "июля": 7, "июль": 7,
    "августа": 8, "август": 8,
    "сентября": 9, "сентябрь": 9,
    "октября": 10, "октябрь": 10,
    "ноября": 11, "ноябрь": 11,
    "декабря": 12, "декабрь": 12,
}


def parse_order_time_input(text: str) -> tuple[str, str, str]:
    """Возвращает display, iso, expire_iso. Заказ удаляется через 30 минут после времени подачи."""
    raw = clean_text(text, 120)
    low = raw.lower().replace(",", " ").replace(" в ", " ")
    now = now_moscow()

    if low == "сейчас":
        scheduled = now
        display = current_time_label()
        expire = scheduled + timedelta(minutes=30)
        return display, scheduled.isoformat(), expire.isoformat()

    if low == "сегодня":
        display = "Сегодня — уточнить время"
        scheduled = now
        expire = scheduled + timedelta(minutes=30)
        return display, scheduled.isoformat(), expire.isoformat()

    if low == "завтра":
        d = now + timedelta(days=1)
        scheduled = d.replace(hour=0, minute=0, second=0, microsecond=0)
        display = scheduled.strftime("%d.%m.%Y — уточнить время")
        expire = scheduled + timedelta(hours=23, minutes=59)
        return display, scheduled.isoformat(), expire.isoformat()

    time_match = re.search(r"(\d{1,2})[:.](\d{2})", low)
    hour = int(time_match.group(1)) if time_match else 0
    minute = int(time_match.group(2)) if time_match else 0
    if hour > 23 or minute > 59:
        raise ValueError("bad time")

    date = None
    if "сегодня" in low:
        date = now.date()
    elif "завтра" in low:
        date = (now + timedelta(days=1)).date()
    else:
        m = re.search(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?", low)
        if m:
            day, month = int(m.group(1)), int(m.group(2))
            year = int(m.group(3)) if m.group(3) else now.year
            if year < 100:
                year += 2000
            date = datetime(year, month, day, tzinfo=now.tzinfo).date()
        else:
            for month_word, month_num in MONTHS_RU.items():
                m2 = re.search(rf"(\d{{1,2}})\s+{month_word}(?:\s+(\d{{4}}))?", low)
                if m2:
                    day = int(m2.group(1))
                    year = int(m2.group(2)) if m2.group(2) else now.year
                    date = datetime(year, month_num, day, tzinfo=now.tzinfo).date()
                    break

    if date is None:
        if not time_match:
            raise ValueError("no date time")
        # Если клиент написал только 16:00 — считаем сегодняшним временем, если оно ещё не прошло, иначе завтра.
        date = now.date()

    scheduled = datetime(date.year, date.month, date.day, hour, minute, tzinfo=now.tzinfo)
    if date == now.date() and time_match and scheduled < now:
        scheduled = scheduled + timedelta(days=1)
    if scheduled.date() < now.date():
        scheduled = scheduled.replace(year=scheduled.year + 1)

    display = format_dt(scheduled)
    expire = scheduled + timedelta(minutes=30)
    return display, scheduled.isoformat(), expire.isoformat()


def parse_iso_dt(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def yandex_link(latitude: float, longitude: float) -> str:
    return (
        "https://yandex.ru/maps/"
        f"?pt={longitude:.6f},{latitude:.6f}&z=16&l=map"
    )


def place_from_message(message) -> Optional[str]:
    if message.location:
        return yandex_link(message.location.latitude, message.location.longitude)

    text = clean_text(message.text)
    if text == "✍️ Ввести адрес":
        return None
    if len(text) < 3:
        return None
    return text


def ensure_storage(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.bot_data.setdefault("drivers", {})
    context.bot_data.setdefault("driver_apps", {})
    context.bot_data.setdefault("orders", {})
    context.bot_data.setdefault("active_by_user", {})
    context.bot_data.setdefault("pending_by_client", {})


def add_tracked(container: dict, chat_id: int, message_id: int) -> None:
    tracked = container.setdefault("tracked_messages", [])
    item = (int(chat_id), int(message_id))
    if item not in tracked:
        tracked.append(item)


def track_incoming_draft(context: ContextTypes.DEFAULT_TYPE, message) -> None:
    draft = context.user_data.get("order")
    if draft and message:
        add_tracked(draft, message.chat_id, message.message_id)


async def reply_and_track(
    context: ContextTypes.DEFAULT_TYPE,
    message,
    text: str,
    **kwargs,
):
    sent = await message.reply_text(text, **kwargs)
    draft = context.user_data.get("order")
    if draft:
        add_tracked(draft, sent.chat_id, sent.message_id)
    return sent


def contact_data_detected(text: str) -> bool:
    value = text or ""
    phone_pattern = r"(?<!\d)(?:\+?\d[\s().-]*){10,15}(?!\d)"
    username_pattern = r"(?<!\w)@[A-Za-z0-9_]{5,}"
    link_pattern = r"(?:https?://)?(?:t\.me|telegram\.me)/\S+"
    return bool(
        re.search(phone_pattern, value)
        or re.search(username_pattern, value)
        or re.search(link_pattern, value, flags=re.IGNORECASE)
    )


async def is_chat_admin(bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in {"administrator", "creator"}
    except TelegramError:
        logger.exception("Не удалось проверить права пользователя %s", user_id)
        return False


def driver_record(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> Optional[dict]:
    ensure_storage(context)
    return context.bot_data["drivers"].get(str(user_id))


def order_public_text(order_id: str, order: dict) -> str:
    return (
        f"🚖 <b>НОВЫЙ ЗАКАЗ №{esc(order_id)}</b>\n\n"
        f"📍 <b>Откуда:</b> {esc(order['from'])}\n"
        f"🏁 <b>Куда:</b> {esc(order['to'])}\n"
        f"🕒 <b>Когда:</b> {esc(order['time'])}\n"
        f"⏳ <b>Удалится:</b> {esc(order.get('expires_at_display', '—'))}\n"
        f"🗓 <b>Создан:</b> {esc(order.get('created_at_display', '—'))}\n"
        f"🚘 <b>Класс:</b> {esc(order['car_class'])}\n"
        f"💳 <b>Тариф:</b> {esc(order.get('tariff', '—'))}\n"
        f"💰 <b>Цена:</b> {esc(order.get('price', '—'))}\n"
        f"💬 <b>Комментарий:</b> {esc(order['comment'])}\n\n"
        "Личные данные клиента скрыты."
    )


def driver_private_order_text(order_id: str, order: dict) -> str:
    status_lines = []
    if order.get("arrived_at_display"):
        status_lines.append(f"📍 <b>На месте:</b> {esc(order['arrived_at_display'])}")
    if order.get("started_at_display"):
        status_lines.append(f"▶️ <b>Начало поездки:</b> {esc(order['started_at_display'])}")
    if order.get("completed_at_display"):
        status_lines.append(f"🏁 <b>Окончание:</b> {esc(order['completed_at_display'])}")
    status_block = "\n" + "\n".join(status_lines) if status_lines else ""

    return (
        f"✅ <b>Вы взяли заказ №{esc(order_id)}</b>\n\n"
        f"📍 <b>Откуда:</b> {esc(order['from'])}\n"
        f"🏁 <b>Куда:</b> {esc(order['to'])}\n"
        f"🕒 <b>Когда:</b> {esc(order['time'])}\n"
        f"🗓 <b>Создан:</b> {esc(order.get('created_at_display', '—'))}\n"
        f"🚘 <b>Класс:</b> {esc(order['car_class'])}\n"
        f"💳 <b>Тариф:</b> {esc(order.get('tariff', '—'))}\n"
        f"💰 <b>Цена:</b> {esc(order.get('price', '—'))}\n"
        f"💬 <b>Комментарий:</b> {esc(order['comment'])}"
        f"{status_block}\n\n"
        "Пишите клиенту прямо в этом чате. Контакты сторон скрыты."
    )


def order_summary(order: dict) -> str:
    return (
        "Проверьте заказ:\n\n"
        f"Имя: {order['name']}\n"
        f"Откуда: {order['from']}\n"
        f"Куда: {order['to']}\n"
        f"Когда: {order['time']}\n"
        f"Создан: {order.get('created_at_display', '—')}\n"
        f"Класс: {order['car_class']}\n"
        f"Тариф: {order.get('tariff', '—')}\n"
        f"Цена: {order.get('price', '—')}\n"
        f"Комментарий: {order['comment']}"
    )


def registration_summary(reg: dict) -> str:
    return (
        "Проверьте анкету:\n\n"
        f"ФИО: {reg['name']}\n"
        f"Телефон: {reg['phone']}\n"
        f"Автомобиль: {reg['car']}\n"
        f"Год: {reg['year']}\n"
        f"Госномер: {reg['plate']}\n"
        f"Класс: {reg['car_class']}\n"
        f"Фото: {len(reg['photos'])}"
    )

# ================= ОБЩИЕ КОМАНДЫ =================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)

    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "Заказы и регистрация оформляются в личном чате с ботом.\n"
            "ID этого чата: /chatid"
        )
        return

    active_order_id = context.bot_data["active_by_user"].get(str(update.effective_user.id))
    if active_order_id:
        await update.effective_message.reply_text(
            f"У вас активный заказ №{active_order_id}.\n"
            "Пишите сообщения сюда — бот передаст их второй стороне.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await update.effective_message.reply_text(
        "🚖 VIP Taxi\n\nВыберите действие:",
        reply_markup=MAIN_KB,
    )


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"ID этого чата: {update.effective_chat.id}")


async def my_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    user_id = update.effective_user.id
    driver = driver_record(context, user_id)

    if driver and driver.get("status") == "approved":
        await update.effective_message.reply_text(
            "✅ Вы зарегистрированы как водитель.\n"
            f"Класс: {driver['car_class']}\n"
            f"Автомобиль: {driver['car']}\n"
            f"Госномер: {driver['plate']}"
        )
        return

    pending = next(
        (
            app
            for app in context.bot_data["driver_apps"].values()
            if app.get("user_id") == user_id and app.get("status") == "pending"
        ),
        None,
    )

    if pending:
        await update.effective_message.reply_text(
            "⏳ Ваша анкета находится на проверке модератора."
        )
    else:
        await update.effective_message.reply_text(
            "Вы пока не зарегистрированы как водитель.\n"
            "Нажмите «👨‍✈️ Стать водителем»."
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("order", None)
    context.user_data.pop("reg", None)
    await update.effective_message.reply_text(
        "Действие отменено.",
        reply_markup=MAIN_KB,
    )
    return ConversationHandler.END

# ================= ЗАКАЗ КЛИЕНТА =================


async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    user_id = update.effective_user.id

    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "Откройте личный чат с ботом и отправьте /start."
        )
        return ConversationHandler.END

    if context.bot_data["active_by_user"].get(str(user_id)):
        await update.effective_message.reply_text(
            "Сначала завершите текущий заказ."
        )
        return ConversationHandler.END

    pending_order_id = context.bot_data["pending_by_client"].get(str(user_id))
    if pending_order_id:
        order = context.bot_data["orders"].get(pending_order_id)
        if order and order.get("status") == "open":
            await update.effective_message.reply_text(
                f"У вас уже есть заказ №{pending_order_id}, который ожидает водителя."
            )
            return ConversationHandler.END
        context.bot_data["pending_by_client"].pop(str(user_id), None)

    context.user_data["order"] = {
        "tracked_messages": [],
        "created_at": now_iso(),
        "created_at_display": format_dt(now_moscow()),
    }
    track_incoming_draft(context, update.effective_message)

    await reply_and_track(
        context,
        update.effective_message,
        "Как к вам обращаться?",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ORDER_NAME


async def order_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_incoming_draft(context, update.effective_message)
    name = clean_text(update.effective_message.text, 100)

    if len(name) < 2:
        await reply_and_track(context, update.effective_message, "Введите имя ещё раз.")
        return ORDER_NAME

    context.user_data["order"]["name"] = name
    await reply_and_track(
        context,
        update.effective_message,
        "Укажите точку подачи. Можно:\n"
        "• написать адрес;\n"
        "• вставить ссылку из Яндекс Карт;\n"
        "• отправить геопозицию Telegram.",
        reply_markup=LOCATION_KB,
    )
    return ORDER_FROM


async def order_from(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_incoming_draft(context, update.effective_message)
    place = place_from_message(update.effective_message)

    if update.effective_message.text == "✍️ Ввести адрес":
        await reply_and_track(
            context,
            update.effective_message,
            "Напишите адрес подачи или вставьте ссылку из Яндекс Карт:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ORDER_FROM

    if not place:
        await reply_and_track(
            context,
            update.effective_message,
            "Не удалось определить точку. Напишите адрес, вставьте ссылку Яндекс Карт "
            "или отправьте геопозицию.",
            reply_markup=LOCATION_KB,
        )
        return ORDER_FROM

    context.user_data["order"]["from"] = place
    await reply_and_track(
        context,
        update.effective_message,
        "Куда нужно ехать? Напишите адрес, вставьте ссылку Яндекс Карт "
        "или отправьте точку через Telegram.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ORDER_TO


async def order_to(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_incoming_draft(context, update.effective_message)
    place = place_from_message(update.effective_message)

    if not place:
        await reply_and_track(
            context,
            update.effective_message,
            "Не удалось определить пункт назначения. Попробуйте ещё раз."
        )
        return ORDER_TO

    context.user_data["order"]["to"] = place
    await reply_and_track(
        context,
        update.effective_message,
        "Когда нужна машина?",
        reply_markup=TIME_KB,
    )
    return ORDER_TIME


async def order_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_incoming_draft(context, update.effective_message)
    raw_when = clean_text(update.effective_message.text, 120)

    if raw_when == "Указать дату и время":
        await reply_and_track(
            context,
            update.effective_message,
            "Напишите дату и время. Например: сегодня в 19:30, завтра в 16:00, 10 июля в 08:00 или 10.07.2026 08:00",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ORDER_TIME

    try:
        display, scheduled_iso, expire_iso = parse_order_time_input(raw_when)
    except Exception:
        await reply_and_track(
            context,
            update.effective_message,
            "Не понял дату и время. Пример: сейчас, завтра в 16:00, 10 июля в 08:00 или 10.07.2026 08:00.",
        )
        return ORDER_TIME

    context.user_data["order"]["time"] = display
    context.user_data["order"]["scheduled_at"] = scheduled_iso
    context.user_data["order"]["expires_at"] = expire_iso
    context.user_data["order"]["expires_at_display"] = format_dt(parse_iso_dt(expire_iso)) if parse_iso_dt(expire_iso) else "—"

    await reply_and_track(
        context,
        update.effective_message,
        "Выберите класс автомобиля:",
        reply_markup=CLASS_KB,
    )
    return ORDER_CLASS


async def order_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_incoming_draft(context, update.effective_message)
    car_class = clean_text(update.effective_message.text, 50)

    if car_class not in ORDER_CLASSES:
        await reply_and_track(
            context,
            update.effective_message,
            "Выберите класс на клавиатуре.",
            reply_markup=CLASS_KB,
        )
        return ORDER_CLASS

    context.user_data["order"]["car_class"] = car_class
    await reply_and_track(
        context,
        update.effective_message,
        "Выберите тариф:",
        reply_markup=TARIFF_KB,
    )
    return ORDER_TARIFF


async def order_tariff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_incoming_draft(context, update.effective_message)
    tariff = clean_text(update.effective_message.text, 80)
    allowed = {"Разовая поездка", "Почасовая", "Аэропорт", "Бизнес-день"}

    if tariff not in allowed:
        await reply_and_track(
            context,
            update.effective_message,
            "Выберите тариф на клавиатуре.",
            reply_markup=TARIFF_KB,
        )
        return ORDER_TARIFF

    context.user_data["order"]["tariff"] = tariff
    await reply_and_track(
        context,
        update.effective_message,
        "Укажите цену или бюджет. Например: 10000 ₽, 30000 ₽ день или нажмите «По договоренности».",
        reply_markup=PRICE_KB,
    )
    return ORDER_PRICE


async def order_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_incoming_draft(context, update.effective_message)
    price = clean_text(update.effective_message.text, 80)

    if not price:
        await reply_and_track(
            context,
            update.effective_message,
            "Укажите цену или нажмите «По договоренности».",
            reply_markup=PRICE_KB,
        )
        return ORDER_PRICE

    context.user_data["order"]["price"] = price
    await reply_and_track(
        context,
        update.effective_message,
        "Добавьте комментарий: номер рейса, детское кресло, количество пассажиров и т. п.\n\n"
        "Либо нажмите «Пропустить».",
        reply_markup=SKIP_KB,
    )
    return ORDER_COMMENT


async def order_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_incoming_draft(context, update.effective_message)
    comment = clean_text(update.effective_message.text)

    if comment.lower() == "пропустить":
        comment = "—"
    elif not comment:
        await reply_and_track(
            context,
            update.effective_message,
            "Введите комментарий или нажмите «Пропустить».",
        )
        return ORDER_COMMENT

    order = context.user_data["order"]
    order["comment"] = comment

    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Отправить заказ", callback_data="order_send"),
            InlineKeyboardButton("❌ Отмена", callback_data="order_cancel"),
        ]]
    )

    await reply_and_track(
        context,
        update.effective_message,
        order_summary(order) + "\n\nОтправить заказ водителям?",
        reply_markup=kb,
    )
    return ORDER_CONFIRM


async def order_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    query = update.callback_query
    await query.answer()

    draft = context.user_data.get("order")
    if draft:
        add_tracked(draft, query.message.chat_id, query.message.message_id)

    if query.data == "order_cancel":
        context.user_data.pop("order", None)
        await query.edit_message_text("Заказ отменён.")
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="Выберите действие:",
            reply_markup=MAIN_KB,
        )
        return ConversationHandler.END

    if not draft:
        await query.edit_message_text("Данные заказа не найдены. Начните заново: /start")
        return ConversationHandler.END

    order_id = uuid.uuid4().hex[:8].upper()
    order = {
        **draft,
        "client_id": query.from_user.id,
        "client_username": query.from_user.username or "",
        "status": "open",
        "driver_id": None,
        "created_at": now_iso(),
        "created_at_display": format_dt(now_moscow()),
    }

    take_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🟢 Взять заказ", callback_data=f"take_{order_id}")]]
    )

    try:
        sent = await context.bot.send_message(
            chat_id=ORDERS_CHAT_ID,
            text=order_public_text(order_id, order),
            parse_mode="HTML",
            reply_markup=take_kb,
            disable_web_page_preview=False,
        )
    except TelegramError:
        logger.exception("Не удалось отправить заказ в группу заказов")
        await query.edit_message_text(
            "Не удалось передать заказ водителям. Попробуйте позже."
        )
        return ConversationHandler.END

    order["group_message_id"] = sent.message_id
    context.bot_data["orders"][order_id] = order
    context.bot_data["pending_by_client"][str(query.from_user.id)] = order_id
    schedule_order_expiration(context, order_id, order)
    context.user_data.pop("order", None)

    cancel_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel_open_{order_id}")]]
    )
    client_message = await query.edit_message_text(
        f"✅ Заказ №{order_id} отправлен водителям.\n"
        "Сообщим, когда водитель возьмёт заказ.",
        reply_markup=cancel_kb,
    )
    add_tracked(order, client_message.chat_id, client_message.message_id)

    return ConversationHandler.END


def schedule_order_expiration(context: ContextTypes.DEFAULT_TYPE, order_id: str, order: dict) -> None:
    expires_dt = parse_iso_dt(order.get("expires_at", ""))
    if not expires_dt:
        return
    delay = max(1, int((expires_dt - now_moscow()).total_seconds()))
    task = asyncio.create_task(expire_order_later(context.application, order_id, delay))
    order["expiration_task_created"] = True


async def expire_order_later(application, order_id: str, delay: int) -> None:
    await asyncio.sleep(delay)
    context_data = application.bot_data
    order = context_data.get("orders", {}).get(order_id)
    if not order or order.get("status") != "open":
        return

    order["status"] = "expired"
    try:
        await application.bot.delete_message(ORDERS_CHAT_ID, order["group_message_id"])
    except TelegramError:
        logger.info("Не удалось удалить просроченный заказ %s из группы", order_id)

    context_data.get("pending_by_client", {}).pop(str(order.get("client_id")), None)
    context_data.get("orders", {}).pop(order_id, None)

    try:
        await application.bot.send_message(
            order["client_id"],
            f"⏱ Заказ №{order_id} удалён из группы: водитель не взял его до {order.get('expires_at_display', 'истечения времени')}.\n"
            "Можно создать новый заказ.",
            reply_markup=MAIN_KB,
        )
    except TelegramError:
        logger.info("Не удалось уведомить клиента о просрочке заказа %s", order_id)


async def cancel_open_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    query = update.callback_query
    order_id = query.data.removeprefix("cancel_open_")
    order = context.bot_data["orders"].get(order_id)

    if not order or order.get("client_id") != query.from_user.id:
        await query.answer("Заказ не найден.", show_alert=True)
        return

    if order.get("status") != "open":
        await query.answer("Заказ уже принят водителем.", show_alert=True)
        return

    try:
        await context.bot.delete_message(ORDERS_CHAT_ID, order["group_message_id"])
    except TelegramError:
        logger.exception("Не удалось удалить отменённый заказ из группы")

    context.bot_data["pending_by_client"].pop(str(order["client_id"]), None)
    context.bot_data["orders"].pop(order_id, None)
    await query.answer("Заказ отменён")
    await query.edit_message_text(f"Заказ №{order_id} отменён.")

# ================= РЕГИСТРАЦИЯ ВОДИТЕЛЯ =================


async def reg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    user_id = update.effective_user.id

    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "Регистрация доступна только в личном чате с ботом."
        )
        return ConversationHandler.END

    existing = driver_record(context, user_id)
    if existing and existing.get("status") == "approved":
        await update.effective_message.reply_text(
            "Вы уже зарегистрированы и одобрены как водитель."
        )
        return ConversationHandler.END

    pending = next(
        (
            app
            for app in context.bot_data["driver_apps"].values()
            if app.get("user_id") == user_id and app.get("status") == "pending"
        ),
        None,
    )
    if pending:
        await update.effective_message.reply_text(
            "Ваша анкета уже находится на проверке."
        )
        return ConversationHandler.END

    context.user_data["reg"] = {"photos": []}
    await update.effective_message.reply_text(
        "Введите ФИО полностью:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return REG_NAME


async def reg_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = clean_text(update.effective_message.text, 120)
    if len(name.split()) < 2:
        await update.effective_message.reply_text(
            "Введите имя и фамилию. Например: Иванов Иван."
        )
        return REG_NAME

    context.user_data["reg"]["name"] = name
    await update.effective_message.reply_text(
        "Отправьте свой номер телефона кнопкой ниже или введите вручную:",
        reply_markup=PHONE_KB,
    )
    return REG_PHONE


async def reg_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if message.contact:
        if message.contact.user_id and message.contact.user_id != update.effective_user.id:
            await message.reply_text("Отправьте именно свой номер телефона.")
            return REG_PHONE
        raw = message.contact.phone_number
    else:
        raw = message.text

    phone = normalize_phone(raw)
    if not phone:
        await message.reply_text("Неверный номер. Пример: +7 999 123-45-67")
        return REG_PHONE

    context.user_data["reg"]["phone"] = phone
    await message.reply_text(
        "Введите марку и модель автомобиля.\nНапример: Mercedes-Benz S-Class W223",
        reply_markup=ReplyKeyboardRemove(),
    )
    return REG_CAR


async def reg_car(update: Update, context: ContextTypes.DEFAULT_TYPE):
    car = clean_text(update.effective_message.text, 120)
    if len(car) < 4:
        await update.effective_message.reply_text("Укажите автомобиль подробнее.")
        return REG_CAR

    context.user_data["reg"]["car"] = car
    await update.effective_message.reply_text("Введите год выпуска автомобиля:")
    return REG_YEAR


async def reg_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    year = clean_text(update.effective_message.text, 10)
    if not re.fullmatch(r"20\d{2}|19\d{2}", year):
        await update.effective_message.reply_text("Введите год четырьмя цифрами.")
        return REG_YEAR

    context.user_data["reg"]["year"] = year
    await update.effective_message.reply_text("Введите государственный номер автомобиля:")
    return REG_PLATE


async def reg_plate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    plate = clean_text(update.effective_message.text.upper(), 20)
    if len(plate) < 5:
        await update.effective_message.reply_text("Введите госномер полностью.")
        return REG_PLATE

    context.user_data["reg"]["plate"] = plate
    await update.effective_message.reply_text(
        "Выберите класс автомобиля:",
        reply_markup=DRIVER_CLASS_KB,
    )
    return REG_CLASS


async def reg_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    car_class = clean_text(update.effective_message.text, 30)
    if car_class not in CAR_CLASSES:
        await update.effective_message.reply_text(
            "Выберите класс на клавиатуре.",
            reply_markup=DRIVER_CLASS_KB,
        )
        return REG_CLASS

    context.user_data["reg"]["car_class"] = car_class
    await update.effective_message.reply_text(
        "Отправьте фотографии:\n"
        "• водительское удостоверение;\n"
        "• СТС;\n"
        "• автомобиль снаружи и внутри.\n\n"
        "После загрузки нажмите «Готово». Максимум 10 фото.",
        reply_markup=DONE_KB,
    )
    return REG_PHOTOS


async def reg_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reg = context.user_data.get("reg")
    if not reg:
        await update.effective_message.reply_text("Начните регистрацию заново: /start")
        return ConversationHandler.END

    if update.effective_message.photo:
        if len(reg["photos"]) >= 10:
            await update.effective_message.reply_text(
                "Достигнут лимит 10 фото. Нажмите «Готово»."
            )
            return REG_PHOTOS

        reg["photos"].append(update.effective_message.photo[-1].file_id)
        await update.effective_message.reply_text(
            f"Фото добавлено. Всего: {len(reg['photos'])}"
        )
        return REG_PHOTOS

    if update.effective_message.text and update.effective_message.text.lower() == "готово":
        if len(reg["photos"]) < 3:
            await update.effective_message.reply_text(
                "Нужно минимум 3 фото: права, СТС и автомобиль."
            )
            return REG_PHOTOS

        kb = InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Отправить", callback_data="reg_send"),
                InlineKeyboardButton("❌ Отмена", callback_data="reg_cancel"),
            ]]
        )
        await update.effective_message.reply_text(
            registration_summary(reg) + "\n\nОтправить анкету модератору?",
            reply_markup=kb,
        )
        return REG_CONFIRM

    await update.effective_message.reply_text("Отправьте фото или нажмите «Готово».")
    return REG_PHOTOS


async def reg_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    query = update.callback_query
    await query.answer()

    if query.data == "reg_cancel":
        context.user_data.pop("reg", None)
        await query.edit_message_text("Регистрация отменена.")
        return ConversationHandler.END

    reg = context.user_data.get("reg")
    if not reg:
        await query.edit_message_text("Данные анкеты не найдены. Начните заново.")
        return ConversationHandler.END

    app_id = uuid.uuid4().hex[:8].upper()
    application = {
        **reg,
        "application_id": app_id,
        "user_id": query.from_user.id,
        "username": query.from_user.username or "",
        "status": "pending",
        "submitted_at": now_iso(),
    }
    context.bot_data["driver_apps"][app_id] = application

    username_line = f"@{application['username']}" if application["username"] else "—"
    text = (
        f"👨‍✈️ <b>НОВАЯ АНКЕТА ВОДИТЕЛЯ №{esc(app_id)}</b>\n\n"
        f"ФИО: {esc(application['name'])}\n"
        f"Телефон: {esc(application['phone'])}\n"
        f"Автомобиль: {esc(application['car'])}\n"
        f"Год: {esc(application['year'])}\n"
        f"Госномер: {esc(application['plate'])}\n"
        f"Класс: {esc(application['car_class'])}\n"
        f"Telegram: {esc(username_line)}\n"
        f"Telegram ID: <code>{application['user_id']}</code>\n"
        f"Фото: {len(application['photos'])}"
    )

    moderation_kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{app_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{app_id}"),
        ]]
    )

    try:
        sent = await context.bot.send_message(
            MODERATION_CHAT_ID,
            text,
            parse_mode="HTML",
            reply_markup=moderation_kb,
        )
        application["moderation_message_id"] = sent.message_id

        for index, photo_id in enumerate(application["photos"], start=1):
            await context.bot.send_photo(
                MODERATION_CHAT_ID,
                photo_id,
                caption=f"Анкета №{app_id}: фото {index}/{len(application['photos'])}",
            )
    except TelegramError:
        logger.exception("Не удалось отправить анкету в группу модерации")
        context.bot_data["driver_apps"].pop(app_id, None)
        await query.edit_message_text(
            "Не удалось отправить анкету. Попробуйте позже."
        )
        return ConversationHandler.END

    context.user_data.pop("reg", None)
    await query.edit_message_text(
        f"✅ Анкета №{app_id} отправлена модератору. Ожидайте решения."
    )
    return ConversationHandler.END


async def moderate_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    query = update.callback_query

    if not query.message or query.message.chat_id != MODERATION_CHAT_ID:
        await query.answer("Кнопка доступна только в группе модерации.", show_alert=True)
        return

    if not await is_chat_admin(context.bot, MODERATION_CHAT_ID, query.from_user.id):
        await query.answer("Только для администраторов.", show_alert=True)
        return

    action, app_id = query.data.split("_", 1)
    application = context.bot_data["driver_apps"].get(app_id)

    if not application:
        await query.answer("Анкета не найдена.", show_alert=True)
        return

    if application.get("status") != "pending":
        await query.answer("Решение по анкете уже принято.", show_alert=True)
        return

    if action == "reject":
        application["status"] = "rejected"
        application["moderated_by"] = query.from_user.id
        application["moderated_at"] = now_iso()
        await query.answer("Анкета отклонена")
        await query.edit_message_text(
            (query.message.text or "Анкета водителя") + "\n\n❌ ОТКЛОНЕНО"
        )
        try:
            await context.bot.send_message(
                application["user_id"],
                "❌ Ваша заявка водителя отклонена модератором.",
                reply_markup=MAIN_KB,
            )
        except TelegramError:
            logger.exception("Не удалось уведомить отклонённого водителя")
        return

    application["status"] = "approved"
    application["moderated_by"] = query.from_user.id
    application["moderated_at"] = now_iso()

    context.bot_data["drivers"][str(application["user_id"])] = {
        "user_id": application["user_id"],
        "name": application["name"],
        "phone": application["phone"],
        "car": application["car"],
        "year": application["year"],
        "plate": application["plate"],
        "car_class": application["car_class"],
        "status": "approved",
        "approved_at": now_iso(),
        "approved_by": query.from_user.id,
    }

    invite_url = None
    try:
        invite = await context.bot.create_chat_invite_link(
            chat_id=ORDERS_CHAT_ID,
            name=f"driver_{application['user_id']}",
            expire_date=datetime.now(timezone.utc) + timedelta(hours=24),
            creates_join_request=True,
        )
        invite_url = invite.invite_link
    except TelegramError:
        logger.exception("Не удалось создать ссылку для группы заказов")

    await query.answer("Водитель одобрен")
    await query.edit_message_text(
        (query.message.text or "Анкета водителя") + "\n\n✅ ОДОБРЕНО"
    )

    try:
        if invite_url:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🚖 Вступить в группу заказов", url=invite_url)]]
            )
            await context.bot.send_message(
                application["user_id"],
                "✅ Ваша заявка одобрена.\n"
                f"Класс автомобиля: {application['car_class']}\n\n"
                "Нажмите кнопку и отправьте запрос на вступление в закрытую группу заказов.",
                reply_markup=kb,
            )
        else:
            await context.bot.send_message(
                application["user_id"],
                "✅ Ваша заявка одобрена, но бот не смог создать ссылку в группу заказов. "
                "Обратитесь к администратору.",
            )
    except TelegramError:
        logger.exception("Не удалось уведомить одобренного водителя")


async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    request = update.chat_join_request

    if request.chat.id != ORDERS_CHAT_ID:
        return

    driver = driver_record(context, request.from_user.id)
    try:
        if driver and driver.get("status") == "approved":
            await context.bot.approve_chat_join_request(
                ORDERS_CHAT_ID,
                request.from_user.id,
            )
            await context.bot.send_message(
                request.from_user.id,
                "✅ Доступ в группу заказов подтверждён.",
            )
        else:
            await context.bot.decline_chat_join_request(
                ORDERS_CHAT_ID,
                request.from_user.id,
            )
    except TelegramError:
        logger.exception("Ошибка обработки запроса на вступление")


async def list_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    if update.effective_chat.id != MODERATION_CHAT_ID:
        return
    if not await is_chat_admin(context.bot, MODERATION_CHAT_ID, update.effective_user.id):
        await update.effective_message.reply_text("Команда только для администраторов.")
        return

    drivers = list(context.bot_data["drivers"].values())
    if not drivers:
        await update.effective_message.reply_text("Одобренных водителей пока нет.")
        return

    lines = ["✅ Одобренные водители:"]
    for item in drivers:
        lines.append(
            f"\n{item['name']}\n"
            f"ID: {item['user_id']}\n"
            f"Класс: {item['car_class']}\n"
            f"Авто: {item['car']} {item['year']}\n"
            f"Номер: {item['plate']}"
        )

    await update.effective_message.reply_text("\n".join(lines[:80]))


async def remove_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    if update.effective_chat.id != MODERATION_CHAT_ID:
        return
    if not await is_chat_admin(context.bot, MODERATION_CHAT_ID, update.effective_user.id):
        await update.effective_message.reply_text("Команда только для администраторов.")
        return

    if not context.args:
        await update.effective_message.reply_text("Использование: /removedriver TELEGRAM_ID")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Telegram ID должен быть числом.")
        return

    removed = context.bot_data["drivers"].pop(str(user_id), None)
    if not removed:
        await update.effective_message.reply_text("Водитель не найден.")
        return

    try:
        await context.bot.ban_chat_member(ORDERS_CHAT_ID, user_id)
        await context.bot.unban_chat_member(ORDERS_CHAT_ID, user_id, only_if_banned=True)
    except TelegramError:
        logger.exception("Не удалось удалить водителя из группы заказов")

    await update.effective_message.reply_text(
        f"Водитель {removed['name']} удалён из системы и группы заказов."
    )

# ================= ПРИНЯТИЕ ЗАКАЗА =================


async def take_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    query = update.callback_query

    if not query.message or query.message.chat_id != ORDERS_CHAT_ID:
        await query.answer("Кнопка недоступна.", show_alert=True)
        return

    order_id = query.data.removeprefix("take_")
    order = context.bot_data["orders"].get(order_id)
    driver = driver_record(context, query.from_user.id)

    if not driver or driver.get("status") != "approved":
        await query.answer(
            "Сначала зарегистрируйтесь и получите одобрение модератора.",
            show_alert=True,
        )
        return

    if not order:
        await query.answer("Заказ не найден.", show_alert=True)
        return

    if order.get("status") != "open":
        await query.answer("Заказ уже взят другим водителем.", show_alert=True)
        return

    if context.bot_data["active_by_user"].get(str(query.from_user.id)):
        await query.answer("Сначала завершите текущий заказ.", show_alert=True)
        return

    required_class = order["car_class"]
    if required_class != "Неважно" and driver["car_class"] != required_class:
        await query.answer(
            f"Этот заказ доступен только классу {required_class}. "
            f"Ваш класс: {driver['car_class']}.",
            show_alert=True,
        )
        return

    order["status"] = "taken"
    order["driver_id"] = query.from_user.id
    order["driver_name"] = driver["name"]
    order["taken_at"] = now_iso()

    context.bot_data["pending_by_client"].pop(str(order["client_id"]), None)
    context.bot_data["active_by_user"][str(order["client_id"])] = order_id
    context.bot_data["active_by_user"][str(query.from_user.id)] = order_id

    try:
        await query.answer("Заказ закреплён за вами ✅", show_alert=True)
        await context.bot.delete_message(ORDERS_CHAT_ID, query.message.message_id)
    except TelegramError:
        logger.exception("Не удалось удалить заказ из группы")
        try:
            await query.edit_message_text(
                "🔴 Заказ уже принят водителем.",
                reply_markup=None,
            )
        except TelegramError:
            pass

    finish_kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📍 Я на месте", callback_data=f"arrived_{order_id}")],
            [InlineKeyboardButton("▶️ Начать поездку", callback_data=f"starttrip_{order_id}")],
            [InlineKeyboardButton("✅ Завершить заказ", callback_data=f"finish_{order_id}")],
        ]
    )

    try:
        driver_message = await context.bot.send_message(
            query.from_user.id,
            driver_private_order_text(order_id, order),
            parse_mode="HTML",
            reply_markup=finish_kb,
            disable_web_page_preview=False,
        )
        add_tracked(order, driver_message.chat_id, driver_message.message_id)
    except Forbidden:
        # Не оставляем заказ закреплённым, если водитель не открыл бота.
        context.bot_data["active_by_user"].pop(str(order["client_id"]), None)
        context.bot_data["active_by_user"].pop(str(query.from_user.id), None)
        order["status"] = "open"
        order["driver_id"] = None
        logger.warning("Водитель не запускал бота в личном чате")
        try:
            sent = await context.bot.send_message(
                ORDERS_CHAT_ID,
                order_public_text(order_id, order),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🟢 Взять заказ", callback_data=f"take_{order_id}")]]
                ),
            )
            order["group_message_id"] = sent.message_id
        except TelegramError:
            logger.exception("Не удалось вернуть заказ в группу")
        return

    client_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📸 Запросить фото машины", callback_data=f"request_photo_{order_id}")]]
    )

    client_message = await context.bot.send_message(
        order["client_id"],
        f"🚘 Водитель принял заказ №{order_id}.\n\n"
        "Пишите сообщения в этот чат — бот передаст их водителю. "
        "Контакты сторон скрыты.\n\n"
        "Можете запросить фото машины кнопкой ниже.",
        reply_markup=client_kb,
    )
    add_tracked(order, client_message.chat_id, client_message.message_id)


async def request_car_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    query = update.callback_query
    order_id = query.data.removeprefix("request_photo_")
    order = context.bot_data["orders"].get(order_id)

    if not order:
        await query.answer("Заказ уже закрыт.", show_alert=True)
        return

    if query.from_user.id != order.get("client_id"):
        await query.answer("Запросить фото может только клиент этого заказа.", show_alert=True)
        return

    if order.get("status") != "taken" or not order.get("driver_id"):
        await query.answer("Водитель ещё не назначен.", show_alert=True)
        return

    order["car_photo_requested_at"] = now_iso()

    await query.answer("Запрос фото отправлен водителю ✅", show_alert=True)

    try:
        client_notice = await context.bot.send_message(
            order["client_id"],
            f"📸 Запрос фото машины по заказу №{order_id} отправлен водителю.",
        )
        add_tracked(order, client_notice.chat_id, client_notice.message_id)
    except TelegramError:
        logger.exception("Не удалось уведомить клиента о запросе фото %s", order_id)

    try:
        driver_notice = await context.bot.send_message(
            order["driver_id"],
            f"📸 Клиент запросил фото машины по заказу №{order_id}.\n\n"
            "Отправьте фото автомобиля прямо в этот чат — бот передаст его клиенту. "
            "Госномер и лица на фото лучше закрыть, если не хотите их показывать.",
        )
        add_tracked(order, driver_notice.chat_id, driver_notice.message_id)
    except Forbidden:
        await query.answer("Водитель не открыл личный чат с ботом.", show_alert=True)
    except TelegramError:
        logger.exception("Не удалось отправить водителю запрос фото %s", order_id)
        await query.answer("Не удалось отправить запрос водителю.", show_alert=True)

# ================= АНОНИМНАЯ ПЕРЕПИСКА =================


async def relay_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    message = update.effective_message
    user_id = update.effective_user.id
    order_id = context.bot_data["active_by_user"].get(str(user_id))

    if not order_id:
        return

    order = context.bot_data["orders"].get(order_id)
    if not order or order.get("status") != "taken":
        context.bot_data["active_by_user"].pop(str(user_id), None)
        return

    is_client = user_id == order["client_id"]
    is_driver = user_id == order["driver_id"]
    if not (is_client or is_driver):
        return

    recipient_id = order["driver_id"] if is_client else order["client_id"]
    sender_label = "клиента" if is_client else "водителя"

    add_tracked(order, message.chat_id, message.message_id)

    if message.contact:
        try:
            await message.delete()
        except TelegramError:
            pass
        warning = await context.bot.send_message(
            user_id,
            "Передача контактов запрещена. Общайтесь через бота.",
        )
        add_tracked(order, warning.chat_id, warning.message_id)
        return

    content_text = message.text or message.caption or ""
    if content_text and contact_data_detected(content_text):
        try:
            await message.delete()
        except TelegramError:
            pass
        warning = await context.bot.send_message(
            user_id,
            "Номер телефона, Telegram username и ссылки на профиль передавать нельзя.",
        )
        add_tracked(order, warning.chat_id, warning.message_id)
        return

    try:
        header = await context.bot.send_message(
            recipient_id,
            f"💬 Сообщение {sender_label} по заказу №{order_id}:",
        )
        add_tracked(order, header.chat_id, header.message_id)

        copied = await context.bot.copy_message(
            chat_id=recipient_id,
            from_chat_id=message.chat_id,
            message_id=message.message_id,
        )
        add_tracked(order, recipient_id, copied.message_id)
    except TelegramError:
        logger.exception("Не удалось передать сообщение по заказу %s", order_id)
        error_message = await context.bot.send_message(
            user_id,
            "Не удалось передать сообщение. Попробуйте ещё раз.",
        )
        add_tracked(order, error_message.chat_id, error_message.message_id)


async def start_trip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    query = update.callback_query
    order_id = query.data.removeprefix("starttrip_")
    order = context.bot_data["orders"].get(order_id)

    if not order:
        await query.answer("Заказ уже закрыт.", show_alert=True)
        return
    if query.from_user.id != order.get("driver_id"):
        await query.answer("Начать поездку может только назначенный водитель.", show_alert=True)
        return
    if order.get("status") != "taken":
        await query.answer("Заказ уже не активен.", show_alert=True)
        return
    if order.get("started_at"):
        await query.answer("Поездка уже начата.", show_alert=True)
        return

    order["started_at"] = now_iso()
    order["started_at_display"] = format_dt(now_moscow())
    await query.answer("Клиенту отправлено: поездка началась ✅", show_alert=True)

    finish_only_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Завершить заказ", callback_data=f"finish_{order_id}")]]
    )
    try:
        await query.edit_message_text(
            driver_private_order_text(order_id, order),
            parse_mode="HTML",
            reply_markup=finish_only_kb,
            disable_web_page_preview=False,
        )
    except TelegramError:
        logger.exception("Не удалось обновить карточку начала поездки %s", order_id)

    try:
        msg = await context.bot.send_message(
            order["client_id"],
            f"▶️ Поездка по заказу №{order_id} началась.\n"
            f"Время начала: {order['started_at_display']}",
        )
        add_tracked(order, msg.chat_id, msg.message_id)
    except TelegramError:
        logger.exception("Не удалось уведомить клиента о начале поездки %s", order_id)


async def arrived_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    query = update.callback_query
    order_id = query.data.removeprefix("arrived_")
    order = context.bot_data["orders"].get(order_id)

    if not order:
        await query.answer("Заказ уже закрыт.", show_alert=True)
        return

    if query.from_user.id != order.get("driver_id"):
        await query.answer("Отметку может поставить только назначенный водитель.", show_alert=True)
        return

    if order.get("status") != "taken":
        await query.answer("Заказ уже не активен.", show_alert=True)
        return

    if order.get("arrived_at"):
        await query.answer("Вы уже отметились на месте.", show_alert=True)
        return

    order["arrived_at"] = now_iso()
    order["arrived_at_display"] = format_dt(now_moscow())

    await query.answer("Клиенту отправлено: водитель на месте ✅", show_alert=True)

    finish_only_kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("▶️ Начать поездку", callback_data=f"starttrip_{order_id}")],
            [InlineKeyboardButton("✅ Завершить заказ", callback_data=f"finish_{order_id}")],
        ]
    )

    try:
        await query.edit_message_text(
            driver_private_order_text(order_id, order),
            parse_mode="HTML",
            reply_markup=finish_only_kb,
            disable_web_page_preview=False,
        )
    except TelegramError:
        logger.exception("Не удалось обновить карточку заказа у водителя %s", order_id)

    try:
        msg = await context.bot.send_message(
            order["client_id"],
            f"📍 Водитель прибыл на место подачи по заказу №{order_id}.\n"
            f"Время отметки: {order['arrived_at_display']}\n\n"
            "Пишите в этот чат, если нужно уточнить точку встречи.",
        )
        add_tracked(order, msg.chat_id, msg.message_id)
    except TelegramError:
        logger.exception("Не удалось уведомить клиента о прибытии по заказу %s", order_id)


async def finish_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    query = update.callback_query
    order_id = query.data.removeprefix("finish_")
    order = context.bot_data["orders"].get(order_id)

    if not order:
        await query.answer("Заказ уже завершён.", show_alert=True)
        return

    if query.from_user.id != order.get("driver_id"):
        await query.answer("Завершить заказ может только назначенный водитель.", show_alert=True)
        return

    await query.answer("Заказ завершён")
    order["status"] = "completed"
    order["completed_at"] = now_iso()
    order["completed_at_display"] = format_dt(now_moscow())

    # Удаляем сообщения заказа и переписки в обоих личных чатах.
    tracked = list(dict.fromkeys(tuple(item) for item in order.get("tracked_messages", [])))
    for chat_id, message_id in reversed(tracked):
        try:
            await context.bot.delete_message(chat_id, message_id)
        except (BadRequest, Forbidden):
            # Telegram может не удалить слишком старое сообщение или сообщение без прав.
            pass
        except TelegramError:
            logger.exception("Ошибка удаления сообщения %s/%s", chat_id, message_id)

    client_id = order["client_id"]
    driver_id = order["driver_id"]

    context.bot_data["active_by_user"].pop(str(client_id), None)
    context.bot_data["active_by_user"].pop(str(driver_id), None)
    context.bot_data["pending_by_client"].pop(str(client_id), None)
    context.bot_data["orders"].pop(order_id, None)

    try:
        await context.bot.send_message(
            client_id,
            "✅ Заказ завершён. Связь с водителем закрыта.\n"
            f"Начало: {order.get('started_at_display', '—')}\n"
            f"Окончание: {order.get('completed_at_display', '—')}",
            reply_markup=MAIN_KB,
        )
    except TelegramError:
        pass

    try:
        await context.bot.send_message(
            driver_id,
            "✅ Заказ завершён. Связь с клиентом закрыта.\n"
            f"Начало: {order.get('started_at_display', '—')}\n"
            f"Окончание: {order.get('completed_at_display', '—')}",
            reply_markup=MAIN_KB,
        )
    except TelegramError:
        pass

# ================= ОШИБКИ И ЗАПУСК =================


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Необработанное исключение", exc_info=context.error)


async def post_init(application: Application) -> None:
    """После перезапуска Railway заново ставит таймеры удаления открытых заказов."""
    for order_id, order in list(application.bot_data.get("orders", {}).items()):
        if order.get("status") != "open":
            continue
        expires_dt = parse_iso_dt(order.get("expires_at", ""))
        if not expires_dt:
            continue
        delay = max(1, int((expires_dt - now_moscow()).total_seconds()))
        asyncio.create_task(expire_order_later(application, order_id, delay))


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Укажите переменную окружения BOT_TOKEN")

    persistence_file = Path(PERSISTENCE_PATH)
    if persistence_file.parent != Path("."):
        persistence_file.parent.mkdir(parents=True, exist_ok=True)

    persistence = PicklePersistence(filepath=PERSISTENCE_PATH)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .post_init(post_init)
        .concurrent_updates(False)
        .build()
    )

    order_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("order", order_start),
            MessageHandler(filters.Regex(r"^🚖 Заказать поездку$"), order_start),
        ],
        states={
            ORDER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_name)],
            ORDER_FROM: [
                MessageHandler(
                    (filters.TEXT | filters.LOCATION) & ~filters.COMMAND,
                    order_from,
                )
            ],
            ORDER_TO: [
                MessageHandler(
                    (filters.TEXT | filters.LOCATION) & ~filters.COMMAND,
                    order_to,
                )
            ],
            ORDER_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_time)],
            ORDER_CLASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_class)],
            ORDER_TARIFF: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_tariff)],
            ORDER_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_price)],
            ORDER_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_comment)],
            ORDER_CONFIRM: [
                CallbackQueryHandler(order_confirm, pattern=r"^order_(send|cancel)$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        name="client_order",
        persistent=True,
    )

    registration_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("register_driver", reg_start),
            MessageHandler(filters.Regex(r"^👨‍✈️ Стать водителем$"), reg_start),
        ],
        states={
            REG_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)],
            REG_PHONE: [
                MessageHandler(filters.CONTACT, reg_phone),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_phone),
            ],
            REG_CAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_car)],
            REG_YEAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_year)],
            REG_PLATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_plate)],
            REG_CLASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_class)],
            REG_PHOTOS: [
                MessageHandler(filters.PHOTO, reg_photos),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_photos),
            ],
            REG_CONFIRM: [
                CallbackQueryHandler(reg_confirm, pattern=r"^reg_(send|cancel)$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        name="driver_registration",
        persistent=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chat_id))
    app.add_handler(CommandHandler("drivers", list_drivers))
    app.add_handler(CommandHandler("removedriver", remove_driver))
    app.add_handler(MessageHandler(filters.Regex(r"^📋 Мой статус$"), my_status))

    app.add_handler(order_conversation)
    app.add_handler(registration_conversation)

    app.add_handler(CallbackQueryHandler(cancel_open_order, pattern=r"^cancel_open_[A-F0-9]{8}$"))
    app.add_handler(CallbackQueryHandler(moderate_driver, pattern=r"^(approve|reject)_[A-F0-9]{8}$"))
    app.add_handler(CallbackQueryHandler(take_order, pattern=r"^take_[A-F0-9]{8}$"))
    app.add_handler(CallbackQueryHandler(arrived_order, pattern=r"^arrived_[A-F0-9]{8}$"))
    app.add_handler(CallbackQueryHandler(start_trip, pattern=r"^starttrip_[A-F0-9]{8}$"))
    app.add_handler(CallbackQueryHandler(request_car_photo, pattern=r"^request_photo_[A-F0-9]{8}$"))
    app.add_handler(CallbackQueryHandler(finish_order, pattern=r"^finish_[A-F0-9]{8}$"))

    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    # Переписка через бота. Добавляется после диалогов регистрации и заказа.
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            relay_message,
        ),
        group=1,
    )

    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
