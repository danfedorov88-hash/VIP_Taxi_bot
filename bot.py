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
    KeyboardButton,
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

# Основное имя переменной — ORDERS_CHAT_ID.
# DRIVER_REG_CHAT_ID оставлен как временный запасной вариант,
# чтобы уже настроенный Railway продолжил работать без остановки.
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
    ORDER_PHONE,
    ORDER_FROM,
    ORDER_TO,
    ORDER_TIME,
    ORDER_CLASS,
    ORDER_COMMENT,
    ORDER_CONFIRM,
) = range(8)

# ================= КЛАВИАТУРЫ =================

MAIN_KB = ReplyKeyboardMarkup(
    [["🚖 Заказать поездку"]],
    resize_keyboard=True,
)

PHONE_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📱 Отправить мой номер", request_contact=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
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

# ================= УТИЛИТЫ =================


def normalize_phone(text: str) -> Optional[str]:
    """Приводит номер к виду +XXXXXXXXXX и отсеивает явно неверные значения."""
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


def clean_text(text: str, max_length: int = 300) -> str:
    """Убирает лишние пробелы и ограничивает длину пользовательского текста."""
    value = " ".join((text or "").strip().split())
    return value[:max_length]


def order_summary(order: dict) -> str:
    return (
        "Проверьте заказ:\n\n"
        f"Имя: {order['name']}\n"
        f"Телефон: {order['phone']}\n"
        f"Откуда: {order['from']}\n"
        f"Куда: {order['to']}\n"
        f"Когда: {order['time']}\n"
        f"Класс: {order['car_class']}\n"
        f"Комментарий: {order['comment']}"
    )


def group_order_text(order_id: str, order: dict) -> str:
    return (
        f"🚖 НОВЫЙ ЗАКАЗ №{html.escape(order_id)}\n\n"
        f"👤 Клиент: {html.escape(order['name'])}\n"
        f"📱 Телефон: {html.escape(order['phone'])}\n"
        f"📍 Откуда: {html.escape(order['from'])}\n"
        f"🏁 Куда: {html.escape(order['to'])}\n"
        f"🕒 Когда: {html.escape(order['time'])}\n"
        f"🚘 Класс: {html.escape(order['car_class'])}\n"
        f"💬 Комментарий: {html.escape(order['comment'])}\n\n"
        "Статус: 🟢 свободен"
    )


def driver_display_name(user) -> str:
    full_name = clean_text(user.full_name, 100) or f"ID {user.id}"
    if user.username:
        return f"{full_name} (@{user.username})"
    return full_name


# ================= ОБЩИЕ КОМАНДЫ =================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "Заказ оформляется в личном чате с ботом.\n"
            "Для получения ID этого чата используйте /chatid."
        )
        return

    context.user_data.pop("order", None)
    await update.effective_message.reply_text(
        "🚖 VIP Taxi\n\nНажмите кнопку, чтобы оформить поездку:",
        reply_markup=MAIN_KB,
    )


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"ID этого чата: {update.effective_chat.id}"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("order", None)
    await update.effective_message.reply_text(
        "Оформление заказа отменено.",
        reply_markup=MAIN_KB,
    )
    return ConversationHandler.END


# ================= СОЗДАНИЕ ЗАКАЗА =================


async def order_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text(
            "Откройте личный чат с ботом и отправьте /start."
        )
        return ConversationHandler.END

    context.user_data["order"] = {}
    await update.effective_message.reply_text(
        "Как к вам обращаться?",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ORDER_NAME


async def order_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = clean_text(update.effective_message.text, 100)
    if len(name) < 2:
        await update.effective_message.reply_text("Введите имя ещё раз.")
        return ORDER_NAME

    context.user_data["order"]["name"] = name
    await update.effective_message.reply_text(
        "Отправьте номер телефона кнопкой ниже или введите его вручную:",
        reply_markup=PHONE_KB,
    )
    return ORDER_PHONE


async def order_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message

    if message.contact:
        if message.contact.user_id and message.contact.user_id != update.effective_user.id:
            await message.reply_text("Отправьте именно свой номер телефона.")
            return ORDER_PHONE
        raw_phone = message.contact.phone_number
    else:
        raw_phone = message.text

    phone = normalize_phone(raw_phone)
    if not phone:
        await message.reply_text(
            "Не удалось распознать номер. Пример: +7 999 123-45-67"
        )
        return ORDER_PHONE

    context.user_data["order"]["phone"] = phone
    await message.reply_text(
        "Откуда вас забрать?\nНапишите адрес или название места:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ORDER_FROM


async def order_from(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pickup = clean_text(update.effective_message.text)
    if len(pickup) < 3:
        await update.effective_message.reply_text("Укажите место подачи подробнее.")
        return ORDER_FROM

    context.user_data["order"]["from"] = pickup
    await update.effective_message.reply_text("Куда нужно ехать?")
    return ORDER_TO


async def order_to(update: Update, context: ContextTypes.DEFAULT_TYPE):
    destination = clean_text(update.effective_message.text)
    if len(destination) < 3:
        await update.effective_message.reply_text("Укажите пункт назначения подробнее.")
        return ORDER_TO

    context.user_data["order"]["to"] = destination
    await update.effective_message.reply_text(
        "Когда нужна машина?\n"
        "Например: сейчас, сегодня в 19:30 или 10 июля в 08:00"
    )
    return ORDER_TIME


async def order_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    when = clean_text(update.effective_message.text, 100)
    if len(when) < 3:
        await update.effective_message.reply_text("Укажите дату и время подробнее.")
        return ORDER_TIME

    context.user_data["order"]["time"] = when
    await update.effective_message.reply_text(
        "Выберите класс автомобиля:",
        reply_markup=CLASS_KB,
    )
    return ORDER_CLASS


async def order_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    car_class = clean_text(update.effective_message.text, 50)
    allowed = {"Business", "First", "Минивэн", "Неважно"}

    if car_class not in allowed:
        await update.effective_message.reply_text(
            "Выберите один из вариантов на клавиатуре.",
            reply_markup=CLASS_KB,
        )
        return ORDER_CLASS

    context.user_data["order"]["car_class"] = car_class
    await update.effective_message.reply_text(
        "Добавьте комментарий к заказу: номер рейса, детское кресло, количество пассажиров и т. п.\n\n"
        "Либо нажмите «Пропустить».",
        reply_markup=COMMENT_KB,
    )
    return ORDER_COMMENT


async def order_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = clean_text(update.effective_message.text)
    if comment.lower() == "пропустить":
        comment = "—"
    elif not comment:
        await update.effective_message.reply_text("Введите комментарий или нажмите «Пропустить».")
        return ORDER_COMMENT

    order = context.user_data["order"]
    order["comment"] = comment

    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Отправить заказ", callback_data="order_send"),
            InlineKeyboardButton("❌ Отмена", callback_data="order_cancel"),
        ]]
    )

    await update.effective_message.reply_text(
        order_summary(order) + "\n\nОтправить заказ водителям?",
        reply_markup=kb,
    )
    return ORDER_CONFIRM


async def order_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "order_cancel":
        context.user_data.pop("order", None)
        await query.edit_message_text("Заказ отменён.")
        await context.bot.send_message(
            chat_id=query.from_user.id,
            text="Можно оформить новый заказ:",
            reply_markup=MAIN_KB,
        )
        return ConversationHandler.END

    order = context.user_data.get("order")
    if not order:
        await query.edit_message_text("Данные заказа не найдены. Начните заново: /start")
        return ConversationHandler.END

    order_id = uuid.uuid4().hex[:8].upper()
    order_record = {
        **order,
        "client_id": query.from_user.id,
        "client_username": query.from_user.username or "",
        "status": "open",
        "driver_id": None,
        "driver_name": None,
    }

    take_kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Взять заказ", callback_data=f"take_{order_id}")]]
    )

    try:
        sent = await context.bot.send_message(
            chat_id=ORDERS_CHAT_ID,
            text=group_order_text(order_id, order_record),
            parse_mode="HTML",
            reply_markup=take_kb,
        )
    except TelegramError:
        logger.exception("Не удалось отправить заказ в группу %s", ORDERS_CHAT_ID)
        await query.edit_message_text(
            "Не удалось передать заказ водителям. Попробуйте ещё раз позже."
        )
        return ConversationHandler.END

    order_record["group_message_id"] = sent.message_id
    context.bot_data.setdefault("orders", {})[order_id] = order_record

    context.user_data.pop("order", None)
    await query.edit_message_text(
        f"✅ Заказ №{order_id} отправлен водителям.\n"
        "Мы сообщим, когда водитель возьмёт заказ."
    )
    await context.bot.send_message(
        chat_id=query.from_user.id,
        text="Для нового заказа нажмите кнопку ниже.",
        reply_markup=MAIN_KB,
    )

    return ConversationHandler.END


# ================= ВОДИТЕЛЬ БЕРЁТ ЗАКАЗ =================


async def take_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if not query.message or query.message.chat_id != ORDERS_CHAT_ID:
        await query.answer("Эта кнопка недоступна.", show_alert=True)
        return

    order_id = query.data.removeprefix("take_")
    orders = context.bot_data.setdefault("orders", {})
    order = orders.get(order_id)

    if not order:
        await query.answer(
            "Данные заказа не найдены. Возможно, бот перезапускался.",
            show_alert=True,
        )
        return

    if order.get("status") != "open":
        await query.answer("Этот заказ уже взят другим водителем.", show_alert=True)
        return

    try:
        member = await context.bot.get_chat_member(ORDERS_CHAT_ID, query.from_user.id)
    except TelegramError:
        logger.exception("Не удалось проверить участника группы")
        await query.answer("Не удалось проверить доступ. Попробуйте ещё раз.", show_alert=True)
        return

    if member.status not in {"member", "administrator", "creator", "restricted"}:
        await query.answer("Только для участников группы водителей.", show_alert=True)
        return

    # Обновления обрабатываются последовательно: первый водитель меняет статус,
    # а все последующие получают сообщение, что заказ уже занят.
    driver = query.from_user
    driver_name = driver_display_name(driver)
    order["status"] = "taken"
    order["driver_id"] = driver.id
    order["driver_name"] = driver_name

    accepted_text = (
        group_order_text(order_id, order).replace(
            "Статус: 🟢 свободен",
            f"Статус: 🔴 заказ взят\n🚘 Водитель: {html.escape(driver_name)}",
        )
    )

    try:
        await query.edit_message_text(
            accepted_text,
            parse_mode="HTML",
            reply_markup=None,
        )
    except TelegramError:
        # Если редактирование не удалось, возвращаем заказ в свободное состояние.
        order["status"] = "open"
        order["driver_id"] = None
        order["driver_name"] = None
        logger.exception("Не удалось закрепить заказ %s", order_id)
        await query.answer("Не удалось взять заказ. Попробуйте ещё раз.", show_alert=True)
        return

    await query.answer("Заказ закреплён за вами ✅", show_alert=True)

    client_notice = (
        f"🚘 Водитель взял заказ №{order_id}.\n\n"
        f"Водитель: {driver_name}\n"
        "Водитель свяжется с вами по указанному номеру."
    )

    try:
        await context.bot.send_message(order["client_id"], client_notice)
    except Forbidden:
        logger.warning("Клиент %s заблокировал бота", order["client_id"])
    except TelegramError:
        logger.exception("Не удалось уведомить клиента по заказу %s", order_id)

    driver_notice = (
        f"✅ Вы взяли заказ №{order_id}\n\n"
        f"Клиент: {order['name']}\n"
        f"Телефон: {order['phone']}\n"
        f"Откуда: {order['from']}\n"
        f"Куда: {order['to']}\n"
        f"Когда: {order['time']}\n"
        f"Класс: {order['car_class']}\n"
        f"Комментарий: {order['comment']}"
    )

    try:
        await context.bot.send_message(driver.id, driver_notice)
    except Forbidden:
        logger.info(
            "Водитель %s не запускал бота в личном чате; детали остались в группе",
            driver.id,
        )
    except TelegramError:
        logger.exception("Не удалось отправить детали водителю %s", driver.id)


# ================= ОШИБКИ =================


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
            MessageHandler(filters.Regex(r"^🚖 Заказать поездку$"), order_start),
        ],
        states={
            ORDER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_name)],
            ORDER_PHONE: [
                MessageHandler(filters.CONTACT, order_phone),
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_phone),
            ],
            ORDER_FROM: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_from)],
            ORDER_TO: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_to)],
            ORDER_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_time)],
            ORDER_CLASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, order_class)],
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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chat_id))
    app.add_handler(order_conversation)
    app.add_handler(CallbackQueryHandler(take_order, pattern=r"^take_[A-F0-9]{8}$"))
    app.add_error_handler(error_handler)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
