# -*- coding: utf-8 -*-
"""
Нотификатор MAX -> Telegram.
Слушает новые сообщения в твоём аккаунте MAX и шлёт в Telegram короткий СИГНАЛ
"есть новое в таком-то чате" — только по незамьюченным чатам, без текста.
"""

import asyncio
import logging
import os
import subprocess
import sys
import threading
import time

# Под pythonw.exe (запуск без консоли) sys.stdout и sys.stderr равны None.
# pymax вешает свой логгер на sys.stderr, и при первой же записи лога
# StreamHandler обращается к None.write(...) -> AttributeError. Эта ошибка
# не перехватывается (см. ниже про logging.raiseExceptions) и роняет процесс.
# Поэтому ДО импорта pymax подменяем отсутствующие потоки: stderr -> файл,
# stdout -> пустышка. Делаем это перед `from pymax import ...`, т.к. логгер
# может захватить ссылку на sys.stderr уже на этапе импорта.
if sys.stdout is None or sys.stderr is None:
    os.makedirs("cache", exist_ok=True)
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(
            os.path.join("cache", "stderr.log"),
            "a",
            encoding="utf-8",
            buffering=1,
        )

import aiohttp
from pymax import Client, ExtraConfig, Message, SyncOverrides
from pymax.auth import ConsolePasswordProvider, ConsoleSmsCodeProvider
import pymax.app as _pymax_app
from pymax.protocol.enums import Opcode
from pymax.types.domain import ControlAttachment
from pymax.types.domain.attachments.enums import AttachmentType
from pymax.types.domain.sync import DEFAULT_CONFIG_HASH

import config

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Логи всегда пишем в файл (приложение фоновое, консоли может не быть —
# при запуске через pythonw sys.stderr is None, и StreamHandler бы падал).
# Если консоль всё же есть (ручной запуск, первый логин) — дублируем в неё.
os.makedirs("cache", exist_ok=True)
LOG_PATH = os.path.join("cache", "notifier.log")
_handlers: list[logging.Handler] = [
    logging.FileHandler(LOG_PATH, encoding="utf-8")
]
if sys.stderr is not None:
    _handlers.append(logging.StreamHandler())
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("max2tg")

# --- Mute (не беспокоить) -------------------------------------------------
# Признак mute приходит НЕ в объекте чата, а в персональном user-config внутри
# LOGIN-ответа: config.chats.{chatId}.dontDisturbUntil. Значения:
#   -1  -> заглушён навсегда
#    0  -> не заглушён
#   >0  -> заглушён до этого момента (Unix-время в мс)
# pymax парсит login в модель и секцию config выбрасывает, поэтому перехватываем
# сырой ответ LOGIN через monkeypatch App.invoke и читаем карту ОДИН раз при
# запуске (config_hash=DEFAULT ниже заставляет сервер прислать config целиком).
# chat_id -> dontDisturbUntil
_muted_until: dict[int, int] = {}


def _harvest_mute(obj) -> None:
    """Ищет в произвольной структуре карты вида {chatId: {dontDisturbUntil}}
    и наполняет _muted_until. Структуро-независимо: не завязано на точный путь."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict) and "dontDisturbUntil" in v:
                try:
                    _muted_until[int(k)] = v["dontDisturbUntil"]
                except (TypeError, ValueError):
                    pass
            else:
                _harvest_mute(v)
    elif isinstance(obj, list):
        for item in obj:
            _harvest_mute(item)


def is_muted_now(chat_id) -> bool:
    """Заглушён ли чат сейчас по данным MAX (config.chats.dontDisturbUntil)."""
    if not config.RESPECT_MAX_MUTE:
        return False
    v = _muted_until.get(chat_id)
    if v is None or v == 0:
        return False
    if v == -1:
        return True
    return v > time.time() * 1000  # заглушён до момента v (мс)


_orig_invoke = _pymax_app.App.invoke


async def _invoke_capture(self, opcode, payload, *args, **kwargs):
    resp = await _orig_invoke(self, opcode, payload, *args, **kwargs)
    if opcode == Opcode.LOGIN:
        try:
            _muted_until.clear()
            _harvest_mute((resp.payload or {}).get("config"))
            muted = sum(1 for cid in _muted_until if is_muted_now(cid))
            log.info("mute-конфиг загружен: %d чатов, из них заглушено %d",
                     len(_muted_until), muted)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось разобрать mute-конфиг из LOGIN")
    return resp


_pymax_app.App.invoke = _invoke_capture

SESSION_PATH = os.path.join("cache", "main.db")

client = Client(
    phone=config.MAX_PHONE,
    work_dir="cache",
    session_name="main.db",
    # Встроенная авторизация: SMS-код и 2FA-пароль вводятся прямо в консоли при
    # первом запуске. Делается один раз, дальше используется сохранённая в
    # cache/main.db сессия.
    sms_code_provider=ConsoleSmsCodeProvider(),
    password_provider=ConsolePasswordProvider(),
    # Ничего не тянем списком целиком: имя чата/собеседника подтягиваем лениво
    # (по одному, при первом сообщении из него) и кешируем — см. build_ping_text().
    # telemetry=False — не слать телеметрию в MAX.
    # config_hash=DEFAULT — чтобы сервер прислал полный config с mute-настройками
    # (иначе при сохранённом хеше config приходит дельтой и mute не виден).
    extra_config=ExtraConfig(
        telemetry=False,
        sync=SyncOverrides(config_hash=DEFAULT_CONFIG_HASH),
    ),
)

_http: aiohttp.ClientSession | None = None
# chat_id -> время последнего отправленного пинга (для антиспама)
_last_ping: dict[int, float] = {}


async def send_to_telegram(text: str) -> None:
    if _http is None:
        return
    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": config.TELEGRAM_USER_ID,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        async with _http.post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=20)
        ) as resp:
            if resp.status != 200:
                log.error("Telegram вернул %s: %s", resp.status, await resp.text())
    except Exception as e:  # noqa: BLE001
        log.error("Не удалось отправить в Telegram: %s", e)


def get_chat(chat_id):
    for c in (client.chats or []):
        if getattr(c, "id", None) == chat_id:
            return c
    return None


# Кеши имён (чтобы не дёргать сеть повторно): отдельно по группам/каналам и по
# пользователям — их id лежат в разных пространствах (у диалога id чата это
# XOR двух пользователей, поэтому по chat_id собеседника не вычислить).
_chat_title_cache: dict[int, str] = {}
_user_name_cache: dict[int, str] = {}


def _user_display_name(user) -> str | None:
    """Достаёт читаемое имя из объекта User (поле names: list[Name])."""
    if user is None:
        return None
    for n in getattr(user, "names", None) or []:
        full = getattr(n, "name", None)
        if full:
            return full
        first = getattr(n, "first_name", None)
        last = getattr(n, "last_name", None)
        if first or last:
            return " ".join(p for p in (first, last) if p)
    return None


async def _group_title(chat_id, chat) -> str | None:
    """Название группы/канала: из готового объекта чата, иначе тянем ОДИН чат."""
    if chat_id in _chat_title_cache:
        return _chat_title_cache[chat_id]
    title = getattr(chat, "title", None) if chat is not None else None
    if title is None:
        try:
            fetched = await client.get_chat(chat_id)
            title = getattr(fetched, "title", None)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось получить чат %s", chat_id)
    if title:
        _chat_title_cache[chat_id] = title
    return title


async def _sender_name(sender_id) -> str | None:
    """Имя пользователя по id: из кеша клиента, иначе тянем ОДНОГО пользователя."""
    if sender_id in _user_name_cache:
        return _user_name_cache[sender_id]
    name = None
    try:
        user = client.get_cached_user(sender_id) or await client.get_user(sender_id)
        name = _user_display_name(user)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось получить пользователя %s", sender_id)
    if name:
        _user_name_cache[sender_id] = name
    return name


def _is_service_chat(chat) -> bool:
    """Служебный чат MAX (коды входа, системные сообщения)."""
    opts = getattr(chat, "options", None) if chat is not None else None
    return isinstance(opts, dict) and bool(opts.get("SERVICE_CHAT"))


def _is_service_event(message) -> bool:
    """Системное событие чата, а не настоящее сообщение: «X теперь в MAX»,
    «X вышел из чата», добавление/удаление участника и т.п. MAX отдаёт такие
    события сообщением с управляющим вложением ControlAttachment (type=CONTROL);
    обычные текстовые/медиа-сообщения его не несут. Признак структурный —
    не завязан на текст или язык."""
    for a in getattr(message, "attaches", None) or []:
        if isinstance(a, ControlAttachment):
            return True
        if getattr(a, "type", None) == AttachmentType.CONTROL:
            return True
    return False


# Подписи для медиа-сообщений без текста (когда включён INCLUDE_MESSAGE_TEXT).
_ATTACH_LABELS = {
    AttachmentType.PHOTO: "📷 Фото",
    AttachmentType.VIDEO: "🎬 Видео",
    AttachmentType.FILE: "📎 Файл",
    AttachmentType.STICKER: "🩷 Стикер",
    AttachmentType.AUDIO: "🎤 Голосовое сообщение",
    AttachmentType.CONTACT: "👤 Контакт",
    AttachmentType.CALL: "📞 Звонок",
    AttachmentType.SHARE: "🔗 Ссылка",
    AttachmentType.INLINE_KEYBOARD: "Сообщение с кнопками",
}


def _message_body(message) -> str:
    """Тело сообщения для режима INCLUDE_MESSAGE_TEXT: сам текст (обрезанный до
    MESSAGE_TEXT_LIMIT), а для медиа без текста — пометка вида «📷 Фото»."""
    text = (getattr(message, "text", "") or "").strip()
    if text:
        limit = config.MESSAGE_TEXT_LIMIT
        if limit and len(text) > limit:
            text = text[:limit].rstrip() + "…"
        return text
    for a in getattr(message, "attaches", None) or []:
        label = _ATTACH_LABELS.get(getattr(a, "type", None))
        if label:
            return label
    return "[без текста]"


async def _ping_header(message, chat) -> str:
    """Заголовок сигнала: для группы/канала — её название, для личного диалога —
    имя собеседника (это message.sender, т.к. свои сообщения уже отфильтрованы).
    Боты в личке отображаются как обычный отправитель — по их имени."""
    chat_id = message.chat_id

    # Служебный чат MAX (если объект чата под рукой и помечен SERVICE_CHAT).
    if _is_service_chat(chat):
        return "🔔 Новое сообщение в «Служебный чат MAX»"

    # Группа/канал (id отрицательный).
    if isinstance(chat_id, int) and chat_id < 0:
        title = await _group_title(chat_id, chat)
        return f"🔔 Новое сообщение в «{title}»" if title else f"🔔 Новое сообщение в чате {chat_id}"

    # Личный диалог: показываем имя отправителя (собеседника).
    sender = getattr(message, "sender", None)
    if sender is not None:
        name = await _sender_name(sender)
        if name:
            return f"🔔 Новое сообщение от «{name}»"
    return f"🔔 Новое сообщение в чате {chat_id}"


async def build_ping_text(message, chat) -> str:
    """Полный текст для Telegram: заголовок-сигнал, а при INCLUDE_MESSAGE_TEXT —
    плюс сам текст сообщения (или пометка о медиа) следующей строкой."""
    header = await _ping_header(message, chat)
    if config.INCLUDE_MESSAGE_TEXT:
        body = _message_body(message)
        if body:
            return f"{header}\n{body}"
    return header


def should_notify(chat_id, chat) -> bool:
    if config.WATCHED_CHAT_IDS and chat_id not in config.WATCHED_CHAT_IDS:
        return False
    if chat_id in config.IGNORED_CHAT_IDS:
        return False
    if is_muted_now(chat_id):
        return False
    return True


def cooldown_ok(chat_id) -> bool:
    now = time.monotonic()
    last = _last_ping.get(chat_id, 0.0)
    if now - last < config.COOLDOWN_SECONDS:
        return False
    _last_ping[chat_id] = now
    return True


@client.on_message()
async def handle(message: Message, client: Client) -> None:
    try:
        chat_id = getattr(message, "chat_id", None)
        if chat_id is None:
            return

        # пропускаем собственные сообщения (отправленные тобой с телефона).
        # ВАЖНО: id аккаунта лежит в me.contact.id, а НЕ в me.id (у Profile
        # поля id нет) — иначе фильтр не срабатывает и прилетает пинг на своё же.
        me = getattr(client, "me", None)
        contact = getattr(me, "contact", None)
        my_id = getattr(contact, "id", None)
        if my_id is not None and getattr(message, "sender", None) == my_id:
            return

        # Системные события чата («X теперь в MAX», «X вышел из чата» и т.п.) —
        # это не сообщения, пинговать по ним не нужно.
        if config.SKIP_SERVICE_MESSAGES and _is_service_event(message):
            return

        chat = get_chat(chat_id)
        if not should_notify(chat_id, chat):
            return
        # В режиме с текстом сообщения шлём каждое; антиспам нужен только для
        # «голых» сигналов, где десять сообщений подряд схлопываются в один пинг.
        if not config.INCLUDE_MESSAGE_TEXT and not cooldown_ok(chat_id):
            return

        text = await build_ping_text(message, chat)
        await send_to_telegram(text)
        log.info("→ TG | пинг по чату %s | %s", chat_id, text)
    except Exception:  # noqa: BLE001
        log.exception("Ошибка при обработке сообщения")


@client.on_start()
async def on_start(client: Client) -> None:
    log.info("MAX userbot запущен, слушаю входящие...")


async def main() -> None:
    global _http
    async with aiohttp.ClientSession() as session:
        _http = session
        await send_to_telegram("✅ Нотификатор MAX → Telegram запущен")
        await client.start()


# --- Системный трей ------------------------------------------------------
# Приложение фоновое и без консоли (запуск через pythonw / start_hidden.vbs).
# Чтобы его было видно и можно было закрыть — показываем значок в трее.
# asyncio-цикл крутится в фоновом потоке, pystray.Icon.run() блокирует
# главный поток. «Выход» отменяет основную задачу — client.start() при этом
# завершается, а aiohttp-сессия закрывается через `async with`.
_loop: asyncio.AbstractEventLoop | None = None
_main_task: asyncio.Task | None = None


def _make_tray_image():
    """Простой значок-«колокольчик» (генерируем, чтобы не таскать .ico)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    gold = (255, 196, 0, 255)
    # купол колокольчика
    d.pieslice((14, 12, 50, 48), 180, 360, fill=gold)
    d.rectangle((14, 30, 50, 44), fill=gold)
    # юбка и язычок
    d.polygon([(10, 44), (54, 44), (48, 50), (16, 50)], fill=gold)
    d.ellipse((28, 50, 36, 58), fill=gold)
    return img


def _run_asyncio() -> None:
    """Фоновый поток: гоняем основной цикл уведомлятора."""
    global _loop, _main_task
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _main_task = _loop.create_task(main())
    try:
        _loop.run_until_complete(_main_task)
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        log.exception("Основной цикл аварийно завершился")
    finally:
        _loop.close()


def _open_log(icon, item) -> None:  # noqa: ARG001
    path = os.path.abspath(LOG_PATH)
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:  # noqa: BLE001
        log.exception("Не удалось открыть лог")


def _quit(icon, item) -> None:  # noqa: ARG001
    log.info("Выход по запросу из трея")
    if _loop is not None and _main_task is not None and not _main_task.done():
        _loop.call_soon_threadsafe(_main_task.cancel)
    icon.stop()


def run_with_tray() -> None:
    import pystray

    worker = threading.Thread(target=_run_asyncio, name="notifier", daemon=True)
    worker.start()

    menu = pystray.Menu(
        pystray.MenuItem("Открыть лог", _open_log),
        pystray.MenuItem("Выход", _quit),
    )
    icon = pystray.Icon(
        "max2tg",
        _make_tray_image(),
        "Нотификатор MAX → Telegram",
        menu,
    )
    icon.run()
    worker.join(timeout=10)


if __name__ == "__main__":
    # Трей поднимаем только на Windows и только когда сессия уже есть. Первый
    # запуск (нет cache/main.db) и явный --login проходят в консоли: там нужно
    # ввести SMS-код и 2FA-пароль. На Linux/macOS — headless-режим (--no-tray),
    # значок в трее не используем (см. README, автозапуск через systemd).
    force_console = "--no-tray" in sys.argv or "--login" in sys.argv
    use_tray = (
        sys.platform == "win32"
        and not force_console
        and os.path.exists(SESSION_PATH)
    )
    if use_tray:
        run_with_tray()
    else:
        asyncio.run(main())
