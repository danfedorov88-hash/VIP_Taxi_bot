# -*- coding: utf-8 -*-

import asyncio
import html
import json
import logging
import os
import re
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

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
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free")
MODERATION_CHAT_ID = int(os.getenv("MODERATION_CHAT_ID", "-5062249297"))
ORDERS_CHAT_ID = int(os.getenv("ORDERS_CHAT_ID", "-1003446115764"))
PERSISTENCE_PATH = os.getenv("PERSISTENCE_PATH", "bot_state.pickle")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ================= ТАРИФЫ =================

CAR_CLASSES = {"Business", "First", "Lux", "Минивэн"}
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

# ================= СОСТОЯНИЯ РЕГИСТРАЦИИ =================

(
    REG_NAME,
    REG_PHONE,
    REG_CAR,
    REG_YEAR,
    REG_PLATE,
    REG_CLASS,
    REG_DOCUMENT_PHOTOS,
    REG_CAR_PHOTOS,
    REG_CONFIRM,
) = range(20, 29)

# ================= КЛАВИАТУРЫ =================

MAIN_KB = ReplyKeyboardMarkup(
    [
        ["🚘 Заказать поездку", "✨ Особый запрос"],
        ["👨‍✈️ Стать водителем", "📋 Мой статус"],
    ],
    resize_keyboard=True,
)

PHONE_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📱 Отправить мой номер", request_contact=True)]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

DRIVER_CLASS_KB = ReplyKeyboardMarkup(
    [["Business", "First"], ["Lux", "Минивэн"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

DONE_KB = ReplyKeyboardMarkup(
    [["Готово"]],
    resize_keyboard=True,
    one_time_keyboard=True,
)

AI_WELCOME = (
    "🤖 VIP Taxi AI\n\n"
    "Опишите поездку обычным сообщением. Я соберу маршрут, уточню только "
    "недостающие данные и рассчитаю стоимость.\n\n"
    "Пример:\n"
    "Завтра в 9:00 забрать с Тверской 10, заехать на Кутузовский 22, "
    "потом в Шереметьево. Нужен First, 2 пассажира и детское кресло."
)

# ================= УТИЛИТЫ =================


def clean_text(text: str, max_length: int = 1200) -> str:
    return " ".join((text or "").strip().split())[:max_length]


def esc(value: object) -> str:
    return html.escape(str(value if value not in (None, "") else "—"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_moscow() -> datetime:
    return datetime.now(timezone(timedelta(hours=3)))


def format_dt(value: datetime) -> str:
    return value.strftime("%d.%m.%Y %H:%M")


def parse_iso(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def normalize_phone(text: str) -> Optional[str]:
    digits = re.sub(r"\D", "", text or "")
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if not 10 <= len(digits) <= 15 or len(set(digits)) == 1:
        return None
    return "+" + digits


def ensure_storage(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.bot_data.setdefault("drivers", {})
    context.bot_data.setdefault("driver_apps", {})
    context.bot_data.setdefault("orders", {})
    context.bot_data.setdefault("active_by_user", {})
    context.bot_data.setdefault("pending_by_client", {})
    context.bot_data.setdefault("client_history", {})


def get_draft(context: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    draft = context.user_data.get("order_draft")
    return draft if isinstance(draft, dict) else None


def new_draft(user, special: bool = False) -> dict:
    return {
        "client_name": user.first_name or "Клиент",
        "route_points": [],
        "scheduled_at": None,
        "time": None,
        "car_class": None,
        "tariff": "Особый запрос" if special else None,
        "hours": None,
        "passengers": None,
        "comment": "",
        "special_request": "" if special else None,
        "status": "collecting",
        "last_question": None,
        "created_at": now_iso(),
    }


def airport_group(route_points: list[dict]) -> Optional[str]:
    text = " ".join(str(p.get("label") or p.get("value") or "") for p in route_points).lower()
    if any(x in text for x in ("шереметьево", "svo", "внуково", "vko")):
        return "svo_vko"
    if any(x in text for x in ("домодедово", "dme", "жуковский", "zia")):
        return "dme_zia"
    return None


def detect_tariff(draft: dict) -> str:
    if draft.get("special_request") is not None:
        return "Особый запрос"
    if draft.get("hours"):
        return "Почасовая"
    if airport_group(draft.get("route_points", [])):
        return "Аэропорт"
    return draft.get("tariff") or "Разовая поездка"


def calculate_price(draft: dict) -> str:
    tariff = detect_tariff(draft)
    draft["tariff"] = tariff
    if tariff == "Особый запрос":
        return "По договорённости"
    if tariff == "Почасовая":
        rate = HOURLY_RATES.get(draft.get("car_class"))
        hours = draft.get("hours")
        if rate and hours:
            return f"{rate * int(hours):,} ₽ ({rate:,} ₽/час × {hours} ч.)".replace(",", " ")
        return "По договорённости"
    if tariff == "Аэропорт":
        # Фиксированная цена только для прямого маршрута из 2 точек.
        if len(draft.get("route_points", [])) != 2:
            return "По договорённости"
        group = airport_group(draft.get("route_points", []))
        car_class = draft.get("car_class")
        if group and car_class in AIRPORT_RATES[group]:
            return f"{AIRPORT_RATES[group][car_class]:,} ₽".replace(",", " ")
    return "По договорённости"


def route_text(route_points: list[dict]) -> str:
    if not route_points:
        return "—"
    lines = []
    for index, point in enumerate(route_points, 1):
        value = point.get("label") or point.get("value") or "—"
        lines.append(f"{index}. {value}")
    return "\n".join(lines)


def missing_field(draft: dict) -> Optional[str]:
    route = draft.get("route_points", [])
    if len(route) < 2:
        return "route"
    if not draft.get("time"):
        return "time"
    if not draft.get("car_class") and draft.get("special_request") is None:
        return "class"
    if not draft.get("passengers"):
        return "passengers"
    if draft.get("comment") is None:
        return "comment"
    if draft.get("special_request") is not None and not clean_text(draft.get("special_request", "")):
        return "special"
    return None


def confirmation_text(draft: dict) -> str:
    price = calculate_price(draft)
    special_line = ""
    if draft.get("special_request") is not None:
        special_line = f"\n✨ <b>Особый запрос:</b> {esc(draft.get('special_request'))}"
    return (
        "<b>Проверьте заказ</b>\n\n"
        f"📍 <b>Маршрут:</b>\n{esc(route_text(draft.get('route_points', [])))}\n\n"
        f"🕒 <b>Когда:</b> {esc(draft.get('time'))}\n"
        f"🚘 <b>Класс:</b> {esc(draft.get('car_class') or 'Неважно')}\n"
        f"💳 <b>Тариф:</b> {esc(draft.get('tariff'))}\n"
        f"💰 <b>Цена:</b> {esc(price)}\n"
        f"👥 <b>Пассажиры:</b> {esc(draft.get('passengers'))}\n"
        f"💬 <b>Пожелания:</b> {esc(draft.get('comment') or 'Без пожеланий')}"
        f"{special_line}"
    )


def order_public_text(order_id: str, order: dict) -> str:
    title = "✨ <b>ОСОБЫЙ ЗАПРОС</b>" if order.get("special_request") is not None else "🚖 <b>НОВЫЙ ЗАКАЗ</b>"
    return (
        f"{title} №{esc(order_id)}\n\n"
        f"📍 <b>Маршрут:</b>\n{esc(route_text(order.get('route_points', [])))}\n\n"
        f"🕒 <b>Когда:</b> {esc(order.get('time'))}\n"
        f"🚘 <b>Класс:</b> {esc(order.get('car_class') or 'Неважно')}\n"
        f"💳 <b>Тариф:</b> {esc(order.get('tariff'))}\n"
        f"💰 <b>Цена:</b> {esc(order.get('price'))}\n"
        f"👥 <b>Пассажиры:</b> {esc(order.get('passengers'))}\n"
        f"💬 <b>Пожелания:</b> {esc(order.get('comment') or 'Без пожеланий')}\n"
        f"✨ <b>Особый запрос:</b> {esc(order.get('special_request'))}\n\n"
        "Личные данные клиента скрыты."
    )


def contact_data_detected(text: str) -> bool:
    return bool(
        re.search(r"(?<!\d)(?:\+?\d[\s().-]*){10,15}(?!\d)", text or "")
        or re.search(r"(?<!\w)@[A-Za-z0-9_]{5,}", text or "")
        or re.search(r"(?:https?://)?(?:t\.me|telegram\.me)/\S+", text or "", re.I)
    )

# ================= OPENROUTER =================


def _openrouter_request(messages: list[dict[str, str]]) -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY не настроен")
    payload = json.dumps(
        {
            "model": OPENROUTER_MODEL,
            "messages": messages,
            "temperature": 0.1,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "X-Title": "VIP Taxi AI",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        data = json.loads(response.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"]


async def openrouter_chat(messages: list[dict[str, str]]) -> str:
    return await asyncio.to_thread(_openrouter_request, messages)


def extract_json(text: str) -> dict[str, Any]:
    value = text.strip()
    value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.I)
    value = re.sub(r"\s*```$", "", value)
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("JSON не найден")
    return json.loads(value[start:end + 1])


async def ai_update_draft(draft: dict, user_text: str) -> dict[str, Any]:
    system = f"""
Ты AI-диспетчер премиального такси в Москве. Сейчас {format_dt(now_moscow())} по Москве.
Тебе передают текущий черновик заказа и новое сообщение клиента.
Верни ТОЛЬКО JSON без markdown:
{{
  "route_points": [{{"label": "точный адрес или понятное название"}}] | null,
  "scheduled_text": string | null,
  "car_class": "Business|First|Lux|Минивэн" | null,
  "hours": integer | null,
  "passengers": integer | null,
  "comment_append": string | null,
  "special_request": string | null,
  "clear_special_request": boolean,
  "answer": string | null
}}

Правила:
- Сохраняй порядок маршрута. Первая точка — подача, последняя — конечная, между ними остановки.
- Если клиент добавляет точку, верни ПОЛНЫЙ обновлённый список route_points.
- Не выдумывай номер дома или терминал.
- «детское кресло», «бустер», «багаж», «табличка», «несколько остановок» — обычный comment_append, НЕ special_request.
- Rolls-Royce, Bentley, Maybach Pullman, кортеж, охрана, несколько машин, свадьба, делегация, редкая модель/цвет — special_request.
- Mercedes V-Class/V class/минивэн => car_class="Минивэн".
- Число пассажиров извлекай даже из фраз «двое взрослых и ребёнок».
- Если сообщение — ответ на последний вопрос, используй контекст черновика.
- Никогда не формируй цену: её считает Python.
"""
    user = json.dumps({"draft": draft, "message": user_text}, ensure_ascii=False)
    raw = await openrouter_chat([
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ])
    return extract_json(raw)


def parse_scheduled_text(text: str) -> tuple[str, str]:
    raw = clean_text(text, 120).lower().replace(",", " ").replace(" в ", " ")
    now = now_moscow()
    tm = re.search(r"(\d{1,2})(?::|\.)(\d{2})", raw)
    if tm:
        hour, minute = int(tm.group(1)), int(tm.group(2))
    else:
        hm = re.search(r"(?:в\s*)?(\d{1,2})\s*(?:утра|дня|вечера)?", raw)
        hour, minute = (int(hm.group(1)), 0) if hm else (0, 0)
        if "вечера" in raw and hour < 12:
            hour += 12
    if "завтра" in raw:
        date = (now + timedelta(days=1)).date()
    elif "послезавтра" in raw:
        date = (now + timedelta(days=2)).date()
    else:
        dm = re.search(r"(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?", raw)
        if dm:
            day, month = int(dm.group(1)), int(dm.group(2))
            year = int(dm.group(3)) if dm.group(3) else now.year
            if year < 100:
                year += 2000
            date = datetime(year, month, day, tzinfo=now.tzinfo).date()
        else:
            date = now.date()
    scheduled = datetime(date.year, date.month, date.day, hour, minute, tzinfo=now.tzinfo)
    if scheduled < now and "завтра" not in raw and "послезавтра" not in raw:
        scheduled += timedelta(days=1)
    return format_dt(scheduled), scheduled.isoformat()

# ================= КЛИЕНТСКИЙ AI-ДИАЛОГ =================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    user_id = str(update.effective_user.id)
    active = context.bot_data["active_by_user"].get(user_id)
    pending = context.bot_data["pending_by_client"].get(user_id)
    if active:
        await update.effective_message.reply_text(
            f"У вас активный заказ №{active}. Пишите сюда — сообщение получит водитель.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    if pending:
        await update.effective_message.reply_text(
            f"Заказ №{pending} ожидает водителя.", reply_markup=MAIN_KB
        )
        return
    context.user_data.pop("order_draft", None)
    await update.effective_message.reply_text(AI_WELCOME, reply_markup=MAIN_KB)


async def begin_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    context.user_data["order_draft"] = new_draft(update.effective_user, special=False)
    await update.effective_message.reply_text(
        "Опишите поездку одним сообщением: маршрут, дату, время, класс и количество пассажиров.",
        reply_markup=ReplyKeyboardRemove(),
    )


async def begin_special(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    context.user_data["order_draft"] = new_draft(update.effective_user, special=True)
    await update.effective_message.reply_text(
        "Опишите особый запрос: нужный автомобиль, цвет, цель поездки, маршрут, дату и длительность.",
        reply_markup=ReplyKeyboardRemove(),
    )


def class_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Business", callback_data="draft_class_Business"),
         InlineKeyboardButton("First", callback_data="draft_class_First")],
        [InlineKeyboardButton("Lux", callback_data="draft_class_Lux"),
         InlineKeyboardButton("Mercedes V-Class", callback_data="draft_class_Минивэн")],
        [InlineKeyboardButton("❌ Отмена", callback_data="draft_cancel")],
    ])


def wishes_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Без пожеланий", callback_data="draft_wish_none")],
        [InlineKeyboardButton("Детское кресло", callback_data="draft_wish_childseat"),
         InlineKeyboardButton("Бустер", callback_data="draft_wish_booster")],
        [InlineKeyboardButton("Встреча с табличкой", callback_data="draft_wish_sign"),
         InlineKeyboardButton("Есть багаж", callback_data="draft_wish_luggage")],
        [InlineKeyboardButton("✍️ Написать самостоятельно", callback_data="draft_wish_custom")],
        [InlineKeyboardButton("❌ Отмена", callback_data="draft_cancel")],
    ])


async def ask_next(update_or_query, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = get_draft(context)
    if not draft:
        return
    missing = missing_field(draft)
    message = update_or_query.message if hasattr(update_or_query, "message") else update_or_query.effective_message

    if missing == "route":
        draft["last_question"] = "route"
        await message.reply_text(
            "Укажите полный маршрут по порядку. Можно добавить несколько точек.\n"
            "Пример: Тверская 10 → Кутузовский 22 → Шереметьево."
        )
        return
    if missing == "time":
        draft["last_question"] = "time"
        await message.reply_text("Когда нужна машина? Укажите дату и время.")
        return
    if missing == "class":
        draft["last_question"] = "class"
        await message.reply_text("Какой класс автомобиля нужен?", reply_markup=class_keyboard())
        return
    if missing == "passengers":
        draft["last_question"] = "passengers"
        await message.reply_text("Сколько будет пассажиров?")
        return
    if missing == "comment":
        draft["last_question"] = "comment"
        await message.reply_text("Есть дополнительные пожелания?", reply_markup=wishes_keyboard())
        return
    if missing == "special":
        draft["last_question"] = "special"
        await message.reply_text("Опишите особый запрос подробнее.")
        return

    draft["status"] = "confirming"
    draft["price"] = calculate_price(draft)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data="draft_confirm")],
        [InlineKeyboardButton("✏️ Изменить", callback_data="draft_edit")],
        [InlineKeyboardButton("❌ Отменить", callback_data="draft_cancel")],
    ])
    await message.reply_text(confirmation_text(draft), parse_mode="HTML", reply_markup=kb)


async def client_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    user_id = str(update.effective_user.id)
    if context.bot_data["active_by_user"].get(user_id):
        await relay_active_message(update, context)
        return
    if context.bot_data["pending_by_client"].get(user_id):
        await update.effective_message.reply_text("Ваш заказ уже опубликован и ожидает водителя.")
        return

    text = clean_text(update.effective_message.text)
    if text in {"🚘 Заказать поездку", "✨ Особый запрос", "👨‍✈️ Стать водителем", "📋 Мой статус"}:
        return

    draft = get_draft(context)
    if not draft:
        draft = new_draft(update.effective_user, special=False)
        context.user_data["order_draft"] = draft

    thinking = await update.effective_message.reply_text("Секунду, уточняю заказ…")
    try:
        parsed = await ai_update_draft(draft, text)
    except Exception:
        logger.exception("Ошибка OpenRouter")
        await thinking.edit_text("AI временно недоступен. Попробуйте ещё раз через минуту.")
        return

    if parsed.get("route_points"):
        draft["route_points"] = parsed["route_points"]
    if parsed.get("scheduled_text"):
        try:
            draft["time"], draft["scheduled_at"] = parse_scheduled_text(parsed["scheduled_text"])
        except Exception:
            draft["time"] = clean_text(parsed["scheduled_text"])
    if parsed.get("car_class") in CAR_CLASSES:
        draft["car_class"] = parsed["car_class"]
    if parsed.get("hours"):
        draft["hours"] = int(parsed["hours"])
    if parsed.get("passengers"):
        draft["passengers"] = int(parsed["passengers"])
    if parsed.get("comment_append"):
        old = clean_text(draft.get("comment", ""))
        new = clean_text(parsed["comment_append"])
        draft["comment"] = ", ".join(x for x in (old, new) if x)
    if parsed.get("clear_special_request"):
        draft["special_request"] = None
    if parsed.get("special_request"):
        draft["special_request"] = clean_text(parsed["special_request"])
        draft["tariff"] = "Особый запрос"
    draft["tariff"] = detect_tariff(draft)
    await thinking.delete()
    await ask_next(update, context)


async def draft_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draft = get_draft(context)
    if not draft:
        await query.edit_message_text("Черновик заказа не найден. Отправьте /start.")
        return
    data = query.data

    if data == "draft_cancel":
        context.user_data.pop("order_draft", None)
        await query.edit_message_text("Заказ отменён.")
        await context.bot.send_message(query.from_user.id, "Выберите действие:", reply_markup=MAIN_KB)
        return
    if data.startswith("draft_class_"):
        draft["car_class"] = data.removeprefix("draft_class_")
        await query.edit_message_text(f"Класс выбран: {draft['car_class']}")
        await ask_next(query, context)
        return
    wishes = {
        "draft_wish_none": "",
        "draft_wish_childseat": "Детское кресло",
        "draft_wish_booster": "Бустер",
        "draft_wish_sign": "Встреча с табличкой",
        "draft_wish_luggage": "Есть багаж",
    }
    if data in wishes:
        draft["comment"] = wishes[data]
        await query.edit_message_text("Пожелания сохранены.")
        await ask_next(query, context)
        return
    if data == "draft_wish_custom":
        draft["comment"] = None
        draft["last_question"] = "comment"
        await query.edit_message_text("Напишите пожелания одним сообщением.")
        return
    if data == "draft_edit":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📍 Маршрут", callback_data="edit_route")],
            [InlineKeyboardButton("🕒 Дата и время", callback_data="edit_time")],
            [InlineKeyboardButton("🚘 Класс", callback_data="edit_class")],
            [InlineKeyboardButton("👥 Пассажиры", callback_data="edit_passengers")],
            [InlineKeyboardButton("💬 Пожелания", callback_data="edit_comment")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="edit_back")],
        ])
        await query.edit_message_text("Что изменить?", reply_markup=kb)
        return
    if data.startswith("edit_"):
        field = data.removeprefix("edit_")
        if field == "back":
            await query.edit_message_text(confirmation_text(draft), parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Подтвердить", callback_data="draft_confirm")],
                [InlineKeyboardButton("✏️ Изменить", callback_data="draft_edit")],
                [InlineKeyboardButton("❌ Отменить", callback_data="draft_cancel")],
            ]))
            return
        if field == "class":
            await query.edit_message_text("Выберите новый класс:", reply_markup=class_keyboard())
            return
        draft["last_question"] = field
        if field == "route":
            draft["route_points"] = []
            text = "Напишите новый маршрут по порядку."
        elif field == "time":
            draft["time"] = None
            text = "Напишите новую дату и время."
        elif field == "passengers":
            draft["passengers"] = None
            text = "Сколько будет пассажиров?"
        else:
            draft["comment"] = None
            text = "Напишите новые пожелания."
        await query.edit_message_text(text)
        return
    if data == "draft_confirm":
        await publish_order(query, context, draft)
        context.user_data.pop("order_draft", None)


async def publish_order(query, context: ContextTypes.DEFAULT_TYPE, draft: dict):
    ensure_storage(context)
    user_id = str(query.from_user.id)
    if context.bot_data["pending_by_client"].get(user_id):
        await query.answer("У вас уже есть опубликованный заказ.", show_alert=True)
        return
    order_id = uuid.uuid4().hex[:8].upper()
    order = {
        **draft,
        "client_id": query.from_user.id,
        "client_username": query.from_user.username or "",
        "status": "open",
        "driver_id": None,
        "price": calculate_price(draft),
        "created_at": now_iso(),
    }
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🟢 Взять заказ", callback_data=f"take_{order_id}")]])
    sent = await context.bot.send_message(
        ORDERS_CHAT_ID,
        order_public_text(order_id, order),
        parse_mode="HTML",
        reply_markup=kb,
        disable_web_page_preview=False,
    )
    order["group_message_id"] = sent.message_id
    context.bot_data["orders"][order_id] = order
    context.bot_data["pending_by_client"][user_id] = order_id
    await query.edit_message_text(
        f"✅ Заказ №{order_id} отправлен водителям. Сообщим, когда водитель его примет."
    )

# ================= СТАТУС КЛИЕНТА/ВОДИТЕЛЯ =================


async def my_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    driver = context.bot_data["drivers"].get(str(update.effective_user.id))
    if driver and driver.get("status") == "approved":
        await update.effective_message.reply_text(
            "✅ Вы зарегистрированы как водитель.\n"
            f"Класс: {driver.get('car_class')}\n"
            f"Автомобиль: {driver.get('car')} {driver.get('year')}\n"
            f"Госномер: {driver.get('plate')}"
        )
        return
    pending = any(
        app.get("user_id") == update.effective_user.id and app.get("status") == "pending"
        for app in context.bot_data["driver_apps"].values()
    )
    await update.effective_message.reply_text(
        "⏳ Анкета на проверке." if pending else "Вы пока не зарегистрированы как водитель."
    )

# ================= РЕГИСТРАЦИЯ ВОДИТЕЛЯ =================


async def reg_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    existing = context.bot_data["drivers"].get(str(update.effective_user.id))
    if existing and existing.get("status") == "approved":
        await update.effective_message.reply_text("Вы уже одобрены как водитель.")
        return ConversationHandler.END
    context.user_data["reg"] = {"document_photos": [], "car_photos": []}
    await update.effective_message.reply_text("Введите ФИО полностью:", reply_markup=ReplyKeyboardRemove())
    return REG_NAME


async def reg_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = clean_text(update.effective_message.text, 120)
    if len(name.split()) < 2:
        await update.effective_message.reply_text("Введите имя и фамилию.")
        return REG_NAME
    context.user_data["reg"]["name"] = name
    await update.effective_message.reply_text("Отправьте номер телефона:", reply_markup=PHONE_KB)
    return REG_PHONE


async def reg_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    raw = message.contact.phone_number if message.contact else message.text
    phone = normalize_phone(raw)
    if not phone:
        await message.reply_text("Неверный номер.")
        return REG_PHONE
    context.user_data["reg"]["phone"] = phone
    await message.reply_text("Введите марку и модель автомобиля:", reply_markup=ReplyKeyboardRemove())
    return REG_CAR


async def reg_car(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["reg"]["car"] = clean_text(update.effective_message.text, 120)
    await update.effective_message.reply_text("Введите год выпуска:")
    return REG_YEAR


async def reg_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    year = clean_text(update.effective_message.text, 10)
    if not re.fullmatch(r"19\d{2}|20\d{2}", year):
        await update.effective_message.reply_text("Введите год четырьмя цифрами.")
        return REG_YEAR
    context.user_data["reg"]["year"] = year
    await update.effective_message.reply_text("Введите госномер:")
    return REG_PLATE


async def reg_plate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["reg"]["plate"] = clean_text(update.effective_message.text.upper(), 20)
    await update.effective_message.reply_text("Выберите класс:", reply_markup=DRIVER_CLASS_KB)
    return REG_CLASS


async def reg_class(update: Update, context: ContextTypes.DEFAULT_TYPE):
    car_class = clean_text(update.effective_message.text, 30)
    if car_class not in CAR_CLASSES:
        await update.effective_message.reply_text("Выберите класс кнопкой.")
        return REG_CLASS
    context.user_data["reg"]["car_class"] = car_class
    await update.effective_message.reply_text(
        "Отправьте фото водительского удостоверения и СТС. Затем нажмите «Готово».",
        reply_markup=DONE_KB,
    )
    return REG_DOCUMENT_PHOTOS


async def reg_document_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reg = context.user_data.get("reg")
    if update.effective_message.photo:
        reg["document_photos"].append(update.effective_message.photo[-1].file_id)
        await update.effective_message.reply_text(f"Документ добавлен. Всего: {len(reg['document_photos'])}")
        return REG_DOCUMENT_PHOTOS
    if (update.effective_message.text or "").lower() == "готово":
        if len(reg["document_photos"]) < 2:
            await update.effective_message.reply_text("Нужно минимум 2 фото: права и СТС.")
            return REG_DOCUMENT_PHOTOS
        await update.effective_message.reply_text(
            "Теперь отправьте 2–6 фото автомобиля: кузов и салон. "
            "Именно эти фото будут показаны клиенту. Затем нажмите «Готово».",
            reply_markup=DONE_KB,
        )
        return REG_CAR_PHOTOS
    return REG_DOCUMENT_PHOTOS


async def reg_car_photos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reg = context.user_data.get("reg")
    if update.effective_message.photo:
        if len(reg["car_photos"]) >= 6:
            await update.effective_message.reply_text("Максимум 6 фото автомобиля.")
            return REG_CAR_PHOTOS
        reg["car_photos"].append(update.effective_message.photo[-1].file_id)
        await update.effective_message.reply_text(f"Фото автомобиля добавлено. Всего: {len(reg['car_photos'])}")
        return REG_CAR_PHOTOS
    if (update.effective_message.text or "").lower() == "готово":
        if len(reg["car_photos"]) < 2:
            await update.effective_message.reply_text("Нужно минимум 2 фото автомобиля.")
            return REG_CAR_PHOTOS
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Отправить", callback_data="reg_send"),
            InlineKeyboardButton("❌ Отмена", callback_data="reg_cancel"),
        ]])
        await update.effective_message.reply_text(
            f"ФИО: {reg['name']}\nАвтомобиль: {reg['car']} {reg['year']}\n"
            f"Класс: {reg['car_class']}\nДокументы: {len(reg['document_photos'])}\n"
            f"Фото автомобиля: {len(reg['car_photos'])}\n\nОтправить анкету?",
            reply_markup=kb,
        )
        return REG_CONFIRM
    return REG_CAR_PHOTOS


async def reg_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    query = update.callback_query
    await query.answer()
    if query.data == "reg_cancel":
        context.user_data.pop("reg", None)
        await query.edit_message_text("Регистрация отменена.")
        return ConversationHandler.END
    reg = context.user_data.pop("reg", None)
    if not reg:
        await query.edit_message_text("Анкета не найдена.")
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
    text = (
        f"👨‍✈️ <b>НОВАЯ АНКЕТА №{app_id}</b>\n\n"
        f"ФИО: {esc(application['name'])}\n"
        f"Телефон: {esc(application['phone'])}\n"
        f"Автомобиль: {esc(application['car'])} {esc(application['year'])}\n"
        f"Госномер: {esc(application['plate'])}\n"
        f"Класс: {esc(application['car_class'])}\n"
        f"Документы: {len(application['document_photos'])}\n"
        f"Фото автомобиля: {len(application['car_photos'])}"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Одобрить", callback_data=f"approve_{app_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"reject_{app_id}"),
    ]])
    await context.bot.send_message(MODERATION_CHAT_ID, text, parse_mode="HTML", reply_markup=kb)
    for photo in application["document_photos"]:
        await context.bot.send_photo(MODERATION_CHAT_ID, photo, caption=f"Анкета {app_id}: документ")
    for photo in application["car_photos"]:
        await context.bot.send_photo(MODERATION_CHAT_ID, photo, caption=f"Анкета {app_id}: автомобиль")
    await query.edit_message_text(f"✅ Анкета №{app_id} отправлена модератору.")
    return ConversationHandler.END


async def moderate_driver(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    query = update.callback_query
    action, app_id = query.data.split("_", 1)
    application = context.bot_data["driver_apps"].get(app_id)
    if not application or application.get("status") != "pending":
        await query.answer("Анкета уже обработана.", show_alert=True)
        return
    if action == "reject":
        application["status"] = "rejected"
        await query.edit_message_text((query.message.text or "") + "\n\n❌ ОТКЛОНЕНО")
        await context.bot.send_message(application["user_id"], "❌ Ваша заявка отклонена.")
        return
    application["status"] = "approved"
    context.bot_data["drivers"][str(application["user_id"])] = {
        "user_id": application["user_id"],
        "name": application["name"],
        "phone": application["phone"],
        "car": application["car"],
        "year": application["year"],
        "plate": application["plate"],
        "car_class": application["car_class"],
        "car_photos": application["car_photos"],
        "status": "approved",
        "approved_at": now_iso(),
    }
    await query.edit_message_text((query.message.text or "") + "\n\n✅ ОДОБРЕНО")
    try:
        invite = await context.bot.create_chat_invite_link(
            ORDERS_CHAT_ID,
            name=f"driver_{application['user_id']}",
            expire_date=datetime.now(timezone.utc) + timedelta(hours=24),
            creates_join_request=True,
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚖 Вступить в группу заказов", url=invite.invite_link)]])
        await context.bot.send_message(application["user_id"], "✅ Ваша заявка одобрена.", reply_markup=kb)
    except TelegramError:
        await context.bot.send_message(application["user_id"], "✅ Ваша заявка одобрена.")


async def handle_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    request = update.chat_join_request
    driver = context.bot_data["drivers"].get(str(request.from_user.id))
    if request.chat.id != ORDERS_CHAT_ID:
        return
    if driver and driver.get("status") == "approved":
        await context.bot.approve_chat_join_request(ORDERS_CHAT_ID, request.from_user.id)
    else:
        await context.bot.decline_chat_join_request(ORDERS_CHAT_ID, request.from_user.id)

# ================= ПРИНЯТИЕ И ВЫПОЛНЕНИЕ ЗАКАЗА =================


async def take_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    query = update.callback_query
    order_id = query.data.removeprefix("take_")
    order = context.bot_data["orders"].get(order_id)
    driver = context.bot_data["drivers"].get(str(query.from_user.id))
    if not driver or driver.get("status") != "approved":
        await query.answer("Сначала получите одобрение.", show_alert=True)
        return
    if not order or order.get("status") != "open":
        await query.answer("Заказ уже недоступен.", show_alert=True)
        return
    required = order.get("car_class")
    if order.get("special_request") is None and required and required != driver.get("car_class"):
        await query.answer(f"Требуется класс {required}.", show_alert=True)
        return
    if context.bot_data["active_by_user"].get(str(query.from_user.id)):
        await query.answer("Сначала завершите текущий заказ.", show_alert=True)
        return

    order["status"] = "taken"
    order["driver_id"] = query.from_user.id
    order["driver_name"] = driver.get("name")
    context.bot_data["pending_by_client"].pop(str(order["client_id"]), None)
    context.bot_data["active_by_user"][str(order["client_id"])] = order_id
    context.bot_data["active_by_user"][str(query.from_user.id)] = order_id
    try:
        await context.bot.delete_message(ORDERS_CHAT_ID, query.message.message_id)
    except TelegramError:
        pass

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📍 Я на месте", callback_data=f"arrived_{order_id}")],
        [InlineKeyboardButton("▶️ Начать поездку", callback_data=f"starttrip_{order_id}")],
        [InlineKeyboardButton("✅ Завершить заказ", callback_data=f"finish_{order_id}")],
    ])
    await context.bot.send_message(
        query.from_user.id,
        order_public_text(order_id, order),
        parse_mode="HTML",
        reply_markup=kb,
    )
    await context.bot.send_message(
        order["client_id"],
        f"🚘 Водитель принял заказ №{order_id}.\n\n"
        f"Водитель: {driver.get('name')}\n"
        f"Автомобиль: {driver.get('car')} {driver.get('year')}\n"
        f"Класс: {driver.get('car_class')}\n"
        f"Госномер: {driver.get('plate')}\n\n"
        "Ниже фотографии автомобиля из подтверждённой анкеты.",
    )
    for photo in driver.get("car_photos", []):
        try:
            await context.bot.send_photo(order["client_id"], photo)
        except TelegramError:
            logger.exception("Не удалось отправить фото автомобиля")


async def arrived_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    order_id = query.data.removeprefix("arrived_")
    order = context.bot_data["orders"].get(order_id)
    if not order or query.from_user.id != order.get("driver_id"):
        await query.answer("Недоступно.", show_alert=True)
        return
    await query.answer("Клиент уведомлён.")
    await context.bot.send_message(order["client_id"], f"📍 Водитель прибыл по заказу №{order_id}.")


async def start_trip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    order_id = query.data.removeprefix("starttrip_")
    order = context.bot_data["orders"].get(order_id)
    if not order or query.from_user.id != order.get("driver_id"):
        await query.answer("Недоступно.", show_alert=True)
        return
    order["started_at"] = now_iso()
    await query.answer("Поездка началась.")
    await context.bot.send_message(order["client_id"], f"▶️ Поездка по заказу №{order_id} началась.")


async def finish_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    query = update.callback_query
    order_id = query.data.removeprefix("finish_")
    order = context.bot_data["orders"].get(order_id)
    if not order or query.from_user.id != order.get("driver_id"):
        await query.answer("Недоступно.", show_alert=True)
        return
    order["status"] = "completed"
    order["completed_at"] = now_iso()
    client_id, driver_id = order["client_id"], order["driver_id"]
    context.bot_data["active_by_user"].pop(str(client_id), None)
    context.bot_data["active_by_user"].pop(str(driver_id), None)
    context.bot_data["orders"].pop(order_id, None)
    context.bot_data["client_history"].setdefault(str(client_id), []).append(order)
    await query.answer("Заказ завершён.")
    await context.bot.send_message(client_id, "✅ Заказ завершён.", reply_markup=MAIN_KB)
    await context.bot.send_message(driver_id, "✅ Заказ завершён.", reply_markup=MAIN_KB)

# ================= АНОНИМНАЯ ПЕРЕПИСКА =================


async def relay_active_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    user_id = update.effective_user.id
    order_id = context.bot_data["active_by_user"].get(str(user_id))
    order = context.bot_data["orders"].get(order_id) if order_id else None
    if not order:
        return
    recipient = order["driver_id"] if user_id == order["client_id"] else order["client_id"]
    message = update.effective_message
    text = message.text or message.caption or ""
    if message.contact or contact_data_detected(text):
        try:
            await message.delete()
        except TelegramError:
            pass
        await context.bot.send_message(user_id, "Передача контактов запрещена. Общайтесь через бота.")
        return
    try:
        await context.bot.copy_message(recipient, message.chat_id, message.message_id)
    except TelegramError:
        await context.bot.send_message(user_id, "Не удалось передать сообщение.")


async def relay_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_storage(context)
    if context.bot_data["active_by_user"].get(str(update.effective_user.id)):
        await relay_active_message(update, context)

# ================= СЛУЖЕБНОЕ =================


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("order_draft", None)
    context.user_data.pop("reg", None)
    await update.effective_message.reply_text("Действие отменено.", reply_markup=MAIN_KB)
    return ConversationHandler.END


async def chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"ID этого чата: {update.effective_chat.id}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Необработанное исключение", exc_info=context.error)

# ================= ЗАПУСК =================


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Укажите BOT_TOKEN")
    persistence_path = Path(PERSISTENCE_PATH)
    persistence_path.parent.mkdir(parents=True, exist_ok=True)
    persistence = PicklePersistence(filepath=PERSISTENCE_PATH)
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .concurrent_updates(False)
        .build()
    )

    registration = ConversationHandler(
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
            REG_DOCUMENT_PHOTOS: [
                MessageHandler(filters.PHOTO, reg_document_photos),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_document_photos),
            ],
            REG_CAR_PHOTOS: [
                MessageHandler(filters.PHOTO, reg_car_photos),
                MessageHandler(filters.TEXT & ~filters.COMMAND, reg_car_photos),
            ],
            REG_CONFIRM: [CallbackQueryHandler(reg_confirm, pattern=r"^reg_(send|cancel)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
        name="driver_registration",
        persistent=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chat_id))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(MessageHandler(filters.Regex(r"^🚘 Заказать поездку$"), begin_order))
    app.add_handler(MessageHandler(filters.Regex(r"^✨ Особый запрос$"), begin_special))
    app.add_handler(MessageHandler(filters.Regex(r"^📋 Мой статус$"), my_status))
    app.add_handler(registration)

    app.add_handler(CallbackQueryHandler(draft_callback, pattern=r"^(draft_|edit_).+"))
    app.add_handler(CallbackQueryHandler(moderate_driver, pattern=r"^(approve|reject)_[A-F0-9]{8}$"))
    app.add_handler(CallbackQueryHandler(take_order, pattern=r"^take_[A-F0-9]{8}$"))
    app.add_handler(CallbackQueryHandler(arrived_order, pattern=r"^arrived_[A-F0-9]{8}$"))
    app.add_handler(CallbackQueryHandler(start_trip, pattern=r"^starttrip_[A-F0-9]{8}$"))
    app.add_handler(CallbackQueryHandler(finish_order, pattern=r"^finish_[A-F0-9]{8}$"))
    app.add_handler(ChatJoinRequestHandler(handle_join_request))

    # Один текстовый обработчик клиента: либо дополняет AI-черновик, либо пересылает активному водителю.
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, client_text), group=1)
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & (filters.PHOTO | filters.VIDEO | filters.VOICE | filters.AUDIO | filters.Document.ALL | filters.LOCATION | filters.CONTACT),
            relay_media,
        ),
        group=1,
    )

    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
