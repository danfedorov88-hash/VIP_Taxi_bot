# -*- coding: utf-8 -*-
"""
VIP Taxi Bot — кнопочный заказ без AI.

Основные функции:
- заказ через кнопки, геолокацию и адреса;
- несколько точек маршрута;
- имя клиента берётся из Telegram и сохраняется;
- количество пассажиров отсутствует;
- фиксированные аэропортовые и почасовые тарифы;
- особые запросы без AI;
- регистрация и модерация водителей;
- фото автомобиля отправляются клиенту после принятия заказа;
- блокировка, разблокировка и удаление водителей;
- статусы поездки и расчёт длительности;
- анонимная переписка клиент ↔ водитель.
"""

import html
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PicklePersistence,
    filters,
)

# ========================= НАСТРОЙКИ =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MODERATION_CHAT_ID = int(os.getenv("MODERATION_CHAT_ID", "-5062249297"))
ORDERS_CHAT_ID = int(os.getenv("ORDERS_CHAT_ID", "-1003446115764"))
PERSISTENCE_PATH = os.getenv("PERSISTENCE_PATH", "bot_state.pickle")

MSK = timezone(timedelta(hours=3))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ========================= СОСТОЯНИЯ =========================

(
    ORDER_FROM,
    ORDER_TO,
    ORDER_ROUTE_MENU,
    ORDER_EXTRA_POINT,
    ORDER_TIME,
    ORDER_CLASS,
    ORDER_TARIFF,
    ORDER_HOURS,
    ORDER_COMMENT,
    ORDER_CONFIRM,
    SPECIAL_KIND,
    SPECIAL_FROM,
    SPECIAL_TO,
    SPECIAL_TIME,
    SPECIAL_COMMENT,
    SPECIAL_CONFIRM,
) = range(16)

(
    REG_NAME,
    REG_PHONE,
    REG_CAR,
    REG_YEAR,
    REG_PLATE,
    REG_CLASS,
    REG_DOCS,
    REG_CAR_PHOTOS,
    REG_CONFIRM,
) = range(30, 39)

CAR_CLASSES = {"Business", "First", "Lux", "Минивэн"}
ORDER_CLASSES = CAR_CLASSES | {"Неважно"}

HOURLY_RATES = {
    "Business": 2500,
    "First": 5000,
    "Lux": 7000,
    "Минивэн": 4000,
}

AIRPORT_RATES = {
    "svo_vko": {
        "Business": 5000,
        "First": 10000,
        "Lux": 12000,
        "Минивэн": 10000,
    },
    "dme_zia": {
        "Business": 7000,
        "First": 12000,
        "Lux": 14000,
        "Минивэн": 12000,
    },
}

MAIN_KB = ReplyKeyboardMarkup(
    [
        ["🚖 Заказать поездку", "✨ Особый запрос"],
        ["👨‍✈️ Стать водителем", "📋 Мой статус"],
    ],
    resize_keyboard=True,
)

LOCATION_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📍 Отправить геопозицию", request_location=True)],
        ["✍️ Ввести адрес"],
        ["❌ Отмена"],
    ],
    resize_keyboard=True,
)

ROUTE_MENU_KB = ReplyKeyboardMarkup(
    [
        ["➕ Добавить точку", "✅ Маршрут готов"],
        ["🗑 Удалить последнюю точку"],
        ["❌ Отмена"],
    ],
    resize_keyboard=True,
)

TIME_KB = ReplyKeyboardMarkup(
    [["Сейчас"], ["Указать дату и время"], ["❌ Отмена"]],
    resize_keyboard=True,
)

CLASS_KB = ReplyKeyboardMarkup(
    [["Business", "First"], ["Lux", "Минивэн"], ["Неважно"], ["❌ Отмена"]],
    resize_keyboard=True,
)

DRIVER_CLASS_KB = ReplyKeyboardMarkup(
    [["Business", "First"], ["Lux", "Минивэн"], ["❌ Отмена"]],
    resize_keyboard=True,
)

TARIFF_KB = ReplyKeyboardMarkup(
    [["Разовая поездка"], ["Почасовая"], ["Аэропорт"], ["Бизнес-день"], ["❌ Отмена"]],
    resize_keyboard=True,
)

SKIP_KB = ReplyKeyboardMarkup([["Пропустить"], ["❌ Отмена"]], resize_keyboard=True)
DONE_KB = ReplyKeyboardMarkup([["Готово"], ["❌ Отмена"]], resize_keyboard=True)

SPECIAL_KIND_KB = ReplyKeyboardMarkup(
    [
        ["Rolls-Royce / Bentley", "Maybach"],
        ["Свадебный кортеж", "Несколько автомобилей"],
        ["Охрана / сопровождение", "Встреча делегации"],
        ["Другое", "❌ Отмена"],
    ],
    resize_keyboard=True,
)

PHONE_KB = ReplyKeyboardMarkup(
    [
        [KeyboardButton("📱 Отправить мой номер", request_contact=True)],
        ["❌ Отмена"],
    ],
    resize_keyboard=True,
)


# ========================= ВСПОМОГАТЕЛЬНОЕ =========================

def clean_text(value: Optional[str], limit: int = 500) -> str:
    return " ".join((value or "").strip().split())[:limit]


def esc(value: Any) -> str:
    return html.escape(str(value if value not in (None, "") else "—"))


def now_msk() -> datetime:
    return datetime.now(MSK)


def now_iso() -> str:
    return now_msk().isoformat()


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def format_dt(value: datetime) -> str:
    return value.astimezone(MSK).strftime("%d.%m.%Y %H:%M")


def format_clock(value: Optional[str]) -> str:
    dt = parse_iso(value)
    return dt.astimezone(MSK).strftime("%H:%M") if dt else "—"


def format_duration(start_iso: Optional[str], finish_iso: Optional[str]) -> str:
    start = parse_iso(start_iso)
    finish = parse_iso(finish_iso)
    if not start or not finish or finish < start:
        return "—"
    total_minutes = int((finish - start).total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


def normalize_phone(value: Optional[str]) -> Optional[str]:
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return "+" + digits if 10 <= len(digits) <= 15 else None


def ensure_storage(context: ContextTypes.DEFAULT_TYPE) -> None:
    bd = context.bot_data
    bd.setdefault("clients", {})
    bd.setdefault("drivers", {})
    bd.setdefault("driver_apps", {})
    bd.setdefault("orders", {})
    bd.setdefault("active_by_user", {})
    bd.setdefault("open_by_client", {})
    bd.setdefault("message_registry", {})


def save_client_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    ensure_storage(context)
    user = update.effective_user
    uid = str(user.id)
    clients = context.bot_data["clients"]
    existing = clients.get(uid, {})
    name = existing.get("name") or clean_text(user.first_name, 100) or "Клиент"
    clients[uid] = {
        **existing,
        "name": name,
        "telegram_username": user.username,
        "updated_at": now_iso(),
    }
    return name


def point_from_message(message) -> Optional[dict[str, Any]]:
    if message.location:
        return {
            "type": "location",
            "label": "Геопозиция",
            "latitude": message.location.latitude,
            "longitude": message.location.longitude,
        }

    text = clean_text(message.text, 300)
    if not text or text in {"✍️ Ввести адрес", "❌ Отмена"}:
        return None

    return {
        "type": "address",
        "label": text,
        "latitude": None,
        "longitude": None,
    }


def point_text(point: dict[str, Any]) -> str:
    if point.get("type") == "location":
        lat = point.get("latitude")
        lon = point.get("longitude")
        return f"https://yandex.ru/maps/?pt={lon:.6f},{lat:.6f}&z=16&l=map"
    return clean_text(point.get("label"), 300) or "—"


def route_text(points: list[dict[str, Any]]) -> str:
    if not points:
        return "—"
    return "\n".join(f"{index}. {point_text(point)}" for index, point in enumerate(points, start=1))


def parse_order_datetime(raw_text: str) -> datetime:
    raw = clean_text(raw_text, 120).lower().replace(",", " ")
    now = now_msk()

    if raw == "сейчас":
        return now.replace(second=0, microsecond=0)

    # Строгий формат: 18.07.2026 18:00 или 18.07.2026 в 18:00
    full = re.search(
        r"(?P<day>\d{1,2})[./-](?P<month>\d{1,2})[./-](?P<year>\d{4})"
        r"(?:\s+в?\s*)(?P<hour>\d{1,2})[:.](?P<minute>\d{2})",
        raw,
    )
    if full:
        dt = datetime(
            int(full.group("year")),
            int(full.group("month")),
            int(full.group("day")),
            int(full.group("hour")),
            int(full.group("minute")),
            tzinfo=MSK,
        )
        if dt < now:
            raise ValueError("Дата уже прошла")
        return dt

    # Сегодня/завтра HH:MM
    rel = re.search(r"(?P<hour>\d{1,2})[:.](?P<minute>\d{2})", raw)
    if rel and ("сегодня" in raw or "завтра" in raw):
        date = now.date() + (timedelta(days=1) if "завтра" in raw else timedelta())
        dt = datetime(
            date.year,
            date.month,
            date.day,
            int(rel.group("hour")),
            int(rel.group("minute")),
            tzinfo=MSK,
        )
        if dt < now:
            raise ValueError("Время уже прошло")
        return dt

    raise ValueError("Неверный формат даты и времени")


def airport_group(points: list[dict[str, Any]]) -> Optional[str]:
    text = " ".join(point_text(point).lower() for point in points)
    if any(token in text for token in ("шереметьево", "svo", "внуково", "vko")):
        return "svo_vko"
    if any(token in text for token in ("домодедово", "dme", "жуковский", "zia")):
        return "dme_zia"
    return None


def calculate_price(order: dict[str, Any]) -> str:
    if order.get("special_request"):
        return "По договорённости"

    car_class = order.get("car_class")
    tariff = order.get("tariff")
    points = order.get("route_points", [])

    if tariff == "Аэропорт":
        # При дополнительных точках автоматическая цена не выставляется.
        if len(points) > 2:
            return "По договорённости"
        group = airport_group(points)
        if group and car_class in AIRPORT_RATES[group]:
            return f"{AIRPORT_RATES[group][car_class]:,} ₽".replace(",", " ")
        return "По договорённости"

    if tariff == "Почасовая":
        rate = HOURLY_RATES.get(car_class)
        hours = order.get("hours")
        if rate and hours:
            return f"{rate * int(hours):,} ₽".replace(",", " ")
        if rate:
            return f"{rate:,} ₽/час".replace(",", " ")

    return "По договорённости"


def order_summary(order: dict[str, Any]) -> str:
    lines = [
        "Проверьте заказ:",
        "",
        f"Имя: {order.get('client_name', '—')}",
        "Маршрут:",
        route_text(order.get("route_points", [])),
        f"Когда: {order.get('scheduled_text', '—')}",
        f"Класс: {order.get('car_class', '—')}",
        f"Тариф: {order.get('tariff', '—')}",
        f"Цена: {order.get('price', '—')}",
        f"Комментарий: {order.get('comment', '—')}",
    ]
    if order.get("special_request"):
        lines.append(f"Особый запрос: {order['special_request']}")
    return "\n".join(lines)


def order_public_text(order_id: str, order: dict[str, Any]) -> str:
    title = "✨ ОСОБЫЙ ЗАПРОС" if order.get("special_request") else "🚖 НОВЫЙ ЗАКАЗ"
    route = "\n".join(
        f"{index}. {esc(point_text(point))}"
        for index, point in enumerate(order.get("route_points", []), start=1)
    )
    return (
        f"{title} №{esc(order_id)}\n\n"
        f"📍 <b>Маршрут:</b>\n{route}\n"
        f"🕒 <b>Когда:</b> {esc(order.get('scheduled_text'))}\n"
        f"🚘 <b>Класс:</b> {esc(order.get('car_class'))}\n"
        f"💳 <b>Тариф:</b> {esc(order.get('tariff'))}\n"
        f"💰 <b>Цена:</b> {esc(order.get('price'))}\n"
        f"💬 <b>Комментарий:</b> {esc(order.get('comment'))}\n"
        f"✨ <b>Особый запрос:</b> {esc(order.get('special_request'))}\n\n"
        "Личные данные клиента скрыты."
    )


def driver_status_text(driver: dict[str, Any]) -> str:
    status_map = {
        "pending": "На проверке",
        "approved": "Активен",
        "blocked": "Заблокирован",
        "deleted": "Удалён",
        "rejected": "Отклонён",
    }
    return status_map.get(driver.get("status"), driver.get("status", "—"))


def is_moderator_chat(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.id == MODERATION_CHAT_ID)


async def send_once(
    context: ContextTypes.DEFAULT_TYPE,
    recipients: list[int],
    text: str,
    **kwargs: Any,
) -> None:
    sent: set[int] = set()
    for recipient in recipients:
        if not recipient or recipient in sent:
            continue
        sent.add(recipient)
        await context.bot.send_message(recipient, text, **kwargs)


def track_message(
    context: ContextTypes.DEFAULT_TYPE,
    order_id: str,
    chat_id: int,
    message_id: int,
) -> None:
    ensure_storage(context)
    registry = context.bot_data["message_registry"].setdefault(order_id, [])
    item = (int(chat_id), int(message_id))
    if item not in registry:
        registry.append(item)


async def tracked_send_message(
    context: ContextTypes.DEFAULT_TYPE,
    order_id: str,
    chat_id: int,
    text: str,
    **kwargs: Any,
):
    message = await context.bot.send_message(chat_id, text, **kwargs)
    track_message(context, order_id, chat_id, message.message_id)
    return message


async def tracked_send_photo(
    context: ContextTypes.DEFAULT_TYPE,
    order_id: str,
    chat_id: int,
    photo: str,
    **kwargs: Any,
):
    message = await context.bot.send_photo(chat_id, photo=photo, **kwargs)
    track_message(context, order_id, chat_id, message.message_id)
    return message


async def cleanup_order_messages(
    context: ContextTypes.DEFAULT_TYPE,
    order_id: str,
) -> None:
    """Удаляет сообщения, которые бот отправил или переслал по заказу.

    Telegram может не разрешить удалить отдельные входящие сообщения пользователя
    в личном чате. Такие ошибки игнорируются, но сообщения бота и пересланные
    ботом сообщения удаляются.
    """
    ensure_storage(context)
    items = list(context.bot_data["message_registry"].pop(order_id, []))
    for chat_id, message_id in reversed(items):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError:
            pass


async def clear_active_order(
    context: ContextTypes.DEFAULT_TYPE,
    order: dict[str, Any],
) -> None:
    context.bot_data["active_by_user"].pop(str(order.get("client_id")), None)
    context.bot_data["active_by_user"].pop(str(order.get("driver_id")), None)


# ========================= ОБЩИЕ КОМАНДЫ =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = save_client_name(update, context)
    await update.effective_message.reply_text(
        f"Здравствуйте, {name}.\nВыберите действие:",
        reply_markup=MAIN_KB,
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("order", None)
    context.user_data.pop("reg", None)
    await update.effective_message.reply_text(
        "Действие отменено.",
        reply_markup=MAIN_KB,
    )
    return ConversationHandler.END


async def my_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_storage(context)
    uid = str(update.effective_user.id)
    driver = context.bot_data["drivers"].get(uid)
    if not driver:
        await update.effective_message.reply_text(
            "Вы не зарегистрированы как водитель или анкета ещё не одобрена."
        )
        return

    await update.effective_message.reply_text(
        f"Статус водителя: {driver_status_text(driver)}\n"
        f"Автомобиль: {driver.get('car', '—')} {driver.get('year', '')}\n"
        f"Госномер: {driver.get('plate', '—')}\n"
        f"Класс: {driver.get('car_class', '—')}"
    )


# ========================= ОБЫЧНЫЙ ЗАКАЗ =========================

async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ensure_storage(context)
    uid = str(update.effective_user.id)
    if context.bot_data["active_by_user"].get(uid):
        await update.effective_message.reply_text(
            "У вас уже есть активный заказ. Сначала завершите его."
        )
        return ConversationHandler.END

    name = save_client_name(update, context)
    context.user_data["order"] = {
        "client_name": name,
        "route_points": [],
        "comment": "—",
        "special_request": None,
    }
    await update.effective_message.reply_text(
        "Укажите точку подачи:",
        reply_markup=LOCATION_KB,
    )
    return ORDER_FROM


async def order_from(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = clean_text(update.effective_message.text)
    if text == "❌ Отмена":
        return await cancel(update, context)
    if text == "✍️ Ввести адрес":
        await update.effective_message.reply_text(
            "Напишите точный адрес подачи:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ORDER_FROM

    point = point_from_message(update.effective_message)
    if not point:
        await update.effective_message.reply_text("Отправьте геопозицию или напишите адрес.")
        return ORDER_FROM

    context.user_data["order"]["route_points"] = [point]
    await update.effective_message.reply_text(
        "Укажите конечную точку:",
        reply_markup=LOCATION_KB,
    )
    return ORDER_TO


async def order_to(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = clean_text(update.effective_message.text)
    if text == "❌ Отмена":
        return await cancel(update, context)
    if text == "✍️ Ввести адрес":
        await update.effective_message.reply_text(
            "Напишите точный адрес назначения:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ORDER_TO

    point = point_from_message(update.effective_message)
    if not point:
        await update.effective_message.reply_text("Отправьте геопозицию или напишите адрес.")
        return ORDER_TO

    context.user_data["order"]["route_points"].append(point)
    await update.effective_message.reply_text(
        "Маршрут:\n" + route_text(context.user_data["order"]["route_points"]),
        reply_markup=ROUTE_MENU_KB,
    )
    return ORDER_ROUTE_MENU


async def order_route_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = clean_text(update.effective_message.text)
    order = context.user_data["order"]

    if text == "❌ Отмена":
        return await cancel(update, context)

    if text == "➕ Добавить точку":
        await update.effective_message.reply_text(
            "Отправьте геопозицию или напишите адрес дополнительной точки:",
            reply_markup=LOCATION_KB,
        )
        return ORDER_EXTRA_POINT

    if text == "🗑 Удалить последнюю точку":
        if len(order["route_points"]) <= 2:
            await update.effective_message.reply_text(
                "Нельзя удалить точку подачи или конечную точку."
            )
        else:
            order["route_points"].pop(-2)
            await update.effective_message.reply_text(
                "Точка удалена.\n\nМаршрут:\n" + route_text(order["route_points"]),
                reply_markup=ROUTE_MENU_KB,
            )
        return ORDER_ROUTE_MENU

    if text == "✅ Маршрут готов":
        await update.effective_message.reply_text(
            "Когда нужна машина?",
            reply_markup=TIME_KB,
        )
        return ORDER_TIME

    await update.effective_message.reply_text("Выберите действие кнопкой.")
    return ORDER_ROUTE_MENU


async def order_extra_point(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = clean_text(update.effective_message.text)
    if text == "❌ Отмена":
        return await cancel(update, context)
    if text == "✍️ Ввести адрес":
        await update.effective_message.reply_text(
            "Напишите адрес дополнительной точки:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ORDER_EXTRA_POINT

    point = point_from_message(update.effective_message)
    if not point:
        await update.effective_message.reply_text("Отправьте геопозицию или напишите адрес.")
        return ORDER_EXTRA_POINT

    points = context.user_data["order"]["route_points"]
    points.insert(len(points) - 1, point)
    await update.effective_message.reply_text(
        "Точка добавлена.\n\nМаршрут:\n" + route_text(points),
        reply_markup=ROUTE_MENU_KB,
    )
    return ORDER_ROUTE_MENU


async def order_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = clean_text(update.effective_message.text, 120)
    if text == "❌ Отмена":
        return await cancel(update, context)
    if text == "Указать дату и время":
        await update.effective_message.reply_text(
            "Напишите дату и время строго в формате:\n18.07.2026 18:00",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ORDER_TIME

    try:
        scheduled = parse_order_datetime(text)
    except (ValueError, OverflowError) as exc:
        await update.effective_message.reply_text(
            f"Не удалось определить дату и время: {exc}\n"
            "Используйте формат 18.07.2026 18:00."
        )
        return ORDER_TIME

    order = context.user_data["order"]
    order["scheduled_at"] = scheduled.isoformat()
    order["scheduled_text"] = format_dt(scheduled)

    await update.effective_message.reply_text(
        "Выберите класс:",
        reply_markup=CLASS_KB,
    )
    return ORDER_CLASS


async def order_class(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = clean_text(update.effective_message.text, 30)
    if text == "❌ Отмена":
        return await cancel(update, context)
    if text not in ORDER_CLASSES:
        await update.effective_message.reply_text("Выберите класс кнопкой.")
        return ORDER_CLASS

    context.user_data["order"]["car_class"] = text
    await update.effective_message.reply_text(
        "Выберите тариф:",
        reply_markup=TARIFF_KB,
    )
    return ORDER_TARIFF


async def order_tariff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = clean_text(update.effective_message.text, 40)
    if text == "❌ Отмена":
        return await cancel(update, context)
    if text not in {"Разовая поездка", "Почасовая", "Аэропорт", "Бизнес-день"}:
        await update.effective_message.reply_text("Выберите тариф кнопкой.")
        return ORDER_TARIFF

    order = context.user_data["order"]
    order["tariff"] = text

    if text == "Почасовая":
        rate = HOURLY_RATES.get(order.get("car_class"))
        rate_text = f"{rate:,} ₽/час".replace(",", " ") if rate else "По договорённости"
        await update.effective_message.reply_text(
            f"Тариф: {rate_text}\nСколько часов?",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ORDER_HOURS

    order["price"] = calculate_price(order)
    await update.effective_message.reply_text(
        f"Стоимость: {order['price']}\n"
        "Напишите комментарий или нажмите «Пропустить»:",
        reply_markup=SKIP_KB,
    )
    return ORDER_COMMENT


async def order_hours(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if clean_text(update.effective_message.text) == "❌ Отмена":
        return await cancel(update, context)

    match = re.search(r"\d+", update.effective_message.text or "")
    if not match:
        await update.effective_message.reply_text("Укажите количество часов числом.")
        return ORDER_HOURS

    hours = int(match.group())
    if not 1 <= hours <= 24:
        await update.effective_message.reply_text("Допустимо от 1 до 24 часов.")
        return ORDER_HOURS

    order = context.user_data["order"]
    order["hours"] = hours
    order["price"] = calculate_price(order)

    await update.effective_message.reply_text(
        f"Стоимость: {order['price']}\n"
        "Напишите комментарий или нажмите «Пропустить»:",
        reply_markup=SKIP_KB,
    )
    return ORDER_COMMENT


async def order_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = clean_text(update.effective_message.text, 500)
    if text == "❌ Отмена":
        return await cancel(update, context)

    order = context.user_data["order"]
    order["comment"] = "—" if text.lower() == "пропустить" else text
    order["price"] = calculate_price(order)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Отправить", callback_data="order_send"),
                InlineKeyboardButton("❌ Отмена", callback_data="order_cancel"),
            ]
        ]
    )
    await update.effective_message.reply_text(
        order_summary(order),
        reply_markup=keyboard,
    )
    return ORDER_CONFIRM


async def order_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "order_cancel":
        context.user_data.pop("order", None)
        await query.edit_message_text("Заказ отменён.")
        return ConversationHandler.END

    order = context.user_data.pop("order", None)
    if not order:
        await query.edit_message_text("Черновик заказа не найден.")
        return ConversationHandler.END

    await publish_order(context, query.from_user.id, order, query)
    return ConversationHandler.END


# ========================= ОСОБЫЙ ЗАПРОС =========================

async def special_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ensure_storage(context)
    name = save_client_name(update, context)
    context.user_data["order"] = {
        "client_name": name,
        "route_points": [],
        "comment": "—",
        "special_request": None,
        "tariff": "Особый запрос",
        "car_class": "Неважно",
        "price": "По договорённости",
    }
    await update.effective_message.reply_text(
        "Выберите тип особого запроса:",
        reply_markup=SPECIAL_KIND_KB,
    )
    return SPECIAL_KIND


async def special_kind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = clean_text(update.effective_message.text, 200)
    if text == "❌ Отмена":
        return await cancel(update, context)

    context.user_data["order"]["special_request"] = text
    await update.effective_message.reply_text(
        "Укажите место подачи:",
        reply_markup=LOCATION_KB,
    )
    return SPECIAL_FROM


async def special_from(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = clean_text(update.effective_message.text)
    if text == "❌ Отмена":
        return await cancel(update, context)
    if text == "✍️ Ввести адрес":
        await update.effective_message.reply_text(
            "Напишите адрес подачи:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return SPECIAL_FROM

    point = point_from_message(update.effective_message)
    if not point:
        await update.effective_message.reply_text("Отправьте геопозицию или напишите адрес.")
        return SPECIAL_FROM

    context.user_data["order"]["route_points"] = [point]
    await update.effective_message.reply_text(
        "Укажите конечную точку или место проведения:",
        reply_markup=LOCATION_KB,
    )
    return SPECIAL_TO


async def special_to(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = clean_text(update.effective_message.text)
    if text == "❌ Отмена":
        return await cancel(update, context)
    if text == "✍️ Ввести адрес":
        await update.effective_message.reply_text(
            "Напишите адрес назначения:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return SPECIAL_TO

    point = point_from_message(update.effective_message)
    if not point:
        await update.effective_message.reply_text("Отправьте геопозицию или напишите адрес.")
        return SPECIAL_TO

    context.user_data["order"]["route_points"].append(point)
    await update.effective_message.reply_text(
        "Когда нужна машина?",
        reply_markup=TIME_KB,
    )
    return SPECIAL_TIME


async def special_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = clean_text(update.effective_message.text, 120)
    if text == "❌ Отмена":
        return await cancel(update, context)
    if text == "Указать дату и время":
        await update.effective_message.reply_text(
            "Напишите дату и время строго в формате:\n18.07.2026 18:00",
            reply_markup=ReplyKeyboardRemove(),
        )
        return SPECIAL_TIME

    try:
        scheduled = parse_order_datetime(text)
    except (ValueError, OverflowError) as exc:
        await update.effective_message.reply_text(
            f"Не удалось определить дату и время: {exc}\n"
            "Используйте формат 18.07.2026 18:00."
        )
        return SPECIAL_TIME

    order = context.user_data["order"]
    order["scheduled_at"] = scheduled.isoformat()
    order["scheduled_text"] = format_dt(scheduled)

    await update.effective_message.reply_text(
        "Опишите детали запроса или нажмите «Пропустить»:",
        reply_markup=SKIP_KB,
    )
    return SPECIAL_COMMENT


async def special_comment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = clean_text(update.effective_message.text, 700)
    if text == "❌ Отмена":
        return await cancel(update, context)

    order = context.user_data["order"]
    order["comment"] = "—" if text.lower() == "пропустить" else text

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Отправить", callback_data="special_send"),
                InlineKeyboardButton("❌ Отмена", callback_data="special_cancel"),
            ]
        ]
    )
    await update.effective_message.reply_text(
        order_summary(order),
        reply_markup=keyboard,
    )
    return SPECIAL_CONFIRM


async def special_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "special_cancel":
        context.user_data.pop("order", None)
        await query.edit_message_text("Особый запрос отменён.")
        return ConversationHandler.END

    order = context.user_data.pop("order", None)
    if not order:
        await query.edit_message_text("Черновик запроса не найден.")
        return ConversationHandler.END

    await publish_order(context, query.from_user.id, order, query)
    return ConversationHandler.END


# ========================= ПУБЛИКАЦИЯ ЗАКАЗА =========================

async def publish_order(
    context: ContextTypes.DEFAULT_TYPE,
    client_id: int,
    order: dict[str, Any],
    query,
) -> None:
    ensure_storage(context)

    required = ["route_points", "scheduled_at", "scheduled_text", "car_class", "tariff"]
    missing = [field for field in required if not order.get(field)]
    if len(order.get("route_points", [])) < 2:
        missing.append("route_points")

    if missing:
        await query.edit_message_text(
            "Заказ не отправлен: не заполнены обязательные данные."
        )
        return

    order_id = uuid.uuid4().hex[:8].upper()
    order.update(
        {
            "id": order_id,
            "client_id": client_id,
            "status": "open",
            "created_at": now_iso(),
            "accepted_at": None,
            "arrived_at": None,
            "started_at": None,
            "completed_at": None,
            "driver_id": None,
        }
    )

    context.bot_data["orders"][order_id] = order
    context.bot_data["open_by_client"][str(client_id)] = order_id

    if order.get("special_request"):
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🚘 Предложить автомобиль", callback_data=f"offer_{order_id}")]]
        )
    else:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🟢 Взять заказ", callback_data=f"take_{order_id}")]]
        )

    group_message = await context.bot.send_message(
        ORDERS_CHAT_ID,
        order_public_text(order_id, order),
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )
    order["group_message_id"] = group_message.message_id
    track_message(context, order_id, ORDERS_CHAT_ID, group_message.message_id)

    await query.edit_message_text(
        f"✅ Заказ №{order_id} отправлен водителям.",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton(
                    "❌ Отменить заказ",
                    callback_data=f"cancelopen_{order_id}",
                )
            ]]
        ),
    )


# ========================= РЕГИСТРАЦИЯ ВОДИТЕЛЯ =========================

async def reg_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ensure_storage(context)
    current = context.bot_data["drivers"].get(str(update.effective_user.id))
    if current and current.get("status") in {"approved", "blocked"}:
        await update.effective_message.reply_text(
            f"Вы уже зарегистрированы.\nСтатус: {driver_status_text(current)}"
        )
        return ConversationHandler.END

    context.user_data["reg"] = {
        "document_photos": [],
        "car_photos": [],
    }
    await update.effective_message.reply_text(
        "Введите ФИО:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return REG_NAME


async def reg_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = clean_text(update.effective_message.text, 120)
    if text == "❌ Отмена":
        return await cancel(update, context)
    context.user_data["reg"]["name"] = text
    await update.effective_message.reply_text(
        "Отправьте номер телефона:",
        reply_markup=PHONE_KB,
    )
    return REG_PHONE


async def reg_phone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if clean_text(update.effective_message.text) == "❌ Отмена":
        return await cancel(update, context)

    raw = (
        update.effective_message.contact.phone_number
        if update.effective_message.contact
        else update.effective_message.text
    )
    phone = normalize_phone(raw)
    if not phone:
        await update.effective_message.reply_text("Не удалось распознать номер.")
        return REG_PHONE

    context.user_data["reg"]["phone"] = phone
    await update.effective_message.reply_text(
        "Марка и модель автомобиля:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return REG_CAR


async def reg_car(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["reg"]["car"] = clean_text(update.effective_message.text, 120)
    await update.effective_message.reply_text("Год выпуска:")
    return REG_YEAR


async def reg_year(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    year = clean_text(update.effective_message.text, 10)
    if not re.fullmatch(r"\d{4}", year):
        await update.effective_message.reply_text("Введите год четырьмя цифрами.")
        return REG_YEAR
    context.user_data["reg"]["year"] = year
    await update.effective_message.reply_text("Госномер:")
    return REG_PLATE


async def reg_plate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["reg"]["plate"] = clean_text(
        update.effective_message.text.upper(), 20
    )
    await update.effective_message.reply_text(
        "Класс автомобиля:",
        reply_markup=DRIVER_CLASS_KB,
    )
    return REG_CLASS


async def reg_class(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = clean_text(update.effective_message.text, 30)
    if text == "❌ Отмена":
        return await cancel(update, context)
    if text not in CAR_CLASSES:
        await update.effective_message.reply_text("Выберите класс кнопкой.")
        return REG_CLASS

    context.user_data["reg"]["car_class"] = text
    await update.effective_message.reply_text(
        "Отправьте фотографии водительского удостоверения и СТС.\n"
        "После загрузки нажмите «Готово».",
        reply_markup=DONE_KB,
    )
    return REG_DOCS


async def reg_docs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reg = context.user_data["reg"]

    if update.effective_message.photo:
        reg["document_photos"].append(update.effective_message.photo[-1].file_id)
        await update.effective_message.reply_text(
            f"Документ добавлен: {len(reg['document_photos'])}"
        )
        return REG_DOCS

    text = clean_text(update.effective_message.text)
    if text == "❌ Отмена":
        return await cancel(update, context)

    if text.lower() == "готово":
        if len(reg["document_photos"]) < 2:
            await update.effective_message.reply_text("Нужно минимум 2 фотографии документов.")
            return REG_DOCS

        await update.effective_message.reply_text(
            "Теперь отправьте от 2 до 6 фотографий автомобиля:\n"
            "кузов и салон.\n"
            "Именно эти фотографии получит клиент.\n"
            "После загрузки нажмите «Готово».",
            reply_markup=DONE_KB,
        )
        return REG_CAR_PHOTOS

    return REG_DOCS


async def reg_car_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    reg = context.user_data["reg"]

    if update.effective_message.photo:
        if len(reg["car_photos"]) >= 6:
            await update.effective_message.reply_text("Максимум 6 фотографий автомобиля.")
            return REG_CAR_PHOTOS
        reg["car_photos"].append(update.effective_message.photo[-1].file_id)
        await update.effective_message.reply_text(
            f"Фото автомобиля добавлено: {len(reg['car_photos'])}"
        )
        return REG_CAR_PHOTOS

    text = clean_text(update.effective_message.text)
    if text == "❌ Отмена":
        return await cancel(update, context)

    if text.lower() == "готово":
        if len(reg["car_photos"]) < 2:
            await update.effective_message.reply_text(
                "Нужно минимум 2 фотографии автомобиля."
            )
            return REG_CAR_PHOTOS

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Отправить", callback_data="reg_send"),
                    InlineKeyboardButton("❌ Отмена", callback_data="reg_cancel"),
                ]
            ]
        )
        await update.effective_message.reply_text(
            "Отправить анкету модератору?",
            reply_markup=keyboard,
        )
        return REG_CONFIRM

    return REG_CAR_PHOTOS


async def reg_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    ensure_storage(context)
    query = update.callback_query
    await query.answer()

    if query.data == "reg_cancel":
        context.user_data.pop("reg", None)
        await query.edit_message_text("Регистрация отменена.")
        return ConversationHandler.END

    reg = context.user_data.pop("reg")
    application_id = uuid.uuid4().hex[:8].upper()
    application = {
        **reg,
        "user_id": query.from_user.id,
        "status": "pending",
        "created_at": now_iso(),
    }
    context.bot_data["driver_apps"][application_id] = application

    text = (
        f"👨‍✈️ <b>АНКЕТА №{application_id}</b>\n\n"
        f"ФИО: {esc(application['name'])}\n"
        f"Телефон: {esc(application['phone'])}\n"
        f"Авто: {esc(application['car'])} {esc(application['year'])}\n"
        f"Номер: {esc(application['plate'])}\n"
        f"Класс: {esc(application['car_class'])}\n"
        f"Документы: {len(application['document_photos'])}\n"
        f"Фото авто: {len(application['car_photos'])}"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{application_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{application_id}"),
            ]
        ]
    )

    await context.bot.send_message(
        MODERATION_CHAT_ID,
        text,
        parse_mode="HTML",
        reply_markup=keyboard,
    )

    for photo_id in application["document_photos"]:
        await context.bot.send_photo(
            MODERATION_CHAT_ID,
            photo=photo_id,
            caption=f"{application_id}: документ",
        )

    for photo_id in application["car_photos"]:
        await context.bot.send_photo(
            MODERATION_CHAT_ID,
            photo=photo_id,
            caption=f"{application_id}: автомобиль",
        )

    await query.edit_message_text("✅ Анкета отправлена модератору.")
    return ConversationHandler.END


# ========================= МОДЕРАЦИЯ =========================

async def moderate_driver(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_storage(context)
    query = update.callback_query
    await query.answer()

    action, application_id = query.data.split("_", 1)
    application = context.bot_data["driver_apps"].get(application_id)

    if not application or application.get("status") != "pending":
        await query.answer("Анкета уже обработана.", show_alert=True)
        return

    if action == "reject":
        application["status"] = "rejected"
        await query.edit_message_text((query.message.text or "") + "\n\n❌ ОТКЛОНЕНО")
        await context.bot.send_message(
            application["user_id"],
            "❌ Ваша заявка водителя отклонена.",
        )
        return

    driver = {
        **application,
        "status": "approved",
        "approved_at": now_iso(),
    }
    context.bot_data["drivers"][str(application["user_id"])] = driver
    application["status"] = "approved"

    manage_keyboard = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton(
                "🚫 Заблокировать",
                callback_data=f"blockdrv_{application['user_id']}",
            ),
            InlineKeyboardButton(
                "🗑 Удалить",
                callback_data=f"deletedrv_{application['user_id']}",
            ),
        ]]
    )
    await query.edit_message_text(
        (query.message.text or "") + "\n\n✅ ОДОБРЕНО",
        reply_markup=manage_keyboard,
    )
    await context.bot.send_message(
        application["user_id"],
        "✅ Ваша заявка одобрена. Теперь вы можете принимать заказы.",
        reply_markup=MAIN_KB,
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_moderator_chat(update):
        await update.effective_message.reply_text("Команда доступна только в чате модерации.")
        return
    await update.effective_message.reply_text(
        "Панель модератора:",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("👨‍✈️ Показать водителей", callback_data="show_drivers")]]
        ),
    )


async def show_drivers_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    # Используем сообщение callback как обычное сообщение для вывода списка.
    class ProxyUpdate:
        effective_chat = query.message.chat
        effective_message = query.message
    await drivers_command(ProxyUpdate(), context)


def driver_manage_keyboard(uid: str, status: str) -> Optional[InlineKeyboardMarkup]:
    buttons: list[InlineKeyboardButton] = []

    if status == "approved":
        buttons.append(
            InlineKeyboardButton(
                "🚫 Заблокировать",
                callback_data=f"blockdrv_{uid}",
            )
        )
    elif status == "blocked":
        buttons.append(
            InlineKeyboardButton(
                "♻️ Разблокировать",
                callback_data=f"unblockdrv_{uid}",
            )
        )

    if status != "deleted":
        buttons.append(
            InlineKeyboardButton(
                "🗑 Удалить",
                callback_data=f"deletedrv_{uid}",
            )
        )

    return InlineKeyboardMarkup([buttons]) if buttons else None


async def drivers_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_storage(context)

    if not is_moderator_chat(update):
        await update.effective_message.reply_text("Команда доступна только модераторам.")
        return

    drivers = context.bot_data["drivers"]
    if not drivers:
        await update.effective_message.reply_text("Водителей пока нет.")
        return

    for uid, driver in drivers.items():
        text = (
            f"👨‍✈️ <b>{esc(driver.get('name'))}</b>\n"
            f"ID: <code>{esc(uid)}</code>\n"
            f"Статус: {esc(driver_status_text(driver))}\n"
            f"Авто: {esc(driver.get('car'))} {esc(driver.get('year'))}\n"
            f"Номер: {esc(driver.get('plate'))}\n"
            f"Класс: {esc(driver.get('car_class'))}\n"
            f"Фото авто: {len(driver.get('car_photos', []))}"
        )

        await update.effective_message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=driver_manage_keyboard(uid, driver.get("status", "")),
        )


async def manage_driver(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_storage(context)
    query = update.callback_query
    await query.answer()

    match = re.fullmatch(r"(blockdrv|unblockdrv|deletedrv)_(\d+)", query.data)
    if not match:
        return

    action, uid = match.groups()
    driver = context.bot_data["drivers"].get(uid)
    if not driver:
        await query.answer("Водитель не найден.", show_alert=True)
        return

    if action == "blockdrv":
        driver["status"] = "blocked"
        driver["blocked_at"] = now_iso()
        await context.bot.send_message(
            int(uid),
            "🚫 Ваш доступ к новым заказам заблокирован модератором.",
        )
        result = "Водитель заблокирован."

    elif action == "unblockdrv":
        driver["status"] = "approved"
        driver["unblocked_at"] = now_iso()
        await context.bot.send_message(
            int(uid),
            "♻️ Ваш доступ к заказам восстановлен.",
        )
        result = "Водитель разблокирован."

    else:
        driver["status"] = "deleted"
        driver["deleted_at"] = now_iso()
        await context.bot.send_message(
            int(uid),
            "🗑 Ваша анкета водителя удалена модератором.",
        )
        result = "Водитель удалён."

    card_text = (
        f"👨‍✈️ <b>{esc(driver.get('name'))}</b>\n"
        f"ID: <code>{esc(uid)}</code>\n"
        f"Статус: {esc(driver_status_text(driver))}\n"
        f"Авто: {esc(driver.get('car'))} {esc(driver.get('year'))}\n"
        f"Номер: {esc(driver.get('plate'))}\n"
        f"Класс: {esc(driver.get('car_class'))}\n"
        f"Фото авто: {len(driver.get('car_photos', []))}"
    )

    await query.edit_message_text(
        card_text,
        parse_mode="HTML",
        reply_markup=driver_manage_keyboard(uid, driver.get("status", "")),
    )
    await query.answer(result, show_alert=True)


# ========================= ПРИНЯТИЕ И СТАТУСЫ ЗАКАЗА =========================

async def assign_driver_to_order(
    context: ContextTypes.DEFAULT_TYPE,
    order_id: str,
    order: dict[str, Any],
    driver_id: int,
    source_message_id: Optional[int] = None,
) -> None:
    driver = context.bot_data["drivers"][str(driver_id)]
    order.update(
        {
            "status": "taken",
            "driver_id": driver_id,
            "accepted_at": now_iso(),
        }
    )
    context.bot_data["open_by_client"].pop(str(order["client_id"]), None)
    context.bot_data["active_by_user"][str(order["client_id"])] = order_id
    context.bot_data["active_by_user"][str(driver_id)] = order_id

    if source_message_id:
        try:
            await context.bot.edit_message_reply_markup(
                chat_id=ORDERS_CHAT_ID,
                message_id=source_message_id,
                reply_markup=None,
            )
        except TelegramError:
            pass

    driver_keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📍 Я на месте", callback_data=f"arrived_{order_id}")],
            [InlineKeyboardButton("▶️ Начать поездку", callback_data=f"starttrip_{order_id}")],
            [InlineKeyboardButton("✅ Завершить", callback_data=f"finish_{order_id}")],
        ]
    )
    await tracked_send_message(
        context,
        order_id,
        driver_id,
        order_public_text(order_id, order),
        parse_mode="HTML",
        reply_markup=driver_keyboard,
        disable_web_page_preview=True,
    )

    client_keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📸 Запросить фото машины", callback_data=f"photos_{order_id}")],
            [InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancelactive_{order_id}")],
        ]
    )
    await tracked_send_message(
        context,
        order_id,
        order["client_id"],
        (
            f"🚘 Водитель принял заказ №{order_id}.\n"
            f"Водитель: {driver.get('name', '—')}\n"
            f"Автомобиль: {driver.get('car', '—')} {driver.get('year', '')}\n"
            f"Госномер: {driver.get('plate', '—')}\n"
            f"Класс: {driver.get('car_class', '—')}\n"
            f"Принят: {format_clock(order.get('accepted_at'))}"
        ),
        reply_markup=client_keyboard,
    )


async def take_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_storage(context)
    query = update.callback_query
    await query.answer()

    order_id = query.data.removeprefix("take_")
    order = context.bot_data["orders"].get(order_id)
    driver = context.bot_data["drivers"].get(str(query.from_user.id))

    if not order or order.get("status") != "open":
        await query.answer("Заказ уже недоступен.", show_alert=True)
        return
    if order.get("special_request"):
        await query.answer("Для особого запроса предложите автомобиль.", show_alert=True)
        return
    if order.get("client_id") == query.from_user.id:
        await query.answer("Нельзя принять собственный заказ.", show_alert=True)
        return
    if not driver:
        await query.answer("Вы не зарегистрированы как водитель.", show_alert=True)
        return
    if driver.get("status") != "approved":
        await query.answer(
            f"Доступ запрещён. Статус: {driver_status_text(driver)}.",
            show_alert=True,
        )
        return
    if order.get("car_class") not in {"Неважно", driver.get("car_class")}:
        await query.answer("Класс автомобиля не подходит.", show_alert=True)
        return
    if context.bot_data["active_by_user"].get(str(query.from_user.id)):
        await query.answer("У вас уже есть активный заказ.", show_alert=True)
        return

    await assign_driver_to_order(
        context,
        order_id,
        order,
        query.from_user.id,
        query.message.message_id,
    )
    await query.answer("Заказ принят.")


async def offer_special_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_storage(context)
    query = update.callback_query
    await query.answer()

    order_id = query.data.removeprefix("offer_")
    order = context.bot_data["orders"].get(order_id)
    driver = context.bot_data["drivers"].get(str(query.from_user.id))

    if not order or order.get("status") != "open" or not order.get("special_request"):
        await query.answer("Особый запрос уже недоступен.", show_alert=True)
        return
    if order.get("client_id") == query.from_user.id:
        await query.answer("Нельзя предложить машину на собственный заказ.", show_alert=True)
        return
    if not driver or driver.get("status") != "approved":
        await query.answer("Доступно только активным водителям.", show_alert=True)
        return
    if context.bot_data["active_by_user"].get(str(query.from_user.id)):
        await query.answer("У вас уже есть активный заказ.", show_alert=True)
        return

    offers = order.setdefault("offers", {})
    uid = str(query.from_user.id)
    if uid in offers and offers[uid].get("status") == "pending":
        await query.answer("Вы уже предложили автомобиль.", show_alert=True)
        return

    offer_number = 1 + sum(
        1 for item in offers.values() if item.get("created_at")
    )
    offers[uid] = {
        "driver_id": query.from_user.id,
        "status": "pending",
        "created_at": now_iso(),
        "number": offer_number,
    }

    offer_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📸 Посмотреть фото",
                    callback_data=f"offerphotos_{order_id}_{uid}",
                )
            ],
            [
                InlineKeyboardButton(
                    "✅ Выбрать эту машину",
                    callback_data=f"chooseoffer_{order_id}_{uid}",
                )
            ],
            [
                InlineKeyboardButton(
                    "🔄 Не подходит — запросить другую",
                    callback_data=f"rejectoffer_{order_id}_{uid}",
                )
            ],
        ]
    )
    offer_message = await tracked_send_message(
        context,
        order_id,
        order["client_id"],
        (
            f"🚘 Предложение №{offer_number} по особому запросу №{order_id}\n\n"
            f"Автомобиль: {driver.get('car', '—')} {driver.get('year', '')}\n"
            f"Госномер: {driver.get('plate', '—')}\n"
            f"Класс: {driver.get('car_class', '—')}\n\n"
            "Посмотрите фотографии и выберите автомобиль. "
            "Если он не подходит, запрос останется открытым для других водителей."
        ),
        reply_markup=offer_keyboard,
    )
    offers[uid]["client_message_id"] = offer_message.message_id
    await query.answer("Предложение отправлено клиенту.")


async def request_car_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_storage(context)
    query = update.callback_query
    await query.answer()

    order_id = query.data.removeprefix("photos_")
    order = context.bot_data["orders"].get(order_id)
    if not order or order.get("client_id") != query.from_user.id:
        await query.answer("Фотографии недоступны.", show_alert=True)
        return

    driver = context.bot_data["drivers"].get(str(order.get("driver_id")))
    photos = (driver or {}).get("car_photos") or []
    if not photos:
        await query.answer("У водителя нет сохранённых фото.", show_alert=True)
        return

    for photo_id in photos:
        await tracked_send_photo(context, order_id, order["client_id"], photo_id)
    await query.answer("Фотографии отправлены.")


async def request_offer_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_storage(context)
    query = update.callback_query
    await query.answer()

    match = re.fullmatch(r"offerphotos_([A-F0-9]{8})_(\d+)", query.data)
    if not match:
        return
    order_id, driver_uid = match.groups()
    order = context.bot_data["orders"].get(order_id)
    if not order or order.get("client_id") != query.from_user.id:
        await query.answer("Предложение недоступно.", show_alert=True)
        return
    offer = order.get("offers", {}).get(driver_uid)
    if not offer or offer.get("status") != "pending":
        await query.answer("Предложение уже закрыто.", show_alert=True)
        return

    driver = context.bot_data["drivers"].get(driver_uid)
    photos = (driver or {}).get("car_photos") or []
    if not photos:
        await query.answer("У этой машины нет сохранённых фото.", show_alert=True)
        return
    for photo_id in photos:
        await tracked_send_photo(context, order_id, order["client_id"], photo_id)
    await query.answer("Фотографии отправлены.")


async def choose_special_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_storage(context)
    query = update.callback_query
    await query.answer()

    match = re.fullmatch(r"chooseoffer_([A-F0-9]{8})_(\d+)", query.data)
    if not match:
        return
    order_id, driver_uid = match.groups()
    order = context.bot_data["orders"].get(order_id)

    if not order or order.get("client_id") != query.from_user.id:
        await query.answer("Предложение недоступно.", show_alert=True)
        return
    if order.get("status") != "open":
        await query.answer("Машина уже выбрана.", show_alert=True)
        return

    offer = order.get("offers", {}).get(driver_uid)
    driver = context.bot_data["drivers"].get(driver_uid)
    if not offer or offer.get("status") != "pending":
        await query.answer("Предложение уже закрыто.", show_alert=True)
        return
    if not driver or driver.get("status") != "approved":
        await query.answer("Водитель больше недоступен.", show_alert=True)
        return
    if context.bot_data["active_by_user"].get(driver_uid):
        await query.answer("Этот водитель уже занят.", show_alert=True)
        return

    offer["status"] = "selected"
    for uid, other_offer in order.get("offers", {}).items():
        if uid != driver_uid and other_offer.get("status") == "pending":
            other_offer["status"] = "rejected"

    await assign_driver_to_order(
        context,
        order_id,
        order,
        int(driver_uid),
        order.get("group_message_id"),
    )
    await query.edit_message_text(
        f"✅ Вы выбрали {driver.get('car', 'автомобиль')}.\n"
        "Остальные предложения закрыты."
    )


async def reject_special_offer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_storage(context)
    query = update.callback_query
    await query.answer()

    match = re.fullmatch(r"rejectoffer_([A-F0-9]{8})_(\d+)", query.data)
    if not match:
        return
    order_id, driver_uid = match.groups()
    order = context.bot_data["orders"].get(order_id)

    if not order or order.get("client_id") != query.from_user.id:
        await query.answer("Предложение недоступно.", show_alert=True)
        return
    offer = order.get("offers", {}).get(driver_uid)
    if not offer or offer.get("status") != "pending":
        await query.answer("Предложение уже закрыто.", show_alert=True)
        return

    offer["status"] = "rejected"
    offer["rejected_at"] = now_iso()

    driver = context.bot_data["drivers"].get(driver_uid)
    try:
        await query.delete_message()
    except TelegramError:
        await query.edit_message_text(
            "🔄 Это предложение отклонено. Запрос остаётся открытым."
        )

    await context.bot.send_message(
        int(driver_uid),
        f"Клиент отклонил ваше предложение по запросу №{order_id}.",
    )

    await tracked_send_message(
        context,
        order_id,
        order["client_id"],
        (
            "🔄 Машина не выбрана. Особый запрос остаётся открытым.\n"
            "Другие активные водители могут предложить свои автомобили."
        ),
    )


async def cancel_open_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_storage(context)
    query = update.callback_query
    await query.answer()

    order_id = query.data.removeprefix("cancelopen_")
    order = context.bot_data["orders"].get(order_id)

    if not order:
        await query.answer("Заказ не найден.", show_alert=True)
        return

    if order.get("client_id") != query.from_user.id:
        await query.answer("Отменить заказ может только клиент.", show_alert=True)
        return

    if order.get("status") != "open":
        await query.answer(
            "Заказ уже принят или закрыт.",
            show_alert=True,
        )
        return

    order["status"] = "cancelled"
    order["cancelled_at"] = now_iso()
    order["cancelled_by"] = query.from_user.id
    context.bot_data["open_by_client"].pop(str(query.from_user.id), None)

    # Удаляем карточку заказа из водительской группы.
    group_message_id = order.get("group_message_id")
    if group_message_id:
        try:
            await context.bot.delete_message(
                chat_id=ORDERS_CHAT_ID,
                message_id=group_message_id,
            )
        except TelegramError:
            logger.exception("Не удалось удалить отменённый заказ из группы")

    await query.edit_message_text(
        f"❌ Заказ №{order_id} отменён."
    )


async def cancel_active_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_storage(context)

    if update.callback_query:
        query = update.callback_query
        await query.answer()
        order_id = query.data.removeprefix("cancelactive_")
        actor_id = query.from_user.id
    else:
        order_id = context.bot_data["active_by_user"].get(str(update.effective_user.id))
        actor_id = update.effective_user.id
        query = None

    order = context.bot_data["orders"].get(order_id) if order_id else None
    if not order or order.get("status") not in {"taken", "in_progress"}:
        if query:
            await query.answer("Активный заказ не найден.", show_alert=True)
        return
    if actor_id not in {order.get("client_id"), order.get("driver_id")}:
        if query:
            await query.answer("Нет доступа к заказу.", show_alert=True)
        return

    order["status"] = "cancelled"
    order["cancelled_at"] = now_iso()
    order["cancelled_by"] = actor_id
    await clear_active_order(context, order)
    await cleanup_order_messages(context, order_id)

    await send_once(
        context,
        [order.get("client_id"), order.get("driver_id")],
        f"❌ Заказ №{order_id} отменён.",
        reply_markup=MAIN_KB,
    )

def validate_driver_action(
    query,
    order: Optional[dict[str, Any]],
) -> Optional[str]:
    if not order:
        return "Заказ не найден."
    if order.get("driver_id") != query.from_user.id:
        return "Действие доступно только назначенному водителю."
    return None


async def arrived_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    order_id = query.data.removeprefix("arrived_")
    order = context.bot_data.get("orders", {}).get(order_id)

    error = validate_driver_action(query, order)
    if error:
        await query.answer(error, show_alert=True)
        return

    if order.get("arrived_at"):
        await query.answer("Статус уже установлен.", show_alert=True)
        return

    order["arrived_at"] = now_iso()
    await context.bot.send_message(
        order["client_id"],
        f"📍 Водитель на месте.\nВремя: {format_clock(order['arrived_at'])}",
    )
    await query.answer("Клиент уведомлён.")


async def start_trip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    order_id = query.data.removeprefix("starttrip_")
    order = context.bot_data.get("orders", {}).get(order_id)

    error = validate_driver_action(query, order)
    if error:
        await query.answer(error, show_alert=True)
        return

    if order.get("started_at"):
        await query.answer("Поездка уже началась.", show_alert=True)
        return

    order["started_at"] = now_iso()
    order["status"] = "in_progress"

    await send_once(
        context,
        [order["client_id"], order["driver_id"]],
        f"▶️ Поездка началась.\nВремя начала: {format_clock(order['started_at'])}",
    )
    await query.answer("Поездка началась.")


async def finish_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_storage(context)
    query = update.callback_query
    order_id = query.data.removeprefix("finish_")
    order = context.bot_data["orders"].get(order_id)

    error = validate_driver_action(query, order)
    if error:
        await query.answer(error, show_alert=True)
        return

    if order.get("completed_at"):
        await query.answer("Заказ уже завершён.", show_alert=True)
        return

    order["completed_at"] = now_iso()
    order["status"] = "completed"
    await clear_active_order(context, order)

    summary = (
        f"✅ Заказ №{order_id} завершён.\n"
        f"Принят: {format_clock(order.get('accepted_at'))}\n"
        f"Водитель на месте: {format_clock(order.get('arrived_at'))}\n"
        f"Начало поездки: {format_clock(order.get('started_at'))}\n"
        f"Завершение: {format_clock(order.get('completed_at'))}\n"
        f"Время поездки: {format_duration(order.get('started_at'), order.get('completed_at'))}"
    )

    await cleanup_order_messages(context, order_id)
    await send_once(
        context,
        [order["client_id"], order["driver_id"]],
        summary,
        reply_markup=MAIN_KB,
    )
    await query.answer("Заказ завершён.")


# ========================= ПЕРЕПИСКА =========================

async def relay_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_storage(context)
    user_id = update.effective_user.id
    order_id = context.bot_data["active_by_user"].get(str(user_id))
    if not order_id:
        return

    order = context.bot_data["orders"].get(order_id)
    if not order or order.get("status") not in {"taken", "in_progress"}:
        return

    if user_id == order.get("client_id"):
        recipient = order.get("driver_id")
    elif user_id == order.get("driver_id"):
        recipient = order.get("client_id")
    else:
        return

    if not recipient or recipient == user_id:
        return

    # Блокируем явную передачу контактов в текстовых сообщениях.
    text = update.effective_message.text or update.effective_message.caption or ""
    if text and (
        re.search(r"(?:\+?\d[\d\s\-()]{8,}\d)", text)
        or "t.me/" in text.lower()
        or "@" in text
    ):
        await update.effective_message.reply_text(
            "Передача телефонных номеров и Telegram-контактов через бот запрещена."
        )
        return

    # Сохраняем входящее сообщение для попытки очистки после закрытия заказа.
    track_message(
        context,
        order_id,
        update.effective_chat.id,
        update.effective_message.message_id,
    )

    try:
        copied = await context.bot.copy_message(
            chat_id=recipient,
            from_chat_id=update.effective_chat.id,
            message_id=update.effective_message.message_id,
        )
        track_message(context, order_id, recipient, copied.message_id)
    except TelegramError:
        logger.exception("Ошибка пересылки сообщения")


# ========================= ОШИБКИ И ЗАПУСК =========================

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_storage(context)
    user_id = update.effective_user.id

    # Сначала ищем опубликованный, но ещё не принятый заказ.
    order_id = context.bot_data["open_by_client"].get(str(user_id))
    if order_id:
        order = context.bot_data["orders"].get(order_id)
        if order and order.get("status") == "open":
            order["status"] = "cancelled"
            order["cancelled_at"] = now_iso()
            order["cancelled_by"] = user_id
            context.bot_data["open_by_client"].pop(str(user_id), None)

            group_message_id = order.get("group_message_id")
            if group_message_id:
                try:
                    await context.bot.delete_message(
                        chat_id=ORDERS_CHAT_ID,
                        message_id=group_message_id,
                    )
                except TelegramError:
                    logger.exception("Не удалось удалить отменённый заказ из группы")

            await update.effective_message.reply_text(
                f"❌ Заказ №{order_id} отменён.",
                reply_markup=MAIN_KB,
            )
            return

    # Затем ищем уже принятый активный заказ.
    active_id = context.bot_data["active_by_user"].get(str(user_id))
    if active_id:
        order = context.bot_data["orders"].get(active_id)
        if order and order.get("status") in {"taken", "in_progress"}:
            order["status"] = "cancelled"
            order["cancelled_at"] = now_iso()
            order["cancelled_by"] = user_id
            await clear_active_order(context, order)
            await cleanup_order_messages(context, active_id)
            await send_once(
                context,
                [order.get("client_id"), order.get("driver_id")],
                f"❌ Заказ №{active_id} отменён.",
                reply_markup=MAIN_KB,
            )
            return

    await update.effective_message.reply_text(
        "Активный или открытый заказ не найден.",
        reply_markup=MAIN_KB,
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Необработанная ошибка", exc_info=context.error)


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Переменная BOT_TOKEN не настроена")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(PicklePersistence(filepath=PERSISTENCE_PATH))
        .concurrent_updates(False)
        .build()
    )

    order_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("order", order_start),
            MessageHandler(filters.Regex(r"^🚖 Заказать поездку$"), order_start),
        ],
        states={
            ORDER_FROM: [
                MessageHandler((filters.TEXT | filters.LOCATION) & ~filters.COMMAND, order_from)
            ],
            ORDER_TO: [
                MessageHandler((filters.TEXT | filters.LOCATION) & ~filters.COMMAND, order_to)
            ],
            ORDER_ROUTE_MENU: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_route_menu)
            ],
            ORDER_EXTRA_POINT: [
                MessageHandler(
                    (filters.TEXT | filters.LOCATION) & ~filters.COMMAND,
                    order_extra_point,
                )
            ],
            ORDER_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_time)
            ],
            ORDER_CLASS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_class)
            ],
            ORDER_TARIFF: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_tariff)
            ],
            ORDER_HOURS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_hours)
            ],
            ORDER_COMMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_comment)
            ],
            ORDER_CONFIRM: [
                CallbackQueryHandler(order_confirm, pattern=r"^order_(send|cancel)$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        name="client_order_v5_2",
        persistent=True,
    )

    special_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^✨ Особый запрос$"), special_start),
        ],
        states={
            SPECIAL_KIND: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, special_kind)
            ],
            SPECIAL_FROM: [
                MessageHandler(
                    (filters.TEXT | filters.LOCATION) & ~filters.COMMAND,
                    special_from,
                )
            ],
            SPECIAL_TO: [
                MessageHandler(
                    (filters.TEXT | filters.LOCATION) & ~filters.COMMAND,
                    special_to,
                )
            ],
            SPECIAL_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, special_time)
            ],
            SPECIAL_COMMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, special_comment)
            ],
            SPECIAL_CONFIRM: [
                CallbackQueryHandler(
                    special_confirm,
                    pattern=r"^special_(send|cancel)$",
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        name="special_order_v5_2",
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
                MessageHandler(
                    (filters.CONTACT | filters.TEXT) & ~filters.COMMAND,
                    reg_phone,
                )
            ],
            REG_CAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_car)],
            REG_YEAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_year)],
            REG_PLATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_plate)],
            REG_CLASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, reg_class)],
            REG_DOCS: [
                MessageHandler((filters.PHOTO | filters.TEXT) & ~filters.COMMAND, reg_docs)
            ],
            REG_CAR_PHOTOS: [
                MessageHandler(
                    (filters.PHOTO | filters.TEXT) & ~filters.COMMAND,
                    reg_car_photos,
                )
            ],
            REG_CONFIRM: [
                CallbackQueryHandler(reg_confirm, pattern=r"^reg_(send|cancel)$")
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        name="driver_registration_v5_2",
        persistent=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("drivers", drivers_command))
    application.add_handler(CommandHandler("admin", admin_panel))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(
        MessageHandler(filters.Regex(r"^📋 Мой статус$"), my_status)
    )

    application.add_handler(order_conversation)
    application.add_handler(special_conversation)
    application.add_handler(registration_conversation)

    application.add_handler(
        CallbackQueryHandler(
            moderate_driver,
            pattern=r"^(approve|reject)_[A-F0-9]{8}$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            manage_driver,
            pattern=r"^(blockdrv|unblockdrv|deletedrv)_\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(show_drivers_callback, pattern=r"^show_drivers$")
    )
    application.add_handler(
        CallbackQueryHandler(take_order, pattern=r"^take_[A-F0-9]{8}$")
    )
    application.add_handler(
        CallbackQueryHandler(offer_special_order, pattern=r"^offer_[A-F0-9]{8}$")
    )
    application.add_handler(
        CallbackQueryHandler(request_car_photos, pattern=r"^photos_[A-F0-9]{8}$")
    )
    application.add_handler(
        CallbackQueryHandler(
            request_offer_photos,
            pattern=r"^offerphotos_[A-F0-9]{8}_\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            choose_special_offer,
            pattern=r"^chooseoffer_[A-F0-9]{8}_\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            reject_special_offer,
            pattern=r"^rejectoffer_[A-F0-9]{8}_\d+$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cancel_open_order,
            pattern=r"^cancelopen_[A-F0-9]{8}$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(
            cancel_active_order,
            pattern=r"^cancelactive_[A-F0-9]{8}$",
        )
    )
    application.add_handler(
        CallbackQueryHandler(arrived_order, pattern=r"^arrived_[A-F0-9]{8}$")
    )
    application.add_handler(
        CallbackQueryHandler(start_trip, pattern=r"^starttrip_[A-F0-9]{8}$")
    )
    application.add_handler(
        CallbackQueryHandler(finish_order, pattern=r"^finish_[A-F0-9]{8}$")
    )

    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.Regex(r"^❌ Отмена$")
            & ~filters.COMMAND,
            cancel_active_order,
        ),
        group=9,
    )

    # Переписка подключается последней, чтобы не перехватывать мастер заказа.
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            relay_message,
        ),
        group=10,
    )

    application.add_error_handler(error_handler)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
