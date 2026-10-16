from __future__ import annotations

import argparse
import asyncio
import html
import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env", override=False)

DEFAULT_START_TEXT = "Assalomu alaykum! Kino kodini yuboring 🎬"
configured_data_dir = os.getenv("DATA_DIR", "").strip()
DATA_DIR = (
    Path(configured_data_dir).expanduser()
    if configured_data_dir
    else Path.home() / ".local" / "share" / "tamosho-kino-bot"
)
configured_database_path = os.getenv("DATABASE_PATH", "").strip()
DATABASE_PATH = (
    Path(configured_database_path).expanduser()
    if configured_database_path
    else DATA_DIR / "bot.sqlite3"
)
WRITE_LOCK = asyncio.Lock()
router = Router()
active_broadcasts: dict[int, asyncio.Event] = {}
logger = logging.getLogger("tamosho_bot")


class AdminFlow(StatesGroup):
    add_movie_code = State()
    add_movie_name = State()
    add_movie_description = State()
    add_movie_video = State()
    delete_movie_search = State()
    edit_movie_search = State()
    edit_movie_value = State()
    edit_movie_video = State()
    movie_search = State()
    add_channel = State()
    add_admin = State()
    remove_admin = State()
    start_text = State()
    broadcast_message = State()
    broadcast_confirm = State()


class SecretRedactionFilter(logging.Filter):
    """Prevent Telegram API URLs or exception details leaking the bot token."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = "Log message could not be rendered."
        token = os.getenv("BOT_TOKEN", "").strip()
        if token:
            rendered = rendered.replace(token, "[REDACTED]")
        record.msg = rendered
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return True


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(SecretRedactionFilter())


@asynccontextmanager
async def database() -> AsyncIterator[aiosqlite.Connection]:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(DATABASE_PATH, timeout=15)
    connection.row_factory = aiosqlite.Row
    await connection.execute("PRAGMA busy_timeout = 15000")
    await connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        await connection.close()


async def initialize_database() -> None:
    async with database() as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                is_blocked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS movies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                file_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS channels (
                channel_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                invite_url TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        await db.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            ("start_text", DEFAULT_START_TEXT),
        )
        await db.commit()


async def write(query: str, parameters: tuple[Any, ...] = ()) -> int:
    async with WRITE_LOCK:
        async with database() as db:
            cursor = await db.execute(query, parameters)
            await db.commit()
            return cursor.rowcount


async def fetch_one(
    query: str, parameters: tuple[Any, ...] = ()
) -> aiosqlite.Row | None:
    async with database() as db:
        cursor = await db.execute(query, parameters)
        return await cursor.fetchone()


async def fetch_all(
    query: str, parameters: tuple[Any, ...] = ()
) -> list[aiosqlite.Row]:
    async with database() as db:
        cursor = await db.execute(query, parameters)
        return await cursor.fetchall()


async def is_admin(user_id: int) -> bool:
    primary_id = int(os.getenv("ADMIN_ID", "0") or "0")
    if user_id == primary_id:
        return True
    return (
        await fetch_one("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    ) is not None


async def require_admin(
    callback: CallbackQuery | None = None,
    message: Message | None = None,
    state: FSMContext | None = None,
) -> bool:
    user = callback.from_user if callback else (message.from_user if message else None)
    if user and await is_admin(user.id):
        return True
    if state:
        await state.clear()
    if callback:
        await callback.answer("Bu amal faqat adminlar uchun.", show_alert=True)
    elif message:
        await message.answer("Bu amal faqat adminlar uchun.")
    return False


def button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[button("❌ Bekor qilish", "admin:cancel")]]
    )


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("📊 Statistika", "admin:stats")],
            [
                button("🎞 Kino qo‘shish", "admin:add_movie"),
                button("🗑 Kino o‘chirish", "admin:delete_movie"),
            ],
            [
                button("✏️ Kino tahrirlash", "admin:edit_movie"),
                button("🔎 Kino qidirish", "admin:search_movie"),
            ],
            [
                button("📢 Majburiy obuna kanallari", "admin:channels"),
                button("👮 Adminlar", "admin:admins"),
            ],
            [
                button("📝 Start matni", "admin:start_text"),
                button("📣 Reklama", "admin:broadcast"),
            ],
        ]
    )


def channel_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("➕ Kanal qo‘shish", "admin:add_channel")],
            [button("➖ Kanalni o‘chirish", "admin:remove_channel")],
            [button("🔙 Orqaga qaytish", "admin:menu")],
        ]
    )


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [button("➕ Admin qo‘shish", "admin:add_admin")],
            [button("➖ Adminni o‘chirish", "admin:remove_admin")],
            [button("🔙 Orqaga qaytish", "admin:menu")],
        ]
    )


def subscription_keyboard(
    channels: list[aiosqlite.Row],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for channel in channels:
        label = str(channel["title"])[:55]
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"📢 {label}",
                    url=str(channel["invite_url"]),
                )
            ]
        )
    rows.append([button("🔄 Obunani tekshirish", "sub:check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_subscription_prompt(message: Message) -> None:
    channels = await fetch_all(
        "SELECT channel_id, title, invite_url FROM channels ORDER BY title COLLATE NOCASE"
    )
    if not channels:
        return
    await message.answer(
        "Kinolarni olish uchun quyidagi kanallarga obuna bo‘ling, "
        "so‘ng «Obunani tekshirish» tugmasini bosing.",
        reply_markup=subscription_keyboard(channels),
    )


async def check_subscription(bot: Bot, user_id: int) -> bool | None:
    channels = await fetch_all("SELECT channel_id FROM channels")
    for channel in channels:
        try:
            member = await bot.get_chat_member(
                chat_id=channel["channel_id"], user_id=user_id
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            logger.warning("Could not verify membership for a configured channel.")
            return None
        if member.status in {"left", "kicked"}:
            return False
        if member.status == "restricted" and not getattr(member, "is_member", False):
            return False
    return True


async def record_user(user_id: int) -> None:
    await write(
        "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
        (user_id,),
    )


async def get_start_text() -> str:
    row = await fetch_one("SELECT value FROM settings WHERE key = ?", ("start_text",))
    return str(row["value"]) if row else DEFAULT_START_TEXT


async def find_movies(query: str, limit: int = 20) -> list[aiosqlite.Row]:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return await fetch_all(
        """
        SELECT id, code, name, description, file_id
        FROM movies
        WHERE code = ? OR name LIKE ? ESCAPE '\\'
        ORDER BY name COLLATE NOCASE
        LIMIT ?
        """,
        (query, f"%{escaped}%", limit),
    )


def movie_results_keyboard(
    movies: list[aiosqlite.Row], action: str
) -> InlineKeyboardMarkup:
    rows = [
        [
            button(
                f"{movie['code']} · {str(movie['name'])[:35]}",
                f"movie:{action}:{movie['id']}",
            )
        ]
        for movie in movies
    ]
    rows.append([button("🔙 Orqaga qaytish", "admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("start"))
async def start_handler(message: Message) -> None:
    if message.from_user:
        await record_user(message.from_user.id)
    text = await get_start_text()
    await message.answer(html.escape(text), parse_mode="HTML")


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        await message.answer("Bu buyruq faqat adminlar uchun.")
        return
    await message.answer("Admin paneli:", reply_markup=main_menu_keyboard())


@router.message(Command("cancel"))
async def cancel_command(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("Amal bekor qilindi.", reply_markup=main_menu_keyboard())


@router.callback_query(F.data == "admin:cancel")
async def cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    await state.clear()
    await callback.answer("Amal bekor qilindi.")
    if callback.message:
        await callback.message.answer(
            "Admin paneli:", reply_markup=main_menu_keyboard()
        )


@router.callback_query(F.data == "admin:menu")
async def menu_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    await state.clear()
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Admin paneli:", reply_markup=main_menu_keyboard()
        )


@router.callback_query(F.data == "admin:stats")
async def stats_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    row = await fetch_one("SELECT COUNT(*) AS total FROM users")
    await callback.answer()
    if callback.message:
        await callback.message.answer(str(row["total"] if row else 0))


@router.callback_query(F.data == "admin:add_movie")
async def add_movie_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    await state.clear()
    await state.set_state(AdminFlow.add_movie_code)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Yangi kino uchun faqat raqamlardan iborat kod yuboring:",
            reply_markup=cancel_keyboard(),
        )


@router.message(AdminFlow.add_movie_code)
async def add_movie_code(message: Message, state: FSMContext) -> None:
    if not await require_admin(message=message, state=state):
        return
    code = (message.text or "").strip()
    if not re.fullmatch(r"\d{1,20}", code):
        await message.answer("Kod 1–20 ta raqamdan iborat bo‘lishi kerak.")
        return
    if await fetch_one("SELECT 1 FROM movies WHERE code = ?", (code,)):
        await message.answer("Bu kod allaqachon ishlatilgan. Boshqa kod yuboring.")
        return
    await state.update_data(code=code)
    await state.set_state(AdminFlow.add_movie_name)
    await message.answer("Kino nomini yuboring (ko‘pi bilan 120 belgi):")


@router.message(AdminFlow.add_movie_name)
async def add_movie_name(message: Message, state: FSMContext) -> None:
    if not await require_admin(message=message, state=state):
        return
    name = (message.text or "").strip()
    if not name or len(name) > 120:
        await message.answer("Nom bo‘sh bo‘lmasin va 120 belgidan oshmasin.")
        return
    await state.update_data(name=name)
    await state.set_state(AdminFlow.add_movie_description)
    await message.answer(
        "Kino tavsifini yuboring (3500 belgigacha). Tavsif kerak bo‘lmasa «-» yuboring:"
    )


@router.message(AdminFlow.add_movie_description)
async def add_movie_description(message: Message, state: FSMContext) -> None:
    if not await require_admin(message=message, state=state):
        return
    description = (message.text or "").strip()
    if len(description) > 3500:
        await message.answer("Tavsif 3500 belgidan oshmasin.")
        return
    await state.update_data(description="" if description == "-" else description)
    await state.set_state(AdminFlow.add_movie_video)
    await message.answer("Endi kinoning video faylini yuboring:")


@router.message(AdminFlow.add_movie_video)
async def add_movie_video(message: Message, state: FSMContext) -> None:
    if not await require_admin(message=message, state=state):
        return
    if not message.video:
        await message.answer("Video xabar yuboring. Boshqa fayl turi qabul qilinmaydi.")
        return
    data = await state.get_data()
    try:
        await write(
            "INSERT INTO movies (code, name, description, file_id) VALUES (?, ?, ?, ?)",
            (data["code"], data["name"], data["description"], message.video.file_id),
        )
    except aiosqlite.IntegrityError:
        await message.answer("Bu kod allaqachon ishlatilgan; boshqa kod bilan qayta boshlang.")
        await state.clear()
        return
    await state.clear()
    await message.answer(
        f"✅ Kino saqlandi. Kodi: <b>{html.escape(data['code'])}</b>",
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(F.data == "admin:delete_movie")
async def delete_movie_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    await state.clear()
    await state.set_state(AdminFlow.delete_movie_search)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "O‘chirish uchun kino kodini yoki nomidan bir qismini yuboring:",
            reply_markup=cancel_keyboard(),
        )


@router.message(AdminFlow.delete_movie_search)
async def delete_movie_search(message: Message, state: FSMContext) -> None:
    if not await require_admin(message=message, state=state):
        return
    query = (message.text or "").strip()
    if not query:
        await message.answer("Kino kodi yoki nomini yuboring.")
        return
    movies = await find_movies(query)
    if not movies:
        await message.answer("Mos kino topilmadi. Boshqa kod yoki nom kiriting.")
        return
    await state.clear()
    await message.answer(
        "O‘chiriladigan kinoni tanlang:",
        reply_markup=movie_results_keyboard(movies, "delete"),
    )


@router.callback_query(F.data.startswith("movie:delete:"))
async def delete_movie_select(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    movie_id = callback.data.rsplit(":", 1)[-1]
    movie = await fetch_one(
        "SELECT id, code, name FROM movies WHERE id = ?", (movie_id,)
    )
    await callback.answer()
    if not movie:
        if callback.message:
            await callback.message.answer("Bu kino topilmadi yoki oldin o‘chirilgan.")
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("✅ Ha, o‘chirish", f"movie:confirm_delete:{movie_id}"),
                button("❌ Bekor qilish", "admin:menu"),
            ]
        ]
    )
    if callback.message:
        await callback.message.answer(
            f"<b>{html.escape(movie['name'])}</b> "
            f"({html.escape(movie['code'])}) o‘chirilsinmi?",
            parse_mode="HTML",
            reply_markup=kb,
        )


@router.callback_query(F.data.startswith("movie:confirm_delete:"))
async def delete_movie_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    movie_id = callback.data.rsplit(":", 1)[-1]
    deleted = await write("DELETE FROM movies WHERE id = ?", (movie_id,))
    await callback.answer("Kino o‘chirildi." if deleted else "Kino topilmadi.")
    if callback.message:
        await callback.message.answer(
            "✅ Kino o‘chirildi." if deleted else "Kino topilmadi.",
            reply_markup=main_menu_keyboard(),
        )


@router.callback_query(F.data == "admin:edit_movie")
async def edit_movie_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    await state.clear()
    await state.set_state(AdminFlow.edit_movie_search)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Tahrirlash uchun kino kodini yoki nomidan bir qismini yuboring:",
            reply_markup=cancel_keyboard(),
        )


@router.message(AdminFlow.edit_movie_search)
async def edit_movie_search(message: Message, state: FSMContext) -> None:
    if not await require_admin(message=message, state=state):
        return
    query = (message.text or "").strip()
    if not query:
        await message.answer("Kino kodi yoki nomini yuboring.")
        return
    movies = await find_movies(query)
    if not movies:
        await message.answer("Mos kino topilmadi. Boshqa kod yoki nom kiriting.")
        return
    await state.clear()
    await message.answer(
        "Tahrirlanadigan kinoni tanlang:",
        reply_markup=movie_results_keyboard(movies, "edit"),
    )


@router.callback_query(F.data.startswith("movie:edit:"))
async def edit_movie_select(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    movie_id = callback.data.rsplit(":", 1)[-1]
    movie = await fetch_one(
        "SELECT id, code, name FROM movies WHERE id = ?", (movie_id,)
    )
    await callback.answer()
    if not movie:
        if callback.message:
            await callback.message.answer("Bu kino topilmadi yoki oldin o‘chirilgan.")
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [button("Kino kodi", f"movie:field:{movie_id}:code")],
            [button("Nomi", f"movie:field:{movie_id}:name")],
            [button("Tavsifi", f"movie:field:{movie_id}:description")],
            [button("Video fayli", f"movie:field:{movie_id}:video")],
            [button("❌ Bekor qilish", "admin:menu")],
        ]
    )
    if callback.message:
        await callback.message.answer(
            f"<b>{html.escape(movie['name'])}</b> ({html.escape(movie['code'])}) "
            "— o‘zgartiriladigan maydonni tanlang:",
            parse_mode="HTML",
            reply_markup=kb,
        )


@router.callback_query(F.data.startswith("movie:field:"))
async def edit_movie_field(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("Noto‘g‘ri so‘rov.", show_alert=True)
        return
    _, _, movie_id, field = parts
    if field not in {"code", "name", "description", "video"}:
        await callback.answer("Noto‘g‘ri maydon.", show_alert=True)
        return
    movie = await fetch_one("SELECT id FROM movies WHERE id = ?", (movie_id,))
    if not movie:
        await callback.answer("Kino topilmadi.", show_alert=True)
        return
    await state.clear()
    await state.update_data(movie_id=int(movie_id), edit_field=field)
    if field == "video":
        await state.set_state(AdminFlow.edit_movie_video)
        prompt = "Yangi video faylini yuboring:"
    else:
        await state.set_state(AdminFlow.edit_movie_value)
        prompts = {
            "code": "Yangi kino kodini yuboring (faqat raqamlar):",
            "name": "Yangi kino nomini yuboring (120 belgigacha):",
            "description": "Yangi tavsifni yuboring (3500 belgigacha); tavsifni bo‘shatish uchun «-» yuboring:",
        }
        prompt = prompts[field]
    await callback.answer()
    if callback.message:
        await callback.message.answer(prompt, reply_markup=cancel_keyboard())


@router.message(AdminFlow.edit_movie_value)
async def edit_movie_value(message: Message, state: FSMContext) -> None:
    if not await require_admin(message=message, state=state):
        return
    data = await state.get_data()
    field = data.get("edit_field")
    value = (message.text or "").strip()
    if field == "code" and not re.fullmatch(r"\d{1,20}", value):
        await message.answer("Kod 1–20 ta raqamdan iborat bo‘lishi kerak.")
        return
    if field == "name" and (not value or len(value) > 120):
        await message.answer("Nom bo‘sh bo‘lmasin va 120 belgidan oshmasin.")
        return
    if field == "description":
        if len(value) > 3500:
            await message.answer("Tavsif 3500 belgidan oshmasin.")
            return
        value = "" if value == "-" else value
    try:
        changed = await write(
            f"UPDATE movies SET {field} = ? WHERE id = ?",
            (value, data["movie_id"]),
        )
    except aiosqlite.IntegrityError:
        await message.answer("Bu kod boshqa kinoda ishlatilmoqda.")
        return
    await state.clear()
    await message.answer(
        "✅ Kino ma’lumoti yangilandi." if changed else "Kino topilmadi.",
        reply_markup=main_menu_keyboard(),
    )


@router.message(AdminFlow.edit_movie_video)
async def edit_movie_video(message: Message, state: FSMContext) -> None:
    if not await require_admin(message=message, state=state):
        return
    if not message.video:
        await message.answer("Yangi video xabar yuboring.")
        return
    data = await state.get_data()
    changed = await write(
        "UPDATE movies SET file_id = ? WHERE id = ?",
        (message.video.file_id, data["movie_id"]),
    )
    await state.clear()
    await message.answer(
        "✅ Video fayli yangilandi." if changed else "Kino topilmadi.",
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(F.data == "admin:search_movie")
async def search_movie_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    await state.clear()
    await state.set_state(AdminFlow.movie_search)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Qidirish uchun kino kodini yoki nomidan bir qismini yuboring:",
            reply_markup=cancel_keyboard(),
        )


@router.message(AdminFlow.movie_search)
async def search_movie_message(message: Message, state: FSMContext) -> None:
    if not await require_admin(message=message, state=state):
        return
    query = (message.text or "").strip()
    if not query:
        await message.answer("Kod yoki kino nomini yuboring.")
        return
    movies = await find_movies(query)
    if not movies:
        await message.answer("❌ Kino topilmadi. Boshqa kod yoki nom kiriting.")
        return
    lines = [
        f"<b>{html.escape(movie['code'])}</b> — {html.escape(movie['name'])}"
        for movie in movies
    ]
    await state.clear()
    await message.answer(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(F.data == "admin:channels")
async def channels_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    await state.clear()
    channels = await fetch_all(
        "SELECT channel_id, title FROM channels ORDER BY title COLLATE NOCASE"
    )
    if channels:
        channel_list = "\n".join(
            f"• {html.escape(channel['title'])} "
            f"(<code>{html.escape(channel['channel_id'])}</code>)"
            for channel in channels
        )
    else:
        channel_list = "Majburiy obuna kanallari hozircha qo‘shilmagan."
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            channel_list,
            parse_mode="HTML",
            reply_markup=channel_menu_keyboard(),
        )


@router.callback_query(F.data == "admin:add_channel")
async def add_channel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    await state.clear()
    await state.set_state(AdminFlow.add_channel)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Kanalning @username’i yoki raqamli ID sini yuboring. "
            "Bot kanal administratori bo‘lishi shart:",
            reply_markup=cancel_keyboard(),
        )


@router.message(AdminFlow.add_channel)
async def add_channel_message(message: Message, state: FSMContext, bot: Bot) -> None:
    if not await require_admin(message=message, state=state):
        return
    raw = (message.text or "").strip()
    if not raw or len(raw) > 100:
        await message.answer("Kanalning @username’i yoki raqamli ID sini yuboring.")
        return
    if raw.startswith("@"):
        channel_ref: str | int = raw
    elif re.fullmatch(r"-?\d{5,20}", raw):
        channel_ref = int(raw)
    else:
        await message.answer("Kanal @username yoki raqamli ID bo‘lishi kerak.")
        return
    try:
        chat = await bot.get_chat(channel_ref)
        bot_user = await bot.get_me()
        bot_member = await bot.get_chat_member(chat_id=chat.id, user_id=bot_user.id)
    except (TelegramBadRequest, TelegramForbiddenError):
        await message.answer(
            "Kanal topilmadi yoki bot unga kira olmadi. Botni kanalga qo‘shib, "
            "administrator huquqini bering."
        )
        return
    if bot_member.status not in {"administrator", "creator"}:
        await message.answer(
            "Bot bu kanalda administrator emas. Avval admin qilib, so‘ng qayta urinib ko‘ring."
        )
        return
    if chat.username:
        invite_url = f"https://t.me/{chat.username}"
    else:
        try:
            invite = await bot.create_chat_invite_link(chat_id=chat.id)
            invite_url = invite.invite_link
        except (TelegramBadRequest, TelegramForbiddenError):
            await message.answer(
                "Yopiq kanal uchun taklif havolasini yaratib bo‘lmadi. "
                "Botga taklif havolasi yaratish huquqini bering."
            )
            return
    try:
        await write(
            "INSERT INTO channels (channel_id, title, invite_url) VALUES (?, ?, ?)",
            (str(chat.id), chat.title or str(chat.id), invite_url),
        )
    except aiosqlite.IntegrityError:
        await message.answer("Bu kanal oldin qo‘shilgan.")
        return
    await state.clear()
    await message.answer(
        f"✅ Kanal qo‘shildi: <b>{html.escape(chat.title or str(chat.id))}</b>",
        parse_mode="HTML",
        reply_markup=channel_menu_keyboard(),
    )


@router.callback_query(F.data == "admin:remove_channel")
async def remove_channel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    await state.clear()
    channels = await fetch_all(
        "SELECT channel_id, title FROM channels ORDER BY title COLLATE NOCASE"
    )
    await callback.answer()
    if not callback.message:
        return
    if not channels:
        await callback.message.answer(
            "O‘chirish uchun kanal yo‘q.", reply_markup=channel_menu_keyboard()
        )
        return
    rows = [
        [button(f"➖ {str(channel['title'])[:45]}", f"channel:delete:{channel['channel_id']}")]
        for channel in channels
    ]
    rows.append([button("🔙 Orqaga qaytish", "admin:channels")])
    await callback.message.answer(
        "O‘chiriladigan kanalni tanlang:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("channel:delete:"))
async def remove_channel_select(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    channel_id = callback.data.removeprefix("channel:delete:")
    deleted = await write("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
    await callback.answer("Kanal o‘chirildi." if deleted else "Kanal topilmadi.")
    if callback.message:
        await callback.message.answer(
            "✅ Kanal o‘chirildi." if deleted else "Kanal topilmadi.",
            reply_markup=channel_menu_keyboard(),
        )


@router.callback_query(F.data == "admin:admins")
async def admins_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    await state.clear()
    primary_id = int(os.getenv("ADMIN_ID", "0") or "0")
    admins = await fetch_all("SELECT user_id FROM admins ORDER BY user_id")
    ids = {int(row["user_id"]) for row in admins}
    ids.add(primary_id)
    text = "Admin IDlari:\n" + "\n".join(f"• <code>{item}</code>" for item in sorted(ids))
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            text, parse_mode="HTML", reply_markup=admin_menu_keyboard()
        )


@router.callback_query(F.data == "admin:add_admin")
async def add_admin_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    await state.clear()
    await state.set_state(AdminFlow.add_admin)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Yangi adminning raqamli Telegram ID sini yuboring:",
            reply_markup=cancel_keyboard(),
        )


@router.message(AdminFlow.add_admin)
async def add_admin_message(message: Message, state: FSMContext) -> None:
    if not await require_admin(message=message, state=state):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or len(raw) > 20:
        await message.answer("Telegram ID faqat raqamlardan iborat bo‘lishi kerak.")
        return
    new_id = int(raw)
    try:
        await write(
            "INSERT INTO admins (user_id, added_by) VALUES (?, ?)",
            (new_id, message.from_user.id if message.from_user else 0),
        )
    except aiosqlite.IntegrityError:
        await message.answer("Bu foydalanuvchi allaqachon admin.")
        return
    await state.clear()
    await message.answer(
        f"✅ Admin qo‘shildi: <code>{new_id}</code>",
        parse_mode="HTML",
        reply_markup=admin_menu_keyboard(),
    )


@router.callback_query(F.data == "admin:remove_admin")
async def remove_admin_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    await state.clear()
    await state.set_state(AdminFlow.remove_admin)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "O‘chiriladigan adminning Telegram ID sini yuboring. "
            "Asosiy adminni o‘chirib bo‘lmaydi:",
            reply_markup=cancel_keyboard(),
        )


@router.message(AdminFlow.remove_admin)
async def remove_admin_message(message: Message, state: FSMContext) -> None:
    if not await require_admin(message=message, state=state):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or len(raw) > 20:
        await message.answer("Telegram ID faqat raqamlardan iborat bo‘lishi kerak.")
        return
    user_id = int(raw)
    primary_id = int(os.getenv("ADMIN_ID", "0") or "0")
    if user_id == primary_id:
        await message.answer("Asosiy adminni o‘chirib bo‘lmaydi.")
        return
    deleted = await write("DELETE FROM admins WHERE user_id = ?", (user_id,))
    await state.clear()
    await message.answer(
        "✅ Admin o‘chirildi." if deleted else "Bu ID adminlar ro‘yxatida yo‘q.",
        reply_markup=admin_menu_keyboard(),
    )


@router.callback_query(F.data == "admin:start_text")
async def start_text_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    await state.clear()
    await state.set_state(AdminFlow.start_text)
    current = await get_start_text()
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Yangi /start xabarini yuboring (3500 belgigacha).\n\n"
            f"Hozirgi xabar: {html.escape(current)}",
            parse_mode="HTML",
            reply_markup=cancel_keyboard(),
        )


@router.message(AdminFlow.start_text)
async def start_text_message(message: Message, state: FSMContext) -> None:
    if not await require_admin(message=message, state=state):
        return
    text = (message.text or "").strip()
    if not text or len(text) > 3500:
        await message.answer("Xabar bo‘sh bo‘lmasin va 3500 belgidan oshmasin.")
        return
    await write(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        ("start_text", text),
    )
    await state.clear()
    await message.answer(
        "✅ /start xabari saqlandi.", reply_markup=main_menu_keyboard()
    )


@router.callback_query(F.data == "admin:broadcast")
async def broadcast_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    await state.clear()
    await state.set_state(AdminFlow.broadcast_message)
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "Barcha foydalanuvchilarga yuboriladigan matn, rasm yoki videoni yuboring:",
            reply_markup=cancel_keyboard(),
        )


@router.message(AdminFlow.broadcast_message)
async def broadcast_message(message: Message, state: FSMContext) -> None:
    if not await require_admin(message=message, state=state):
        return
    if message.text and message.text.startswith("/"):
        await message.answer("Reklama sifatida matn, rasm yoki video yuboring.")
        return
    if not (message.text or message.photo or message.video):
        await message.answer("Faqat matn, rasm yoki video qabul qilinadi.")
        return
    await state.update_data(
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
    )
    await state.set_state(AdminFlow.broadcast_confirm)
    if message.text:
        await message.answer(
            f"Reklama matni:\n\n{html.escape(message.text)}",
            parse_mode="HTML",
        )
    else:
        await message.bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=message.chat.id,
            message_id=message.message_id,
        )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                button("✅ Yuborishni boshlash", "broadcast:confirm"),
                button("❌ Bekor qilish", "broadcast:cancel"),
            ]
        ]
    )
    await message.answer(
        "Reklama barcha foydalanuvchilarga yuborilsinmi?",
        reply_markup=kb,
    )


async def broadcast_to_users(
    bot: Bot,
    admin_id: int,
    source_chat_id: int,
    source_message_id: int,
    cancel_event: asyncio.Event,
) -> None:
    users = await fetch_all("SELECT user_id FROM users WHERE is_blocked = 0")
    sent = 0
    failed = 0
    stopped = False
    try:
        for user in users:
            if cancel_event.is_set():
                stopped = True
                break
            user_id = int(user["user_id"])
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=source_chat_id,
                    message_id=source_message_id,
                )
                sent += 1
            except TelegramRetryAfter as error:
                await asyncio.sleep(float(error.retry_after) + 0.25)
                if cancel_event.is_set():
                    stopped = True
                    break
                try:
                    await bot.copy_message(
                        chat_id=user_id,
                        from_chat_id=source_chat_id,
                        message_id=source_message_id,
                    )
                    sent += 1
                except (TelegramForbiddenError, TelegramBadRequest):
                    failed += 1
                    await write(
                        "UPDATE users SET is_blocked = 1 WHERE user_id = ?",
                        (user_id,),
                    )
                except Exception:
                    failed += 1
            except (TelegramForbiddenError, TelegramBadRequest):
                failed += 1
                await write(
                    "UPDATE users SET is_blocked = 1 WHERE user_id = ?",
                    (user_id,),
                )
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
    finally:
        active_broadcasts.pop(admin_id, None)
    suffix = "\nReklama yuborish bekor qilindi." if stopped or cancel_event.is_set() else ""
    try:
        await bot.send_message(
            admin_id,
            f"Yuborildi: {sent}\nYuborilmadi: {failed}{suffix}",
        )
    except Exception:
        logger.warning("Could not send a broadcast summary to its admin.")


@router.callback_query(F.data == "broadcast:confirm")
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    if await state.get_state() != AdminFlow.broadcast_confirm.state:
        await callback.answer("Reklama ma’lumoti topilmadi.", show_alert=True)
        return
    if callback.from_user.id in active_broadcasts:
        await state.clear()
        await callback.answer("Sizda allaqachon reklama yuborilmoqda.", show_alert=True)
        return
    data = await state.get_data()
    await state.clear()
    admin_id = callback.from_user.id
    cancel_event = asyncio.Event()
    active_broadcasts[admin_id] = cancel_event
    await callback.answer("Yuborish boshlandi.")
    if callback.message:
        await callback.message.answer(
            "Reklama yuborilmoqda. Jarayonni to‘xtatish uchun tugmani bosing.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[button("⏹ Yuborishni to‘xtatish", "broadcast:stop")]]
            ),
        )
    asyncio.create_task(
        broadcast_to_users(
            callback.bot,
            admin_id,
            int(data["source_chat_id"]),
            int(data["source_message_id"]),
            cancel_event,
        )
    )


@router.callback_query(F.data == "broadcast:cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    await state.clear()
    await callback.answer("Reklama bekor qilindi.")
    if callback.message:
        await callback.message.answer(
            "Reklama yuborilmadi.", reply_markup=main_menu_keyboard()
        )


@router.callback_query(F.data == "broadcast:stop")
async def broadcast_stop(callback: CallbackQuery, state: FSMContext) -> None:
    if not await require_admin(callback=callback, state=state):
        return
    event = active_broadcasts.get(callback.from_user.id)
    if not event:
        await callback.answer("Hozir reklama yuborilmayapti.", show_alert=True)
        return
    event.set()
    await callback.answer("Yuborish to‘xtatilmoqda.")


@router.callback_query(F.data == "sub:check")
async def subscription_check_callback(callback: CallbackQuery) -> None:
    status = await check_subscription(callback.bot, callback.from_user.id)
    await callback.answer()
    if not callback.message:
        return
    if status is True:
        await callback.message.answer(
            "✅ Obuna tasdiqlandi. Endi kino kodini yuboring."
        )
    elif status is False:
        await callback.message.answer(
            "❌ Hali barcha kanallarga obuna bo‘lmagansiz.",
            reply_markup=subscription_keyboard(
                await fetch_all(
                    "SELECT channel_id, title, invite_url FROM channels "
                    "ORDER BY title COLLATE NOCASE"
                )
            ),
        )
    else:
        await callback.message.answer(
            "Obunani hozir tekshirib bo‘lmadi. Birozdan so‘ng qayta urinib ko‘ring."
        )


@router.message(F.text)
async def movie_code_handler(message: Message) -> None:
    if not message.from_user:
        return
    code = (message.text or "").strip()
    if not code:
        return
    if code.startswith("/"):
        await message.answer("Noma’lum buyruq. Kino kodini yuboring yoki /start bosing.")
        return
    await record_user(message.from_user.id)
    subscribed = await check_subscription(message.bot, message.from_user.id)
    if subscribed is None:
        await message.answer(
            "Obunani hozir tekshirib bo‘lmadi. Birozdan so‘ng qayta urinib ko‘ring."
        )
        return
    if not subscribed:
        await send_subscription_prompt(message)
        return
    if not re.fullmatch(r"\d{1,20}", code):
        await message.answer("Kino kodini raqamlar bilan yuboring.")
        return
    movie = await fetch_one(
        "SELECT code, name, description, file_id FROM movies WHERE code = ?",
        (code,),
    )
    if not movie:
        await message.answer("❌ Bunday kino topilmadi")
        return
    try:
        await message.answer_video(
            video=movie["file_id"],
            caption=f"<b>{html.escape(movie['name'])}</b>",
            parse_mode="HTML",
        )
        if movie["description"]:
            await message.answer(
                html.escape(movie["description"]),
                parse_mode="HTML",
            )
    except (TelegramBadRequest, TelegramForbiddenError):
        logger.warning("A saved video could not be delivered.")
        await message.answer("Kino videosini yuborishda xatolik bo‘ldi.")


@router.message()
async def unsupported_message_handler(message: Message) -> None:
    if not message.from_user or message.from_user.is_bot:
        return
    await message.answer("Kino kodini raqamlar bilan yuboring.")


@router.callback_query()
async def unsupported_callback_handler(callback: CallbackQuery) -> None:
    await callback.answer(
        "Bu tugma yaroqsiz yoki eskirgan. Yangi menyuni oching.",
        show_alert=True,
    )


@router.errors()
async def handle_unexpected_error(
    event_update: Any, exception: Exception
) -> bool:
    logger.error(
        "An unexpected error occurred while processing a bot update (%s).",
        type(exception).__name__,
    )
    return True


async def run_bot() -> None:
    token = (os.getenv("BOT_TOKEN") or "").strip()
    raw_admin_id = (os.getenv("ADMIN_ID") or "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN sozlamasi kiritilmagan.")
    if not raw_admin_id.isdigit() or int(raw_admin_id) <= 0:
        raise SystemExit("ADMIN_ID sozlamasiga asosiy adminning raqamli ID sini kiriting.")

    await initialize_database()
    primary_id = int(raw_admin_id)
    await write(
        "INSERT OR IGNORE INTO admins (user_id, added_by) VALUES (?, ?)",
        (primary_id, primary_id),
    )

    bot = Bot(token=token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(router)
    logger.info("Tamosho kino bot ishga tushmoqda.")
    try:
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=True,
        )
    except Exception as error:
        logger.error("Bot polling failed (%s).", type(error).__name__)
        raise SystemExit(1) from None


async def run_check() -> None:
    await initialize_database()
    print("Python modullari va SQLite bazasi tayyor.")


def main() -> None:
    configure_logging()
    parser = argparse.ArgumentParser(description="Tamosho Telegram kino bot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Telegram tokenisiz SQLite va Python muhitini tekshirish",
    )
    arguments = parser.parse_args()
    if arguments.check:
        asyncio.run(run_check())
    else:
        asyncio.run(run_bot())


if __name__ == "__main__":
    main()
