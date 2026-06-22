# -*- coding: utf-8 -*-
"""
Помощник: узнать свой TELEGRAM_CHAT_ID.
1) Напиши своему боту в Telegram любое сообщение (например, "привет").
2) Запусти:  python get_telegram_chat_id.py
3) Скопируй найденный chat_id в config.py.
"""

import json
import urllib.request

import config


def main() -> None:
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/getUpdates"
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.load(r)

    if not data.get("ok"):
        print("Ошибка ответа Telegram:", data)
        return

    results = data.get("result", [])
    if not results:
        print(
            "Обновлений нет. Сначала напиши своему боту любое сообщение, "
            "затем снова запусти этот скрипт."
        )
        return

    seen = {}
    for upd in results:
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is not None and cid not in seen:
            name = chat.get("title") or chat.get("first_name") or chat.get("username") or ""
            seen[cid] = name

    print("Найденные chat_id:")
    for cid, name in seen.items():
        print(f"  {cid}  {name}")
    print("\nВпиши нужный id в config.py -> TELEGRAM_CHAT_ID")


if __name__ == "__main__":
    main()
