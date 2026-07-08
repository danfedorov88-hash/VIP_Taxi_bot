# -*- coding: utf-8 -*-

import logging
import os
import re
from typing import Optional

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
    PicklePersistence,
)

# ================= НАСТРОЙКИ =================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# ID чата модерации теперь берём из окружения, а не хардкодим.
DRIVER_REG_CHAT_ID = int(os.getenv("DRIVER_REG_CHAT_ID", "0"))

# Таймаут диалога регистрации (в секундах). Если пользователь бросил
# заполнение анкеты, состояние сбросится само через 30 минут.
CONV_TIMEOUT = 30 * 60

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# ================= СОСТОЯНИЯ =================

REG_NAME, REG_PHONE, REG_CAR, REG_DOCS, REG_CONFIRM = range(5)

# ================= УТИЛИТЫ =================

def normalize_phone(text: str) -> Optional[str]:
    digits = re.sub(r"\D", "", text or "")

    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]

    if len(digits) == 10:
        digits = "7" + digits

    if len(digits) != 11 or not digits.startswith("7"):
        return None

    # Отсекаем совсем нереальные номера вида +70000000000
    if digits == "7" + "0" * 10:
        return None

    return "+" + digits


# ================= START =================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = ReplyKeyboardMarkup(
        [["👨‍✈️ Стать водителем"]],
        resize_keyboard=True
    )

    await update.message.reply_text(
        "🚖 VIP Taxi\n\nВыберите действие:",
        reply_markup=kb
    )


# ================= ОТМЕНА =================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("reg", None)

    await update.message.reply_text(
        "Регистрация отменена.",
        reply_markup=ReplyKeyboardRemove()
    )

    return ConversationHandler.END


async def conv_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Срабатывает, если пользователь ничего не отвечает CONV_TIMEOUT секунд."""
    context.user_data.pop("reg", None)

    if update and update.effective_chat:
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Время регистрации истекло. Начните заново: /start",
            reply_markup=ReplyKeyboardRemove()
        )

    return ConversationHandler.END


# ================= РЕГИСТРАЦИЯ =================

async def reg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Защита от дублирующих заявок: если уже есть незавершённая регистрация,
    # просто продолжаем её, а не затираем прогресс.
    if "reg" not in context.user_data:
        context.user_data["reg"] = {"photos": []}

    await update.message.reply_text(
        "Введите ФИО полностью:",
        reply_markup=ReplyKeyboardRemove()
    )

    return REG_NAME


async def reg_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()

    if len(name.split()) < 2:
        await update.message.reply_text(
            "Введите ФИО полностью. Например: Иванов Иван Иванович"
        )
        return REG_NAME

    context.user_data["reg"]["name"] = name

    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Отправить номер", request_contact=True)]],
        resize_keyboard=True
    )

    await update.message.reply_text(
        "Отправьте номер телефона кнопкой ниже или введите вручную:",
        reply_markup=kb
    )

    return REG_PHONE


async def reg_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.contact:
        phone = update.message.contact.phone_number
    else:
        phone = update.message.text

    phone = normalize_phone(phone)

    if not phone:
        await update.message.reply_text(
            "Неверный номер. Введите российский номер в формате +7XXXXXXXXXX."
        )
        return REG_PHONE

    context.user_data["reg"]["phone"] = phone

    await update.message.reply_text(
        "Введите авто: марка, модель, год, госномер.\n\n"
        "Например: Mercedes-Benz S-Class W222, 2019, А123АА777",
        reply_markup=ReplyKeyboardRemove()
    )

    return REG_CAR


async def reg_car(update: Update, context: ContextTypes.DEFAULT_TYPE):
    car = update.message.text.strip()

    if len(car) < 8:
        await update.message.reply_text(
            "Введите авто подробнее: марка, модель, год, госномер."
        )
        return REG_CAR

    context.user_data["reg"]["car"] = car

    kb = ReplyKeyboardMarkup(
        [["Готово"]],
        resize_keyboard=True
    )

    await update.message.reply_text(
        "Отправьте фото документов и авто.\n\n"
        "Например:\n"
        "— водительское удостоверение\n"
        "— СТС\n"
        "— фото автомобиля\n\n"
        "Когда закончите, нажмите «Готово».",
        reply_markup=kb
    )

    return REG_DOCS


async def reg_docs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reg = context.user_data.get("reg")

    if not reg:
        await update.message.reply_text("Ошибка регистрации. Начните заново: /start")
        return ConversationHandler.END

    if update.message.text and update.message.text.lower() == "готово":
        if len(reg["photos"]) == 0:
            await update.message.reply_text(
                "Нужно отправить хотя бы одно фото документов или автомобиля."
            )
            return REG_DOCS

        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Отправить", callback_data="send"),
                InlineKeyboardButton("❌ Отмена", callback_data="cancel")
            ]
        ])

        await update.message.reply_text(
            f"Проверьте заявку:\n\n"
            f"ФИО: {reg['name']}\n"
            f"Телефон: {reg['phone']}\n"
            f"Авто: {reg['car']}\n"
            f"Фото: {len(reg['photos'])}\n\n"
            f"Отправить заявку?",
            reply_markup=kb
        )

        return REG_CONFIRM

    if update.message.photo:
        # Ограничиваем количество фото, чтобы не заспамить чат модерации.
        if len(reg["photos"]) >= 10:
            await update.message.reply_text(
                "Достигнут лимит в 10 фото. Нажмите «Готово», чтобы продолжить."
            )
            return REG_DOCS

        file_id = update.message.photo[-1].file_id
        reg["photos"].append(file_id)

        await update.message.reply_text(
            f"Фото добавлено. Всего фото: {len(reg['photos'])}"
        )

        return REG_DOCS

    await update.message.reply_text(
        "Отправьте фото или нажмите «Готово»."
    )

    return REG_DOCS


async def reg_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancel":
        context.user_data.pop("reg", None)
        await query.edit_message_text("Заявка отменена.")
        return ConversationHandler.END

    reg = context.user_data.get("reg")

    if not reg:
        await query.edit_message_text("Ошибка: данные заявки не найдены.")
        return ConversationHandler.END

    user = query.from_user

    # ИСПРАВЛЕНО: раньше тернарный оператор относился ко всей f-строке text,
    # а не только к последней строке — при отсутствии username вся заявка
    # (ФИО, телефон, авто, фото) исчезала из сообщения модератору.
    username_line = f"Username: @{user.username}" if user.username else "Username: —"

    text = (
        f"🚖 Новая заявка водителя\n\n"
        f"ФИО: {reg['name']}\n"
        f"Телефон: {reg['phone']}\n"
        f"Авто: {reg['car']}\n"
        f"Фото: {len(reg['photos'])}\n\n"
        f"Telegram ID: {user.id}\n"
        f"{username_line}"
    )

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=f"ok_{user.id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"no_{user.id}")
        ]
    ])

    try:
        await context.bot.send_message(
            chat_id=DRIVER_REG_CHAT_ID,
            text=text,
            reply_markup=kb
        )

        for photo_id in reg["photos"]:
            await context.bot.send_photo(
                chat_id=DRIVER_REG_CHAT_ID,
                photo=photo_id
            )
    except TelegramError:
        logger.exception("Не удалось отправить заявку в чат модерации")
        await query.edit_message_text(
            "Не удалось отправить заявку. Попробуйте позже или свяжитесь с поддержкой."
        )
        return ConversationHandler.END

    context.user_data.pop("reg", None)

    await query.edit_message_text(
        "Заявка отправлена. Ожидайте решения."
    )

    return ConversationHandler.END


# ================= МОДЕРАЦИЯ =================

async def moderation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.message.chat_id != DRIVER_REG_CHAT_ID:
        await query.answer("Недоступно", show_alert=True)
        return

    # Проверяем, что нажавший кнопку — админ чата модерации,
    # а не любой участник группы.
    member = await context.bot.get_chat_member(DRIVER_REG_CHAT_ID, query.from_user.id)
    if member.status not in ("administrator", "creator"):
        await query.answer("Только для администраторов", show_alert=True)
        return

    data = query.data

    try:
        action, user_id_raw = data.split("_")
        user_id = int(user_id_raw)
    except Exception:
        await query.edit_message_text("Ошибка обработки заявки.")
        return

    if action == "ok":
        result_text = "Заявка одобрена ✅"
        user_text = "Ваша заявка одобрена ✅"
    elif action == "no":
        result_text = "Заявка отклонена ❌"
        user_text = "Ваша заявка отклонена ❌"
    else:
        return

    try:
        await context.bot.send_message(chat_id=user_id, text=user_text)
    except Forbidden:
        # Пользователь заблокировал бота — модератор всё равно должен
        # увидеть, что решение принято.
        logger.warning("Не удалось уведомить пользователя %s: бот заблокирован", user_id)
        result_text += "\n(не удалось уведомить пользователя — бот заблокирован)"
    except TelegramError:
        logger.exception("Ошибка уведомления пользователя %s", user_id)
        result_text += "\n(ошибка при уведомлении пользователя)"

    await query.edit_message_text(result_text)


# ================= ОБРАБОТКА ОШИБОК =================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Необработанное исключение", exc_info=context.error)


# ================= MAIN =================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("Укажи переменную окружения BOT_TOKEN")

    if not DRIVER_REG_CHAT_ID:
        raise RuntimeError("Укажи переменную окружения DRIVER_REG_CHAT_ID")

    # Сохраняем user_data на диск, чтобы регистрация не терялась
    # при перезапуске бота.
    persistence = PicklePersistence(filepath="bot_state.pickle")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("reg_driver", reg_start),
            MessageHandler(
                filters.Regex("^👨‍✈️ Стать водителем$"),
                reg_start
            ),
        ],
        states={
            REG_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_name)
            ],
            REG_PHONE: [
                MessageHandler(filters.CONTACT, reg_phone),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_phone),
            ],
            REG_CAR: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_car)
            ],
            REG_DOCS: [
                MessageHandler(filters.PHOTO, reg_docs),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_docs),
            ],
            REG_CONFIRM: [
                CallbackQueryHandler(reg_confirm, pattern="^(send|cancel)$")
            ],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, conv_timeout),
                CallbackQueryHandler(conv_timeout),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel)
        ],
        conversation_timeout=CONV_TIMEOUT,
        name="driver_registration",
        persistent=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)

    app.add_handler(
        CallbackQueryHandler(moderation, pattern="^(ok|no)_")
    )

    app.add_error_handler(error_handler)

    app.run_polling()


if __name__ == "__main__":
    main()

