# -*- coding: utf-8 -*-

import html
import logging
import os
import re
import uuid
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import Forbidden, TelegramError
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

# ================= НАСТРОЙКИ =================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Основная переменная. DRIVER_REG_CHAT_ID оставлена для совместимости
# с уже настроенным проектом Railway.
ORDERS_CHAT_ID = int(
    os.getenv("ORDERS_CHAT_ID")
    or os.getenv("DRIVER_REG_CHAT_ID", "0")
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ================= СОСТОЯНИЯ ЗАКАЗА =================

(
    ORDER_NAME,
    ORDER_FROM,
    ORDER_TO,
    ORDER_TIME,
    ORDER_CLASS,
    ORDER_COMMENT,
    ORDER_CONFIRM,
) = range(7)

# ================= КЛАВИАТУРЫ =================

MAIN_KB = ReplyKeyboardMarkup(
    [["🚖 Заказать поездку"]],
    resize_keyboard=True,
)

CLASS_KB = ReplyKeyboardMarkup(
    [
        ["Business", "First"],
        ["Минивэн", "Неважно"],
    ],
    resize_keyboard=True,
    one_time_keyboard=True,
)

COMMENT_KB = ReplyKeyboardMarkup(
    [["Пропустить"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

ALLOWED_CLASSES = {"Business", "First", "Минивэн", "Неважно"}
DRIVER_CLASSES = {"Business", "First", "Минивэн"}

# Очевидные контактные данные не передаются между клиентом и водителем.
CONTACT_RE = re.compile(
    r"(?:\+?\d[\d\s().-]{7,}\d)|(?:@[A-Za-z0-9_]{5,})|(?:https?://t\.me/)|(?:t\.me/)",
    re.IGNORECASE,
)

# ================= ХРАНИЛИЩЕ =================


def get_orders(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.bot_data.setdefault("orders_v3", {})


def get_drivers(context: ContextTypes.DEFAULT_TYPE) -> dict:
    # Ключ — Telegram ID строкой, значение — класс автомобиля.
    return context.bot_data.setdefault("drivers_v3", {})


def get_pending(context: ContextTypes.DEFAULT_TYPE) -> dict:
    # client_id -> order_id
    return context.bot_data.setdefault("pending_by_client_v3", {})


def get_active(context: ContextTypes.DEFAULT_TYPE) -> dict:
    # user_id -> order_id; в карту входят клиент и водитель.
    return context.bot_data.setdefault("active_by_user_v3", {})


def track_message(order: dict, chat_id: int, message_id: int) -> None:
    messages = order.setdefault("message_ids", {})
    ids = messages.setdefault(str(chat_id), [])
    if message_id not in ids:
        ids.append(message_id)


def track_incoming(order: dict, update: Update) -> None:
    message = update.effective_message
    if message:
        track_message(order, message.chat_id, message.message_id)


async def send_tracked(
    context: ContextTypes.DEFAULT_TYPE,
    order: dict,
    chat_id: int,
    text: str,
    **kwargs,
):
    message = await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    track_message(order, chat_id, message.message_id)
    return message


async def reply_tracked(
    update: Update,
    order: dict,
    text: str,
    **kwargs,
):
    message = await update.effective_message.reply_text(text=text, **kwargs)
    track_message(order, message.chat_id, message.message_id)
    return message


# ================= УТИЛИТЫ =================


def clean_text(text: Optional[str], max_length: int = 300) -> str:
    value = " ".join((text or "").strip().split())
    return value[:max_length]


def normalize_driver_class(value: str) -> Optional[str]:
    normalized = clean_text(value, 40).lower().replace("-", " ")
    aliases = {
        "business": "Business",
        "бизнес": "Business",
        "first": "First",
        "первый": "First",
        "минивэн": "Минивэн",
        "минивен": "Минивэн",
        "minivan": "Минивэн",
    }
    return aliases.get(normalized)


def contains_contact_data(message) -> bool:
    if message.contact:
        return True

    text = message.text or message.caption or ""
    return bool(CONTACT_RE.search(text))


def order_summary(order: dict) -> str:
    return (
        "Проверьте заказ:\n\n"
        f"Имя: {order['name']}\n"
        f"Откуда: {order['from']}\n"
        f"Куда: {order['to']}\n"
        f"Когда: {order['time']}\n"
        f"Класс: {order['car_class']}\n"
        f"Комментарий: {order['comment']}"
    )


def public_order_text(order_id: str, order: dict) -> str:
    # В группе нет телефона, Telegram ID и username клиента.
    return (
        f"🚖 НОВЫЙ ЗАКАЗ №{html.escape(order_id)}\n\n"
        f"👤 Клиент: {html.escape(order['name'])}\n"
        f"📍 Откуда: {html.escape(order['from'])}\n"
        f"🏁 Куда: {html.escape(order['to'])}\n"
        f"🕒 Когда: {html.escape(order['time'])}\n"
        f"🚘 Класс: {html.escape(order['car_class'])}\n"
        f"💬 Комментарий: {html.escape(order['comment'])}\n\n"
        "Контакты клиента скрыты. Общение — только через бота."
    )


def private_order_text(order_id: str, order: dict) -> str:
    return (
        f"✅ Вы приняли заказ №{order_id}\n\n"
        f"👤 Клиент: {order['name']}\n"
        f"📍 Откуда: {order['from']}\n"
        f"🏁 Куда: {order['to']}\n"
        f"🕒 Когда: {order['time']}\n"
        f"🚘 Класс: {order['car_class']}\n"
        f"💬 Комментарий: {order['comment']}\n\n"
        "Пишите сообщения в этот чат — бот анонимно передаст их клиенту.\n"
        "Контактные данные передавать нельзя."
    )


def user_has_order(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    uid = str(user_id)
    return uid in get_pending(context) or uid in get_active(context)


async def is_orders_admin(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
) -> bool:
    try:
        member = await context.bot.get_chat_member(ORDERS_CHAT_ID, user_id)
    except TelegramError:
        logger.exception("Не удалось проверить права администратора %s", user_id)
        return False

    return member.status in {"administrator", "creator"}


async def delete_tracked_messages(
    context: ContextTypes.DEFAULT_TYPE,
    order: dict,
) -> None:
    """Удаляет все известные сообщения заказа в личных чатах."""
    for chat_id_raw, message_ids in order.get("message_ids", {}).items():
        chat_id = int(chat_id_raw)
        unique_ids = list(dict.fromkeys(message_ids))

        # Bot API принимает до 100 сообщений за один запрос.
        for index in range(0, len(unique_ids), 100):
            chunk = unique_ids[index:index + 100]
            try:
                await context.bot.delete_messages(
                    chat_id=chat_id,
                    message_ids=chunk,
                )
            except TelegramError:
                # Запасной вариант: удаляем по одному. Некоторые старые или
                # недоступные сообщения Telegram может не разрешить удалить.
                for message_id in chunk:
                    try:
                        await context.bot.delete_message(
                            chat_id=chat_id,
                            message_id=message_id,
                        )
                    except TelegramError:
                        logger.debug(
                            "Не удалось удалить сообщение %s в чате %s",
                            message_id,
                            chat_id,
                        )


async def cleanup_order_links(
    context: ContextTypes.DEFAULT_TYPE,
    order_id: str,
    order: dict,
) -> None:
    pending = get_pending(context)
    active = get_active(context)

    pending.pop(str(order.get("client_id")), None)
    active.pop(str(order.get("client_id")), None)

    driver_id = order.get("driver_id")
    if driver_id:
        active.pop(str(driver_id), None)

    get_orders(context).pop(order_id, None)


# ================= ОБЩИЕ КОМАНДЫ =================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "Заказы оформляются в личном чате с ботом.\n"
            "ID этой группы: /chatid"
        )
        return

    user_id = update.effective_user.id
    driver_class = get_drivers(context).get(str(user_id))

    if driver_class:
        text = (
            f"🚘 Вы зарегистрированы как водитель класса {driver_class}.\n"
            "Новые заказы появляются в группе водителей.\n\n"
            "Через этого же бота можно оформить личную поездку."
        )
    else:
        text = "🚖 VIP Taxi\n\nНажмите кнопку, чтобы оформить поездку:"

    await update.effective_message.reply_text(text, reply_markup=MAIN_KB)


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"ID этого чата: {update.effective_chat.id}"
    )


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    driver_class = get_drivers(context).get(str(user_id))
    suffix = f"\nВаш класс: {driver_class}" if driver_class else ""
    await update.effective_message.reply_text(
        f"Ваш Telegram ID: {user_id}{suffix}"
    )


async def cancel_draft(update: Update, context: ContextTypes.DEFAULT_TYPE):
    draft = context.user_data.pop("draft_order", None)
    if draft:
        track_incoming(draft, update)
        await delete_tracked_messages(context, draft)

    await update.effective_message.reply_text(
        "Оформление заказа отменено.",
        reply_markup=MAIN_KB,
    )
    return ConversationHandler.END


# ================= УПРАВЛЕНИЕ ВОДИТЕЛЯМИ =================


async def set_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_orders_admin(context, update.effective_user.id):
        await update.effective_message.reply_text("Команда доступна только администраторам.")
        return

    if len(context.args) != 2:
        await update.effective_message.reply_text(
            "Использование:\n/setdriver TELEGRAM_ID Business\n"
            "/setdriver TELEGRAM_ID First\n"
            "/setdriver TELEGRAM_ID Минивэн"
        )
        return

    try:
        driver_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Telegram ID должен быть числом.")
        return

    driver_class = normalize_driver_class(context.args[1])
    if not driver_class:
        await update.effective_message.reply_text(
            "Допустимые классы: Business, First, Минивэн."
        )
        return

    get_drivers(context)[str(driver_id)] = driver_class
    await update.effective_message.reply_text(
        f"✅ Водитель {driver_id} зарегистрирован.\nКласс: {driver_class}\n\n"
        "Водитель остаётся обычным участником группы — администратором становиться не нужно."
    )


async def remove_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_orders_admin(context, update.effective_user.id):
        await update.effective_message.reply_text("Команда доступна только администраторам.")
        return

    if len(context.args) != 1:
        await update.effective_message.reply_text(
            "Использование: /removedriver TELEGRAM_ID"
        )
        return

    try:
        driver_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Telegram ID должен быть числом.")
        return

    removed = get_drivers(context).pop(str(driver_id), None)
    if removed:
        await update.effective_message.reply_text("Водитель удалён из системы.")
    else:
        await update.effective_message.reply_text("Водитель не найден.")


async def list_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_orders_admin(context, update.effective_user.id):
        await update.effective_message.reply_text("Команда доступна только администраторам.")
        return

    drivers = get_drivers(context)
    if not drivers:
        await update.effective_message.reply_text("Зарегистрированных водителей пока нет.")
        return

    lines = ["🚘 Зарегистрированные водители:"]
    for driver_id, driver_class in sorted(drivers.items()):
        lines.append(f"• {driver_id} — {driver_class}")

    await update.effective_message.reply_text("\n".join(lines))


# ================= СОЗДАНИЕ ЗАКАЗА =================


async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "Откройте личный чат с ботом и отправьте /start."
        )
        return ConversationHandler.END

    user_id = update.effective_user.id
    if user_has_order(context, user_id):
        await update.effective_message.reply_text(
            "У вас уже есть заказ, ожидающий водителя, или активная поездка."
        )
        return ConversationHandler.END

    draft = {
        "client_id": user_id,
        "status": "draft",
        "message_ids": {},
        "queued_message_ids": [],
    }
    context.user_data["draft_order"] = draft

    track_incoming(draft, update)
    await reply_tracked(
        update,
        draft,
        "Как к вам обращаться?",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ORDER_NAME


async def order_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    draft = context.user_data["draft_order"]
    track_incoming(draft, update)

    name = clean_text(update.effective_message.text, 100)
    if len(name) < 2:
        await reply_tracked(update, draft, "Введите имя ещё раз.")
        return ORDER_NAME

    draft["name"] = name
    await reply_tracked(
        update,
        draft,
        "Откуда вас забрать? Напишите адрес или название места:",
    )
    return ORDER_FROM


async def order_from(update: Update, context: ContextTypes.DEFAULT_TYPE):
    draft = context.user_data["draft_order"]
    track_incoming(draft, update)

    pickup = clean_text(update.effective_message.text)
    if len(pickup) < 3:
        await reply_tracked(update, draft, "Укажите место подачи подробнее.")
        return ORDER_FROM

    draft["from"] = pickup
    await reply_tracked(update, draft, "Куда нужно ехать?")
    return ORDER_TO


async def order_to(update: Update, context: ContextTypes.DEFAULT_TYPE):
    draft = context.user_data["draft_order"]
    track_incoming(draft, update)

    destination = clean_text(update.effective_message.text)
    if len(destination) < 3:
        await reply_tracked(update, draft, "Укажите пункт назначения подробнее.")
        return ORDER_TO

    draft["to"] = destination
    await reply_tracked(
        update,
        draft,
        "Когда нужна машина?\n"
        "Например: сейчас, сегодня в 19:30 или 10 июля в 08:00",
    )
    return ORDER_TIME


async def order_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    draft = context.user_data["draft_order"]
    track_incoming(draft, update)

    when = clean_text(update.effective_message.text, 100)
    if len(when) < 3:
        await reply_tracked(update, draft, "Укажите дату и время подробнее.")
        return ORDER_TIME

    draft["time"] = when
    await reply_tracked(
        update,
        draft,
        "Выберите класс автомобиля:",
        reply_markup=CLASS_KB,
    )
    return ORDER_CLASS


async def order_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    draft = context.user_data["draft_order"]
    track_incoming(draft, update)

    car_class = clean_text(update.effective_message.text, 50)
    if car_class not in ALLOWED_CLASSES:
        await reply_tracked(
            update,
            draft,
            "Выберите один из вариантов на клавиатуре.",
            reply_markup=CLASS_KB,
        )
        return ORDER_CLASS

    draft["car_class"] = car_class
    await reply_tracked(
        update,
        draft,
        "Добавьте комментарий: номер рейса, детское кресло, "
        "количество пассажиров и т. п.\n\n"
        "Либо нажмите «Пропустить».",
        reply_markup=COMMENT_KB,
    )
    return ORDER_COMMENT


async def order_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    draft = context.user_data["draft_order"]
    track_incoming(draft, update)

    comment = clean_text(update.effective_message.text)
    if comment.lower() == "пропустить":
        comment = "—"
    elif not comment:
        await reply_tracked(
            update,
            draft,
            "Введите комментарий или нажмите «Пропустить».",
        )
        return ORDER_COMMENT

    if CONTACT_RE.search(comment):
        await reply_tracked(
            update,
            draft,
            "Контактные данные указывать не нужно. Общение будет через бота.",
        )
        return ORDER_COMMENT

    draft["comment"] = comment

    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Отправить заказ", callback_data="order_send"),
            InlineKeyboardButton("❌ Отмена", callback_data="order_cancel"),
        ]]
    )

    await reply_tracked(
        update,
        draft,
        order_summary(draft) + "\n\nОтправить заказ водителям?",
        reply_markup=kb,
    )
    return ORDER_CONFIRM


async def order_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    draft = context.user_data.get("draft_order")
    if not draft:
        await query.edit_message_text("Данные заказа не найдены. Начните заново: /start")
        return ConversationHandler.END

    if query.data == "order_cancel":
        context.user_data.pop("draft_order", None)
        await delete_tracked_messages(context, draft)
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="Заказ отменён.",
            reply_markup=MAIN_KB,
        )
        return ConversationHandler.END

    order_id = uuid.uuid4().hex[:8].upper()
    order = {
        **draft,
        "order_id": order_id,
        "status": "open",
        "driver_id": None,
        "driver_class": None,
        "group_message_id": None,
        "queued_message_ids": [],
    }

    take_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🟢 Взять заказ", callback_data=f"take_{order_id}")]]
    )

    try:
        group_message = await context.bot.send_message(
            chat_id=ORDERS_CHAT_ID,
            text=public_order_text(order_id, order),
            parse_mode="HTML",
            reply_markup=take_kb,
        )
    except TelegramError:
        logger.exception("Не удалось отправить заказ в группу %s", ORDERS_CHAT_ID)
        await query.edit_message_text(
            "Не удалось передать заказ водителям. Попробуйте ещё раз позже."
        )
        context.user_data.pop("draft_order", None)
        return ConversationHandler.END

    order["group_message_id"] = group_message.message_id
    get_orders(context)[order_id] = order
    get_pending(context)[str(query.from_user.id)] = order_id
    context.user_data.pop("draft_order", None)

    pending_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Отменить заказ", callback_data=f"cancel_{order_id}")]]
    )

    await query.edit_message_text(
        f"✅ Заказ №{order_id} отправлен водителям.\n"
        "Пока водитель не назначен, можете отправить сюда дополнительное сообщение — "
        "бот передаст его водителю после принятия заказа.",
        reply_markup=pending_kb,
    )

    return ConversationHandler.END


async def cancel_pending_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    order_id = query.data.removeprefix("cancel_")
    order = get_orders(context).get(order_id)

    if not order or order.get("status") != "open":
        await query.answer("Заказ уже недоступен.", show_alert=True)
        return

    if query.from_user.id != order.get("client_id"):
        await query.answer("Это не ваш заказ.", show_alert=True)
        return

    await query.answer("Заказ отменён")

    try:
        await context.bot.delete_message(
            chat_id=ORDERS_CHAT_ID,
            message_id=order["group_message_id"],
        )
    except TelegramError:
        logger.debug("Не удалось удалить отменённый заказ из группы")

    await cleanup_order_links(context, order_id, order)
    await delete_tracked_messages(context, order)

    await context.bot.send_message(
        chat_id=order["client_id"],
        text="Заказ отменён. Связь с водителем не создавалась.",
        reply_markup=MAIN_KB,
    )


# ================= ВОДИТЕЛЬ БЕРЁТ ЗАКАЗ =================


async def take_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not query.message or query.message.chat_id != ORDERS_CHAT_ID:
        await query.answer("Эта кнопка недоступна.", show_alert=True)
        return

    order_id = query.data.removeprefix("take_")
    order = get_orders(context).get(order_id)

    if not order or order.get("status") != "open":
        await query.answer("Этот заказ уже недоступен.", show_alert=True)
        return

    driver_id = query.from_user.id
    driver_class = get_drivers(context).get(str(driver_id))

    if not driver_class:
        await query.answer(
            "Вы не зарегистрированы как водитель. Сообщите администратору свой ID из команды /myid.",
            show_alert=True,
        )
        return

    required_class = order["car_class"]
    if required_class != "Неважно" and driver_class != required_class:
        await query.answer(
            f"Для этого заказа нужен класс {required_class}. Ваш класс: {driver_class}.",
            show_alert=True,
        )
        return

    if str(driver_id) in get_active(context):
        await query.answer(
            "Сначала завершите текущий заказ.",
            show_alert=True,
        )
        return

    finish_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Завершить заказ", callback_data=f"finish_{order_id}")]]
    )

    # Сначала проверяем, что бот может написать водителю в личный чат.
    try:
        driver_message = await context.bot.send_message(
            chat_id=driver_id,
            text=private_order_text(order_id, order),
            reply_markup=finish_kb,
            protect_content=True,
        )
    except Forbidden:
        await query.answer(
            "Сначала откройте личный чат с ботом и нажмите /start.",
            show_alert=True,
        )
        return
    except TelegramError:
        logger.exception("Не удалось написать водителю %s", driver_id)
        await query.answer(
            "Не удалось открыть личный чат. Попробуйте ещё раз.",
            show_alert=True,
        )
        return

    track_message(order, driver_id, driver_message.message_id)

    try:
        client_message = await send_tracked(
            context,
            order,
            order["client_id"],
            f"🚘 Водитель принял заказ №{order_id}.\n\n"
            "Теперь пишите сообщения прямо сюда — бот анонимно передаст их водителю.\n"
            "Телефон, username и другие контакты водителю не показываются.",
            protect_content=True,
        )
    except TelegramError:
        logger.exception("Не удалось уведомить клиента %s", order["client_id"])
        try:
            await context.bot.delete_message(driver_id, driver_message.message_id)
        except TelegramError:
            pass
        await query.answer("Клиент сейчас недоступен.", show_alert=True)
        return

    order["status"] = "active"
    order["driver_id"] = driver_id
    order["driver_class"] = driver_class

    get_pending(context).pop(str(order["client_id"]), None)
    get_active(context)[str(order["client_id"])] = order_id
    get_active(context)[str(driver_id)] = order_id

    # Передаём сообщения, которые клиент написал до назначения водителя.
    for source_message_id in order.get("queued_message_ids", []):
        try:
            copied = await context.bot.copy_message(
                chat_id=driver_id,
                from_chat_id=order["client_id"],
                message_id=source_message_id,
                protect_content=True,
            )
            track_message(order, driver_id, copied.message_id)
        except TelegramError:
            logger.debug(
                "Не удалось передать отложенное сообщение %s по заказу %s",
                source_message_id,
                order_id,
            )

    await query.answer("Заказ закреплён за вами ✅", show_alert=True)

    # Заказ полностью исчезает из группы после принятия.
    try:
        await context.bot.delete_message(
            chat_id=ORDERS_CHAT_ID,
            message_id=query.message.message_id,
        )
    except TelegramError:
        logger.exception("Не удалось удалить принятый заказ %s из группы", order_id)
        try:
            await query.edit_message_text("Заказ уже принят.", reply_markup=None)
        except TelegramError:
            pass


# ================= АНОНИМНАЯ ПЕРЕПИСКА =================


async def relay_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user_id = update.effective_user.id
    uid = str(user_id)

    # Сообщение клиента до назначения водителя сохраняется и будет скопировано
    # водителю после принятия заказа.
    pending_order_id = get_pending(context).get(uid)
    if pending_order_id:
        order = get_orders(context).get(pending_order_id)
        if not order or order.get("status") != "open":
            get_pending(context).pop(uid, None)
            return

        if contains_contact_data(message):
            warning = await message.reply_text(
                "Контактные данные передавать нельзя. Напишите сообщение без телефона, username или ссылки."
            )
            track_message(order, user_id, warning.message_id)
            return

        track_message(order, user_id, message.message_id)
        order.setdefault("queued_message_ids", []).append(message.message_id)

        ack = await message.reply_text(
            "Сообщение сохранено. Водитель увидит его после принятия заказа."
        )
        track_message(order, user_id, ack.message_id)
        return

    active_order_id = get_active(context).get(uid)
    if not active_order_id:
        return

    order = get_orders(context).get(active_order_id)
    if not order or order.get("status") != "active":
        get_active(context).pop(uid, None)
        return

    if contains_contact_data(message):
        warning = await message.reply_text(
            "Контактные данные не переданы. Общение должно оставаться внутри бота."
        )
        track_message(order, user_id, warning.message_id)
        return

    client_id = order["client_id"]
    driver_id = order["driver_id"]

    if user_id == client_id:
        recipient_id = driver_id
    elif user_id == driver_id:
        recipient_id = client_id
    else:
        return

    track_message(order, user_id, message.message_id)

    try:
        copied = await context.bot.copy_message(
            chat_id=recipient_id,
            from_chat_id=user_id,
            message_id=message.message_id,
            protect_content=True,
        )
        track_message(order, recipient_id, copied.message_id)
    except TelegramError:
        logger.exception(
            "Не удалось передать сообщение по заказу %s от %s к %s",
            active_order_id,
            user_id,
            recipient_id,
        )
        error_message = await message.reply_text(
            "Не удалось передать сообщение. Попробуйте ещё раз."
        )
        track_message(order, user_id, error_message.message_id)


# ================= ЗАВЕРШЕНИЕ И УДАЛЕНИЕ ПЕРЕПИСКИ =================


async def finish_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    order_id = query.data.removeprefix("finish_")
    order = get_orders(context).get(order_id)

    if not order or order.get("status") != "active":
        await query.answer("Заказ уже завершён или недоступен.", show_alert=True)
        return

    if query.from_user.id != order.get("driver_id"):
        await query.answer("Завершить заказ может только назначенный водитель.", show_alert=True)
        return

    await query.answer("Заказ завершён")
    order["status"] = "finishing"

    client_id = order["client_id"]
    driver_id = order["driver_id"]

    # Сначала разрываем связь, чтобы новые сообщения уже не передавались.
    get_active(context).pop(str(client_id), None)
    get_active(context).pop(str(driver_id), None)

    # Затем удаляем всю известную переписку и сообщения оформления заказа.
    await delete_tracked_messages(context, order)
    get_orders(context).pop(order_id, None)

    # Эти два сообщения создаются уже после очистки и к заказу не привязаны.
    try:
        await context.bot.send_message(
            chat_id=client_id,
            text="✅ Заказ завершён. Переписка удалена, связь с водителем закрыта.",
            reply_markup=MAIN_KB,
        )
    except TelegramError:
        logger.exception("Не удалось отправить клиенту итог заказа %s", order_id)

    try:
        await context.bot.send_message(
            chat_id=driver_id,
            text="✅ Заказ завершён. Переписка удалена, связь с клиентом закрыта.",
        )
    except TelegramError:
        logger.exception("Не удалось отправить водителю итог заказа %s", order_id)


# ================= ОБРАБОТКА ОШИБОК =================


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Необработанное исключение", exc_info=context.error)


# ================= ЗАПУСК =================


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Укажите переменную окружения BOT_TOKEN")

    if not ORDERS_CHAT_ID:
        raise RuntimeError("Укажите переменную окружения ORDERS_CHAT_ID")

    persistence = PicklePersistence(filepath="bot_state.pickle")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .concurrent_updates(False)
        .build()
    )

    order_conversation = ConversationHandler(
        entry_points=[
            CommandHandler("order", order_start),
            MessageHandler(
                filters.ChatType.PRIVATE & filters.Regex(r"^🚖 Заказать поездку$"),
                order_start,
            ),
        ],
        states={
            ORDER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_name)
            ],
            ORDER_FROM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_from)
            ],
            ORDER_TO: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_to)
            ],
            ORDER_TIME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_time)
            ],
            ORDER_CLASS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_class)
            ],
            ORDER_COMMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_comment)
            ],
            ORDER_CONFIRM: [
                CallbackQueryHandler(
                    order_confirm,
                    pattern=r"^order_(send|cancel)$",
                )
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_draft)],
        allow_reentry=True,
        name="client_order_v3",
        persistent=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chat_id))
    app.add_handler(CommandHandler("myid", my_id))
    app.add_handler(CommandHandler("setdriver", set_driver))
    app.add_handler(CommandHandler("removedriver", remove_driver))
    app.add_handler(CommandHandler("drivers", list_drivers))

    app.add_handler(order_conversation)

    app.add_handler(
        CallbackQueryHandler(
            take_order,
            pattern=r"^take_[A-F0-9]{8}$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            cancel_pending_order,
            pattern=r"^cancel_[A-F0-9]{8}$",
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            finish_order,
            pattern=r"^finish_[A-F0-9]{8}$",
        )
    )

    # Последний обработчик: сообщения в личном чате вне анкеты пересылаются
    # только внутри активной анонимной связи клиент ↔ водитель.
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & ~filters.COMMAND,
            relay_private_message,
        )
    )

    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
