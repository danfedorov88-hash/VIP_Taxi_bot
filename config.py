# -*- coding: utf-8 -*-

import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MODERATION_CHAT_ID = int(os.getenv("MODERATION_CHAT_ID", "-5062249297"))
ORDERS_CHAT_ID = int(os.getenv("ORDERS_CHAT_ID", "-1003446115764"))
PERSISTENCE_PATH = os.getenv(
    "PERSISTENCE_PATH",
    "/opt/vip-taxi-bot/bot_state.pickle",
)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL",
    "openai/gpt-4.1-mini",
)
