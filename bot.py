import asyncio
import logging
import re
import sqlite3
from html import escape as he
from os import getenv
from pathlib import Path
from datetime import datetime, date, timedelta

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import gspread
from google.oauth2.service_account import Credentials as GCredentials
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, FSInputFile

load_dotenv()

TOKEN = getenv("BOT_TOKEN")
ADMIN_ID = 6430611356

BASE_DIR = Path(__file__).resolve().parent
IMAGES_DIR = BASE_DIR / "images"
DB_PATH          = Path(getenv("DB_PATH", str(BASE_DIR / "bookings.db")))
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
GOOGLE_SHEET_ID  = "1Q2IFOAZ1-wQoHzKghSR-p1VDRDxVxCcS9enysdE0Emo"
_SHEET_SCOPES    = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
_SHEET_HEADERS   = ["ID", "Создана", "Дата визита", "Время", "Услуга",
                    "Мастер", "Имя клиента", "Телефон", "Telegram", "Статус"]

BANNER_IMAGE   = IMAGES_DIR / "banner.jpg"
ABOUT_IMAGE    = IMAGES_DIR / "about.jpg"
PRICES_IMAGE   = IMAGES_DIR / "prices.jpg"
MASTERS_IMAGE  = IMAGES_DIR / "masters.jpg"
CONTACTS_IMAGE = IMAGES_DIR / "contacts.jpg"

# Соответствие кнопок → (ключ в БД, дефолтный файл)
INFO_SECTION_NAMES = {
    "О салоне":         ("about",    ABOUT_IMAGE),
    "Услуги и цены":    ("prices",   PRICES_IMAGE),
    "Мастера":          ("masters",  MASTERS_IMAGE),
    "Контакты и адрес": ("contacts", CONTACTS_IMAGE),
}

storage = MemoryStorage()
dp = Dispatcher(storage=storage)

WORKING_TIMES = [
    "10:00","11:00","12:00","13:00","14:00",
    "15:00","16:00","17:00","18:00","19:00",
]
DEFAULT_OPEN  = "10:00"
DEFAULT_CLOSE = "20:00"   # последний слот 19:00, закрытие в 20:00

SERVICES = [
    "Маникюр без покрытия",
    "Маникюр + Гель-лак",
    "Маникюр + Наращивание",
    "Маникюр + Коррекция наращивания",
    "Педикюр без покрытия",
    "Педикюр + Гель-лак",
]

TWO_HOUR_SERVICES = {"Маникюр + Наращивание", "Маникюр + Коррекция наращивания"}

BANNED_WORDS = [
    "хуй","хуе","хуё","пизд","еба","ёба","еби","ебл",
    "сука","сучк","бля","бляд","блять","гандон","мудак",
    "мраз","долба","дебил","идиот","урод",
]


# ─── FSM ───────────────────────────────────────────────────────────────────────

class BookingState(StatesGroup):
    choosing_service = State()
    entering_date    = State()
    choosing_master  = State()
    choosing_time    = State()
    entering_name    = State()
    entering_phone   = State()
    confirming       = State()

class CancelBookingState(StatesGroup):
    confirming_single  = State()
    choosing_booking   = State()

class ReviewState(StatesGroup):
    rating_pending  = State()
    comment_pending = State()

class AdminState(StatesGroup):
    admin_home = State()
    # Мастера
    master_menu          = State()
    adding_name          = State()
    adding_services      = State()
    adding_schedule_type = State()
    adding_custom_sched  = State()
    adding_sched_start   = State()
    viewing_ratings_select = State()
    deactivating_select    = State()
    deactivating_confirm   = State()
    editing_select       = State()
    editing_menu         = State()
    editing_sched_menu   = State()
    editing_sched_type   = State()
    editing_custom_sched = State()
    editing_sched_start  = State()
    editing_vacation     = State()
    editing_day_off      = State()
    editing_svcs_menu    = State()
    adding_service       = State()
    removing_svc_select  = State()
    removing_svc_confirm = State()
    # Расписание салона
    salon_menu            = State()
    salon_day_off         = State()
    salon_day_off_confirm = State()
    salon_hours_date      = State()
    salon_hours_val       = State()
    salon_hours_confirm   = State()
    salon_cancel_select   = State()
    salon_cancel_confirm  = State()
    # Расписание записей
    schedule_view = State()

    # Управление услугами
    svc_menu            = State()
    svc_adding_name     = State()
    svc_adding_price    = State()
    svc_adding_duration = State()
    svc_adding_confirm  = State()
    svc_remove_select   = State()
    svc_remove_confirm  = State()
    svc_edit_select     = State()
    svc_edit_action     = State()
    svc_edit_name       = State()
    svc_edit_price      = State()
    svc_edit_confirm    = State()

    # Редактирование инфо-страниц
    info_select  = State()
    info_text    = State()
    info_photo   = State()
    info_confirm = State()

    # Рассылка объявлений
    broadcast_text    = State()
    broadcast_media   = State()
    broadcast_confirm = State()

    # Поиск по клиенту
    client_search_input = State()

    # Блокировка клиентов
    block_menu      = State()
    block_input     = State()
    block_confirm   = State()
    blocked_list    = State()
    unblock_confirm = State()

    # Управление менеджерами (только для администратора)
    managers_menu              = State()
    manager_add_input          = State()
    manager_deactivate_select  = State()
    manager_deactivate_confirm = State()
    manager_log_view           = State()


# ─── Keyboards ─────────────────────────────────────────────────────────────────

main_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="О салоне"),      KeyboardButton(text="Услуги и цены")],
    [KeyboardButton(text="Мастера"),       KeyboardButton(text="Записаться")],
    [KeyboardButton(text="Мои записи"),    KeyboardButton(text="Отменить запись")],
    [KeyboardButton(text="Контакты и адрес")],
], resize_keyboard=True)

def build_client_services_keyboard() -> ReplyKeyboardMarkup:
    svcs = get_active_services()
    rows = [[KeyboardButton(text=s["name"])] for s in svcs]
    rows.append([KeyboardButton(text="Отмена")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

confirm_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Подтвердить запись")],
    [KeyboardButton(text="Отменить запись")],
], resize_keyboard=True)

reminder_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Подтвердить визит")],
    [KeyboardButton(text="Отменить визит")],
    [KeyboardButton(text="Перезаписаться")],
], resize_keyboard=True)

confirm_cancel_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Да, отменить")],
    [KeyboardButton(text="Нет, назад")],
], resize_keyboard=True)

yes_no_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Да"), KeyboardButton(text="Нет")],
], resize_keyboard=True)

rating_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="5"), KeyboardButton(text="4"), KeyboardButton(text="3"),
     KeyboardButton(text="2"), KeyboardButton(text="1")],
], resize_keyboard=True)

review_comment_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Отправить оценку без комментария")],
    [KeyboardButton(text="Назад")],
], resize_keyboard=True)

admin_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="💁‍♀️Мастера"),           KeyboardButton(text="🗓️Расписание записей")],
    [KeyboardButton(text="💅Услуги"),              KeyboardButton(text="📝Расписание салона")],
    [KeyboardButton(text="📄О салоне"),            KeyboardButton(text="✏️Создать объявление")],
    [KeyboardButton(text="🔍Поиск по клиенту"),   KeyboardButton(text="⛔Блок пользователя")],
    [KeyboardButton(text="Выход из редактирования")],
], resize_keyboard=True)

admin_super_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="💁‍♀️Мастера"),           KeyboardButton(text="🗓️Расписание записей")],
    [KeyboardButton(text="💅Услуги"),              KeyboardButton(text="📝Расписание салона")],
    [KeyboardButton(text="📄О салоне"),            KeyboardButton(text="✏️Создать объявление")],
    [KeyboardButton(text="🔍Поиск по клиенту"),   KeyboardButton(text="⛔Блок пользователя")],
    [KeyboardButton(text="Менеджеры")],
    [KeyboardButton(text="Уведомления")],
    [KeyboardButton(text="Выход из редактирования")],
], resize_keyboard=True)

managers_menu_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Добавить менеджера")],
    [KeyboardButton(text="Список менеджеров")],
    [KeyboardButton(text="Лог действий")],
    [KeyboardButton(text="Назад")],
], resize_keyboard=True)

block_menu_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Заблокировать клиента")],
    [KeyboardButton(text="Заблокированные клиенты")],
    [KeyboardButton(text="Назад")],
], resize_keyboard=True)

svc_menu_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Добавить услугу")],
    [KeyboardButton(text="Убрать услугу"),   KeyboardButton(text="Править услугу")],
    [KeyboardButton(text="Назад")],
], resize_keyboard=True)

svc_edit_action_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Изменить название"), KeyboardButton(text="Изменить цену")],
    [KeyboardButton(text="Назад")],
], resize_keyboard=True)

svc_duration_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="1 час"), KeyboardButton(text="2 часа")],
], resize_keyboard=True)

info_sections_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="О салоне"),         KeyboardButton(text="Услуги и цены")],
    [KeyboardButton(text="Мастера"),          KeyboardButton(text="Контакты и адрес")],
    [KeyboardButton(text="Назад")],
], resize_keyboard=True)

keep_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Не менять")],
], resize_keyboard=True)

admin_masters_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="🟢Внести мастера"),
     KeyboardButton(text="🟡Редактировать"),
     KeyboardButton(text="🔴Удалить")],
    [KeyboardButton(text="👯Список мастеров"),
     KeyboardButton(text="⭐Смотреть оценку")],
    [KeyboardButton(text="Назад")],
], resize_keyboard=True)

editing_master_menu_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Редактировать расписание")],
    [KeyboardButton(text="Редактировать услуги")],
    [KeyboardButton(text="Назад")],
], resize_keyboard=True)

editing_schedule_menu_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Другой режим работы")],
    [KeyboardButton(text="Отпуск"), KeyboardButton(text="Выходной")],
    [KeyboardButton(text="Назад")],
], resize_keyboard=True)

editing_services_menu_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Добавить услугу"), KeyboardButton(text="Убрать услугу")],
    [KeyboardButton(text="Назад")],
], resize_keyboard=True)

schedule_type_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="2/2"), KeyboardButton(text="5/2")],
    [KeyboardButton(text="Каждый день")],
    [KeyboardButton(text="Другой (X/Y)")],
    [KeyboardButton(text="Назад")],
], resize_keyboard=True)

admin_salon_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Выходной день"),       KeyboardButton(text="Изменить часы работы")],
    [KeyboardButton(text="Отменить изменение расписания")],
    [KeyboardButton(text="Назад")],
], resize_keyboard=True)

schedule_period_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Сегодня"),  KeyboardButton(text="3 дня")],
    [KeyboardButton(text="Неделя"),   KeyboardButton(text="2 недели")],
    [KeyboardButton(text="Назад")],
], resize_keyboard=True)

back_keyboard = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Назад")]], resize_keyboard=True)

broadcast_media_keyboard = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="Без медиафайла")],
    [KeyboardButton(text="Назад")],
], resize_keyboard=True)

reengagement_promo_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="Записаться")]],
    resize_keyboard=True,
)


# ─── Google Sheets ─────────────────────────────────────────────────────────────

def _get_worksheet() -> gspread.Worksheet:
    creds  = GCredentials.from_service_account_file(str(CREDENTIALS_FILE), scopes=_SHEET_SCOPES)
    client = gspread.authorize(creds)
    ws     = client.open_by_key(GOOGLE_SHEET_ID).sheet1
    if not ws.row_values(1):
        ws.append_row(_SHEET_HEADERS)
    return ws

def _sync_add_row(booking_id: int, created_at: str, visit_date: str, visit_time: str,
                  service: str, master: str, name: str, phone: str, username: str | None):
    ws = _get_worksheet()
    tg = f"@{username}" if username else "—"
    ws.append_row([booking_id, created_at, visit_date, visit_time,
                   service, master, name, phone, tg, "активна"])

def _sync_set_status(booking_id: int, status: str):
    ws   = _get_worksheet()
    cell = ws.find(str(booking_id), in_column=1)
    if cell:
        ws.update_cell(cell.row, 10, status)

async def sheets_add_booking(booking_id: int, created_at: str, visit_date: str,
                              visit_time: str, service: str, master: str,
                              name: str, phone: str, username: str | None):
    if not CREDENTIALS_FILE.exists():
        return
    try:
        await asyncio.to_thread(
            _sync_add_row, booking_id, created_at, visit_date, visit_time,
            service, master, name, phone, username,
        )
    except Exception:
        logging.exception("Ошибка записи в Google Sheets")

async def sheets_set_status(booking_id: int, status: str):
    if not CREDENTIALS_FILE.exists():
        return
    try:
        await asyncio.to_thread(_sync_set_status, booking_id, status)
    except Exception:
        logging.exception("Ошибка обновления в Google Sheets")


# ─── DB init ───────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()

        cols = [r[1] for r in c.execute("PRAGMA table_info(bookings)").fetchall()]
        if cols and "master_id" not in cols:
            c.execute("DROP TABLE IF EXISTS bookings_old")
            c.execute("ALTER TABLE bookings RENAME TO bookings_old")
            cols = []

        c.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                master_id INTEGER NOT NULL,
                service TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                duration INTEGER NOT NULL DEFAULT 1,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reminder_sent INTEGER NOT NULL DEFAULT 0,
                client_confirmed INTEGER NOT NULL DEFAULT 0,
                review_sent INTEGER NOT NULL DEFAULT 0,
                UNIQUE(master_id, date, time)
            )
        """)
        # migrations
        if cols and "duration" not in cols:
            c.execute("ALTER TABLE bookings ADD COLUMN duration INTEGER NOT NULL DEFAULT 1")
        if cols and "review_sent" not in cols:
            c.execute("ALTER TABLE bookings ADD COLUMN review_sent INTEGER NOT NULL DEFAULT 0")

        c.execute("""CREATE TABLE IF NOT EXISTS masters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS master_services (
            master_id INTEGER NOT NULL, service TEXT NOT NULL,
            PRIMARY KEY (master_id, service))""")
        c.execute("""CREATE TABLE IF NOT EXISTS master_schedules (
            master_id INTEGER PRIMARY KEY,
            schedule_type TEXT NOT NULL DEFAULT 'all',
            work_days INTEGER, off_days INTEGER, start_date TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS master_day_overrides (
            master_id INTEGER NOT NULL, date TEXT NOT NULL,
            is_working INTEGER NOT NULL, PRIMARY KEY (master_id, date))""")
        c.execute("""CREATE TABLE IF NOT EXISTS salon_day_overrides (
            date TEXT PRIMARY KEY,
            is_working INTEGER NOT NULL DEFAULT 1,
            open_time TEXT, close_time TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            master_id INTEGER,
            client_name TEXT NOT NULL,
            service TEXT NOT NULL,
            rating INTEGER NOT NULL,
            comment TEXT,
            created_at TEXT NOT NULL)""")

        c.execute("""CREATE TABLE IF NOT EXISTS services (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            price INTEGER NOT NULL DEFAULT 0,
            duration INTEGER NOT NULL DEFAULT 1,
            is_active INTEGER NOT NULL DEFAULT 1)""")

        c.execute("""CREATE TABLE IF NOT EXISTS info_pages (
            section TEXT PRIMARY KEY,
            text TEXT,
            photo_file_id TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS reengagement_log (
            user_id INTEGER PRIMARY KEY,
            sent_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS blocked_clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            phone TEXT,
            name TEXT,
            blocked_at TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS cancelled_bookings (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            username TEXT,
            master_id INTEGER NOT NULL,
            master_name TEXT NOT NULL,
            service TEXT NOT NULL,
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            duration INTEGER NOT NULL DEFAULT 1,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            created_at TEXT NOT NULL,
            cancelled_at TEXT NOT NULL,
            cancel_reason TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS managers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER UNIQUE NOT NULL,
            name TEXT NOT NULL,
            added_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1)""")
        c.execute("""CREATE TABLE IF NOT EXISTS admin_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS manager_action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            manager_id INTEGER NOT NULL,
            manager_name TEXT NOT NULL,
            action TEXT NOT NULL,
            details TEXT,
            logged_at TEXT NOT NULL)""")

        # Seed services if empty
        if not c.execute("SELECT COUNT(*) FROM services").fetchone()[0]:
            for name, price, dur in [
                ("Маникюр без покрытия", 1200, 1),
                ("Маникюр + Гель-лак", 2200, 1),
                ("Маникюр + Наращивание", 3500, 2),
                ("Маникюр + Коррекция наращивания", 3000, 2),
                ("Педикюр без покрытия", 1800, 1),
                ("Педикюр + Гель-лак", 2800, 1),
            ]:
                c.execute("INSERT INTO services (name,price,duration) VALUES (?,?,?)", (name, price, dur))

        # Seed info pages if empty
        if not c.execute("SELECT COUNT(*) FROM info_pages").fetchone()[0]:
            for section, text in [
                ("about", "Nail Studio — салон маникюра и педикюра.\n\nРаботаем ежедневно 10:00–20:00.\nИспользуем стерильные инструменты, одноразовые расходники и качественные материалы."),
                ("prices", None),
                ("masters", None),
                ("contacts", "Контакты Nail Studio:\n\nАдрес: Москва, ул. Примерная, 10\nТелефон: +7 999 123-45-67\nTelegram: @nailstudio_manager\nВремя работы: 10:00–20:00"),
            ]:
                c.execute("INSERT INTO info_pages (section,text,photo_file_id) VALUES (?,?,NULL)", (section, text))

        conn.commit()


# ─── Utility ───────────────────────────────────────────────────────────────────

def get_service_duration(service: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT duration FROM services WHERE name=?", (service,)).fetchone()
    return row[0] if row else 1

def contains_banned_words(text: str) -> bool:
    l = text.lower()
    return any(w in l for w in BANNED_WORDS)

def is_valid_phone(phone: str) -> bool:
    c = re.sub(r"[\s\-\(\)]", "", phone)
    return bool(re.fullmatch(r"8\d{10}", c) or re.fullmatch(r"\+7\d{10}", c))

def parse_date_flexible(text: str) -> date | None:
    for fmt in ("%d.%m.%Y", "%d.%m.%y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    return None

def parse_booking_date(t: str) -> date | None:
    return parse_date_flexible(t)

def parse_date_range(text: str) -> tuple[date, date] | None:
    parts = re.split(r"\s*[-–]\s*", text.strip())
    if len(parts) != 2:
        return None
    s, e = parse_date_flexible(parts[0]), parse_date_flexible(parts[1])
    return (s, e) if s and e and s <= e else None

def is_date_available(d: date) -> tuple[bool, str]:
    today = date.today()
    if d < today:
        return False, "Нельзя записаться на прошедшую дату."
    if d > today + timedelta(days=14):
        return False, "Можно записаться только в пределах ближайших 14 дней."
    return True, ""

def parse_time(t: str) -> int:
    h, m = t.split(":")
    return int(h)*60 + int(m)

def normalize_phone(phone: str) -> str:
    """Приводит номер к виду +7XXXXXXXXXX."""
    c = re.sub(r"[\s\-\(\)]", "", phone)
    if c.startswith("8") and len(c) == 11:
        return "+7" + c[1:]
    return c

def phone_variants(phone: str) -> tuple[str, str]:
    """Возвращает оба варианта: 8XXXXXXXXXX и +7XXXXXXXXXX."""
    norm = normalize_phone(phone)
    if norm.startswith("+7") and len(norm) == 12:
        return (norm, "8" + norm[2:])
    if norm.startswith("8") and len(norm) == 11:
        return (norm, "+7" + norm[1:])
    return (norm, norm)

def get_client_visit_count(phone: str, username: str | None) -> int:
    """Количество активных записей клиента (по телефону или TG-нику)."""
    p7, p8 = phone_variants(phone)
    with sqlite3.connect(DB_PATH) as conn:
        if username:
            row = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE phone IN (?,?) OR username=?",
                (p7, p8, username),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE phone IN (?,?)",
                (p7, p8),
            ).fetchone()
    return row[0] if row else 0

def loyalty_badge(count: int) -> str:
    if count >= 5:
        return " ❤️"
    if count >= 2:
        return " ⭐"
    return ""

def _parse_xy(text: str) -> tuple[int,int] | None:
    p = text.strip().split("/")
    if len(p) == 2:
        try:
            w, o = int(p[0]), int(p[1])
            if w > 0 and o > 0:
                return w, o
        except ValueError:
            pass
    return None


# ─── Salon schedule DB ─────────────────────────────────────────────────────────

def get_salon_hours(date_text: str) -> tuple[str,str] | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT is_working, open_time, close_time FROM salon_day_overrides WHERE date=?",
            (date_text,)).fetchone()
    if row:
        iw, ot, ct = row
        return None if not iw else (ot or DEFAULT_OPEN, ct or DEFAULT_CLOSE)
    return (DEFAULT_OPEN, DEFAULT_CLOSE)

def set_salon_day_off(date_text: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO salon_day_overrides (date,is_working) VALUES(?,0)", (date_text,))
        conn.commit()

def set_salon_hours(date_text: str, ot: str, ct: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO salon_day_overrides (date,is_working,open_time,close_time) VALUES(?,1,?,?)",
                     (date_text, ot, ct))
        conn.commit()

def get_salon_overrides() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT date, is_working, open_time, close_time FROM salon_day_overrides ORDER BY date"
        ).fetchall()
    result = []
    for dt, iw, ot, ct in rows:
        if iw:
            desc = f"{dt} — часы: {ot or DEFAULT_OPEN}–{ct or DEFAULT_CLOSE}"
        else:
            desc = f"{dt} — выходной"
        result.append({"date": dt, "desc": desc})
    return result

def remove_salon_override(date_text: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM salon_day_overrides WHERE date=?", (date_text,))
        conn.commit()


# ─── Services DB ──────────────────────────────────────────────────────────────

def get_active_services() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id,name,price,duration FROM services WHERE is_active=1 ORDER BY id"
        ).fetchall()
    return [{"id": r[0], "name": r[1], "price": r[2], "duration": r[3]} for r in rows]

def get_all_services() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT id,name,price,duration,is_active FROM services ORDER BY id").fetchall()
    return [{"id": r[0], "name": r[1], "price": r[2], "duration": r[3], "is_active": r[4]} for r in rows]

def add_service_to_catalog(name: str, price: int, duration: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO services (name,price,duration) VALUES (?,?,?)", (name, price, duration))
        conn.commit()

def deactivate_service(service_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE services SET is_active=0 WHERE id=?", (service_id,))
        conn.commit()

def edit_service_name(service_id: int, new_name: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE services SET name=? WHERE id=?", (new_name, service_id))
        conn.commit()

def edit_service_price(service_id: int, new_price: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE services SET price=? WHERE id=?", (new_price, service_id))
        conn.commit()


# ─── Info Pages DB ────────────────────────────────────────────────────────────

def get_info_page(section: str) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT text, photo_file_id FROM info_pages WHERE section=?", (section,)
        ).fetchone()
    return {"text": row[0], "photo_file_id": row[1]} if row else {"text": None, "photo_file_id": None}

def save_info_page(section: str, text: str | None, photo_file_id: str | None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO info_pages (section,text,photo_file_id) VALUES (?,?,?)",
            (section, text, photo_file_id),
        )
        conn.commit()


# ─── Master DB ────────────────────────────────────────────────────────────────

def get_all_masters() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT id,name,is_active FROM masters ORDER BY name").fetchall()
    return [{"id":r[0],"name":r[1],"is_active":r[2]} for r in rows]

def get_active_masters() -> list[dict]:
    return [m for m in get_all_masters() if m["is_active"]]

def get_master_by_id(mid: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT id,name,is_active FROM masters WHERE id=?", (mid,)).fetchone()
        if not row:
            return None
        svcs = [r[0] for r in conn.execute("SELECT service FROM master_services WHERE master_id=?", (mid,)).fetchall()]
        sched = conn.execute("SELECT schedule_type,work_days,off_days,start_date FROM master_schedules WHERE master_id=?", (mid,)).fetchone()
    return {"id":row[0],"name":row[1],"is_active":row[2],"services":svcs,"schedule":sched}

def save_review(master_id: int, client_name: str, service: str, rating: int, comment: str | None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO reviews (master_id,client_name,service,rating,comment,created_at) VALUES(?,?,?,?,?,?)",
            (master_id, client_name, service, rating, comment,
             datetime.now().strftime("%d.%m.%Y %H:%M")),
        )
        conn.commit()

def get_master_reviews(master_id: int) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT rating,comment,client_name,service,created_at FROM reviews WHERE master_id=? ORDER BY created_at DESC",
            (master_id,),
        ).fetchall()
    return [{"rating":r[0],"comment":r[1],"client_name":r[2],"service":r[3],"created_at":r[4]} for r in rows]

def get_master_avg_rating(master_id: int) -> float | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT AVG(rating) FROM reviews WHERE master_id=?", (master_id,)).fetchone()
    if row and row[0] is not None:
        return round(row[0], 1)
    return None

def add_master(name: str) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT INTO masters (name) VALUES(?)", (name,))
        conn.commit()
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]

def deactivate_master(mid: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE masters SET is_active=0 WHERE id=?", (mid,))
        conn.commit()

def set_master_services(mid: int, svcs: list[str]):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM master_services WHERE master_id=?", (mid,))
        for s in svcs:
            conn.execute("INSERT INTO master_services (master_id,service) VALUES(?,?)", (mid,s))
        conn.commit()

def add_service_to_master(mid: int, svc: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO master_services (master_id,service) VALUES(?,?)", (mid,svc))
        conn.commit()

def remove_service_from_master(mid: int, svc: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM master_services WHERE master_id=? AND service=?", (mid,svc))
        conn.commit()

def set_master_schedule(mid: int, stype: str, work=None, off=None, start=None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO master_schedules (master_id,schedule_type,work_days,off_days,start_date) VALUES(?,?,?,?,?)",
                     (mid,stype,work,off,start))
        conn.commit()

def add_master_day_off(mid: int, date_text: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR REPLACE INTO master_day_overrides (master_id,date,is_working) VALUES(?,?,0)", (mid,date_text))
        conn.commit()

def add_master_vacation(mid: int, start: date, end: date):
    with sqlite3.connect(DB_PATH) as conn:
        cur = start
        while cur <= end:
            conn.execute("INSERT OR REPLACE INTO master_day_overrides (master_id,date,is_working) VALUES(?,?,0)",
                         (mid, cur.strftime("%d.%m.%Y")))
            cur += timedelta(days=1)
        conn.commit()


# ─── Schedule logic ────────────────────────────────────────────────────────────

def is_master_working(mid: int, date_text: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT is_working FROM master_day_overrides WHERE master_id=? AND date=?", (mid,date_text)).fetchone()
        if row is not None:
            return bool(row[0])
        sched = conn.execute("SELECT schedule_type,work_days,off_days,start_date FROM master_schedules WHERE master_id=?", (mid,)).fetchone()
    if sched is None:
        return True
    stype, w, o, sd = sched
    if stype == "all":
        return True
    target = datetime.strptime(date_text, "%d.%m.%Y").date()
    start  = datetime.strptime(sd, "%d.%m.%Y").date()
    diff = (target - start).days
    return diff >= 0 and (diff % (w+o)) < w

def get_masters_for_service_on_date(service: str, date_text: str) -> list[dict]:
    if get_salon_hours(date_text) is None:
        return []
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT m.id, m.name FROM masters m
            JOIN master_services ms ON m.id=ms.master_id
            WHERE m.is_active=1 AND ms.service=? ORDER BY m.name
        """, (service,)).fetchall()
    return [{"id":r[0],"name":r[1]} for r in rows if is_master_working(r[0], date_text)]


# ─── Booking DB ───────────────────────────────────────────────────────────────

def is_slot_blocked(mid: int, date_text: str, time_text: str) -> bool:
    """True if this time slot is occupied (direct booking or covered by a 2-hour booking)."""
    with sqlite3.connect(DB_PATH) as conn:
        # Direct booking at this time
        row = conn.execute("SELECT duration FROM bookings WHERE master_id=? AND date=? AND time=?",
                           (mid, date_text, time_text)).fetchone()
        if row is not None:
            return True
        # Previous slot has a 2-hour booking that extends here
        idx = WORKING_TIMES.index(time_text) if time_text in WORKING_TIMES else -1
        if idx > 0:
            prev = WORKING_TIMES[idx-1]
            row = conn.execute("SELECT duration FROM bookings WHERE master_id=? AND date=? AND time=?",
                               (mid, date_text, prev)).fetchone()
            if row and row[0] >= 2:
                return True
    return False

def save_booking(user_id, username, master_id, service, date_text, time_text, name, phone) -> int | None:
    duration = get_service_duration(service)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO bookings (user_id,username,master_id,service,date,time,duration,name,phone,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)
            """, (user_id, username, master_id, service, date_text, time_text, duration,
                  name, phone, datetime.now().strftime("%d.%m.%Y %H:%M:%S")))
            conn.commit()
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None

def delete_booking_by_id(bid: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM bookings WHERE id=?", (bid,))
        conn.commit()

def mark_booking_confirmed(bid: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE bookings SET client_confirmed=1 WHERE id=?", (bid,))
        conn.commit()

def mark_reminder_sent(bid: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE bookings SET reminder_sent=1 WHERE id=?", (bid,))
        conn.commit()

def mark_review_sent(bid: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE bookings SET review_sent=1 WHERE id=?", (bid,))
        conn.commit()

def mark_reengagement_sent(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO reengagement_log (user_id, sent_at) VALUES (?, ?)",
            (user_id, datetime.now().strftime("%d.%m.%Y %H:%M")),
        )
        conn.commit()

# ─── Roles ────────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def get_active_manager_ids() -> list[int]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT user_id FROM managers WHERE is_active=1"
        ).fetchall()
    return [r[0] for r in rows]

def is_manager(user_id: int) -> bool:
    return user_id in get_active_manager_ids()

def is_manager_or_admin(user_id: int) -> bool:
    return is_admin(user_id) or is_manager(user_id)

def admin_kb(user_id: int) -> ReplyKeyboardMarkup:
    return admin_super_keyboard if is_admin(user_id) else admin_keyboard

def add_manager(user_id: int, name: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO managers (user_id, name, added_at, is_active) VALUES (?,?,?,1)",
            (user_id, name, datetime.now().strftime("%d.%m.%Y %H:%M")),
        )

def deactivate_manager(manager_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("UPDATE managers SET is_active=0 WHERE id=?", (manager_id,))

def get_all_managers() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, user_id, name, is_active FROM managers ORDER BY id"
        ).fetchall()
    return [{"id": r[0], "user_id": r[1], "name": r[2], "is_active": r[3]} for r in rows]

def get_notifications_enabled() -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT value FROM admin_settings WHERE key='notifications'"
        ).fetchone()
    return row[0] == "1" if row else True

def toggle_notifications() -> bool:
    current = get_notifications_enabled()
    new_val = not current
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO admin_settings (key, value) VALUES ('notifications', ?)",
            ("1" if new_val else "0",),
        )
    return new_val

def log_action(manager_id: int, manager_name: str, action: str, details: str = ""):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO manager_action_log (manager_id, manager_name, action, details, logged_at) VALUES (?,?,?,?,?)",
            (manager_id, manager_name, action, details, datetime.now().strftime("%d.%m.%Y %H:%M")),
        )

def get_action_log(limit: int = 50) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT manager_id, manager_name, action, details, logged_at FROM manager_action_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"manager_id": r[0], "manager_name": r[1], "action": r[2], "details": r[3], "logged_at": r[4]} for r in rows]


# ─── Client blocking ───────────────────────────────────────────────────────────

def is_client_blocked(user_id: int, username: str | None) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        if username:
            row = conn.execute(
                "SELECT 1 FROM blocked_clients WHERE user_id=? OR username=?",
                (user_id, username),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM blocked_clients WHERE user_id=?",
                (user_id,),
            ).fetchone()
    return row is not None

def get_client_identity(query: str) -> dict | None:
    q = query.strip()
    by_username = False
    uname_val   = None
    p7 = p8     = None

    if q.startswith("@"):
        by_username = True
        uname_val   = q[1:]
    elif is_valid_phone(q):
        p7, p8 = phone_variants(q)
    else:
        by_username = True
        uname_val   = q

    with sqlite3.connect(DB_PATH) as conn:
        if by_username:
            row = conn.execute(
                "SELECT user_id, username, phone, name FROM bookings WHERE username=? LIMIT 1",
                (uname_val,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT user_id, username, phone, name FROM bookings WHERE phone IN (?,?) LIMIT 1",
                (p7, p8),
            ).fetchone()

    if not row:
        return None
    return {"user_id": row[0], "username": row[1], "phone": row[2], "name": row[3]}

def block_client(user_id: int | None, username: str | None, phone: str | None, name: str | None):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO blocked_clients (user_id, username, phone, name, blocked_at) VALUES (?,?,?,?,?)",
            (user_id, username, phone, name, datetime.now().strftime("%d.%m.%Y %H:%M")),
        )
        conn.commit()

def unblock_client(block_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM blocked_clients WHERE id=?", (block_id,))
        conn.commit()

def get_blocked_clients() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, user_id, username, phone, name, blocked_at FROM blocked_clients ORDER BY blocked_at DESC"
        ).fetchall()
    return [{"id": r[0], "user_id": r[1], "username": r[2], "phone": r[3],
             "name": r[4], "blocked_at": r[5]} for r in rows]

def archive_cancelled_booking(bid: int, reason: str):
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT b.id, b.user_id, b.username, b.master_id,
                   COALESCE(m.name, 'Неизвестно'), b.service, b.date, b.time,
                   b.duration, b.name, b.phone, b.created_at
            FROM bookings b LEFT JOIN masters m ON b.master_id = m.id
            WHERE b.id = ?
        """, (bid,)).fetchone()
        if not row:
            return
        conn.execute("""
            INSERT OR IGNORE INTO cancelled_bookings
            (id, user_id, username, master_id, master_name, service, date, time,
             duration, name, phone, created_at, cancelled_at, cancel_reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (*row, datetime.now().strftime("%d.%m.%Y %H:%M"), reason))
        conn.commit()

def get_client_history(query: str) -> dict | None:
    q = query.strip()
    by_username = False
    uname_val   = None
    p7 = p8     = None

    if q.startswith("@"):
        by_username = True
        uname_val   = q[1:]
    elif is_valid_phone(q):
        p7, p8 = phone_variants(q)
    else:
        by_username = True
        uname_val   = q

    with sqlite3.connect(DB_PATH) as conn:
        if by_username:
            active_rows = conn.execute("""
                SELECT b.id, b.service, b.date, b.time, b.name, b.phone,
                       COALESCE(m.name,'Неизвестно'), b.duration
                FROM bookings b LEFT JOIN masters m ON b.master_id=m.id
                WHERE b.username=? ORDER BY b.date, b.time
            """, (uname_val,)).fetchall()
            cancelled_rows = conn.execute("""
                SELECT id, service, date, time, name, phone, master_name, duration,
                       cancelled_at, cancel_reason
                FROM cancelled_bookings WHERE username=?
                ORDER BY date DESC, time DESC
            """, (uname_val,)).fetchall()
        else:
            active_rows = conn.execute("""
                SELECT b.id, b.service, b.date, b.time, b.name, b.phone,
                       COALESCE(m.name,'Неизвестно'), b.duration
                FROM bookings b LEFT JOIN masters m ON b.master_id=m.id
                WHERE b.phone IN (?,?) ORDER BY b.date, b.time
            """, (p7, p8)).fetchall()
            cancelled_rows = conn.execute("""
                SELECT id, service, date, time, name, phone, master_name, duration,
                       cancelled_at, cancel_reason
                FROM cancelled_bookings WHERE phone IN (?,?)
                ORDER BY date DESC, time DESC
            """, (p7, p8)).fetchall()

    if not active_rows and not cancelled_rows:
        return None

    def _to_active(r):
        return {"id": r[0], "service": r[1], "date": r[2], "time": r[3],
                "name": r[4], "phone": r[5], "master_name": r[6], "duration": r[7]}

    def _to_cancelled(r):
        return {"id": r[0], "service": r[1], "date": r[2], "time": r[3],
                "name": r[4], "phone": r[5], "master_name": r[6], "duration": r[7],
                "cancelled_at": r[8], "cancel_reason": r[9]}

    active    = [_to_active(r)    for r in active_rows]
    cancelled = [_to_cancelled(r) for r in cancelled_rows]

    all_records = active + cancelled
    latest      = max(all_records, key=lambda x: x["date"])
    username    = uname_val if by_username else (active_rows[0][0] if active_rows else None)
    if not by_username and active_rows:
        with sqlite3.connect(DB_PATH) as conn:
            urow = conn.execute(
                "SELECT username FROM bookings WHERE phone IN (?,?) LIMIT 1", (p7, p8)
            ).fetchone()
            username = urow[0] if urow else None

    return {
        "name":      latest["name"],
        "phone":     latest["phone"],
        "username":  username,
        "active":    active,
        "cancelled": cancelled,
    }

def get_users_for_reengagement() -> list[dict]:
    now       = datetime.now()
    threshold = now.date() - timedelta(days=90)

    with sqlite3.connect(DB_PATH) as conn:
        rows      = conn.execute("SELECT user_id, date, time FROM bookings").fetchall()
        sent_rows = conn.execute("SELECT user_id, sent_at FROM reengagement_log").fetchall()

    sent: dict[int, datetime] = {}
    for uid, sat in sent_rows:
        try:
            sent[uid] = datetime.strptime(sat, "%d.%m.%Y %H:%M")
        except ValueError:
            pass

    user_dts: dict[int, list] = {}
    for uid, dt, tm in rows:
        try:
            bdt = datetime.strptime(f"{dt} {tm}", "%d.%m.%Y %H:%M")
        except ValueError:
            continue
        user_dts.setdefault(uid, []).append(bdt)

    result = []
    for uid, dts in user_dts.items():
        if any(d > now for d in dts):
            continue
        if max(dts).date() > threshold:
            continue
        if uid in sent and (now - sent[uid]).days < 90:
            continue
        result.append({"user_id": uid})
    return result

def get_bookings_for_review() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT b.id, b.user_id, b.service, b.date, b.time, b.name,
                   COALESCE(m.name, '') as master_name, b.master_id
            FROM bookings b LEFT JOIN masters m ON b.master_id=m.id
            WHERE b.review_sent=0
        """).fetchall()
    now = datetime.now()
    result = []
    for bid, uid, svc, dt, tm, nm, mn, mid in rows:
        booking_dt = datetime.strptime(f"{dt} {tm}", "%d.%m.%Y %H:%M")
        if now >= booking_dt + timedelta(hours=3):
            result.append({"id":bid,"user_id":uid,"service":svc,"date":dt,"time":tm,
                           "name":nm,"master_name":mn,"master_id":mid})
    return result

def _booking_row_to_dict(row) -> dict:
    bid, svc, dt, tm, nm, ph, mn, dur = row
    return {"id":bid,"service":svc,"date":dt,"time":tm,"name":nm,"phone":ph,"master_name":mn,"duration":dur}

def get_nearest_booking_for_user(uid: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT b.id,b.service,b.date,b.time,b.name,b.phone,
                   COALESCE(m.name,'Неизвестно'),b.duration
            FROM bookings b LEFT JOIN masters m ON b.master_id=m.id
            WHERE b.user_id=? ORDER BY b.date,b.time
        """, (uid,)).fetchall()
    now = datetime.now()
    for row in rows:
        d = _booking_row_to_dict(row)
        if datetime.strptime(f"{d['date']} {d['time']}", "%d.%m.%Y %H:%M") > now:
            return d
    return None

def get_all_bookings_split(uid: int) -> tuple[list[dict], list[dict]]:
    """Возвращает (прошлые, активные) записи пользователя."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT b.id,b.service,b.date,b.time,b.name,b.phone,
                   COALESCE(m.name,'Неизвестно'),b.duration
            FROM bookings b LEFT JOIN masters m ON b.master_id=m.id
            WHERE b.user_id=? ORDER BY b.date,b.time
        """, (uid,)).fetchall()
    now = datetime.now()
    past, active = [], []
    for row in rows:
        d = _booking_row_to_dict(row)
        bdt = datetime.strptime(f"{d['date']} {d['time']}", "%d.%m.%Y %H:%M")
        (past if bdt < now else active).append(d)
    return past, active

def get_all_future_bookings_for_user(uid: int) -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT b.id,b.service,b.date,b.time,b.name,b.phone,
                   COALESCE(m.name,'Неизвестно'),b.duration
            FROM bookings b LEFT JOIN masters m ON b.master_id=m.id
            WHERE b.user_id=? ORDER BY b.date,b.time
        """, (uid,)).fetchall()
    now = datetime.now()
    return [_booking_row_to_dict(r) for r in rows
            if datetime.strptime(f"{r[2]} {r[3]}", "%d.%m.%Y %H:%M") > now]

def get_bookings_for_reminder() -> list[dict]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT b.id,b.user_id,b.service,b.date,b.time,b.name,
                   COALESCE(m.name,'')
            FROM bookings b LEFT JOIN masters m ON b.master_id=m.id
            WHERE b.reminder_sent=0
        """).fetchall()
    now = datetime.now()
    result = []
    for bid, uid, svc, dt, tm, nm, mn in rows:
        bdt = datetime.strptime(f"{dt} {tm}", "%d.%m.%Y %H:%M")
        diff = bdt - now
        if bdt <= now + timedelta(hours=48):
            should = timedelta(0) < diff <= timedelta(hours=2)
        else:
            should = timedelta(0) < diff <= timedelta(hours=24)
        if should:
            result.append({"id":bid,"user_id":uid,"service":svc,"date":dt,"time":tm,"name":nm,"master_name":mn})
    return result


# ─── Keyboard builders ────────────────────────────────────────────────────────

def build_times_keyboard(times: list[str]) -> ReplyKeyboardMarkup:
    kb = []
    for i in range(0, len(times), 3):
        kb.append([KeyboardButton(text=t) for t in times[i:i+3]])
    kb.append([KeyboardButton(text="Отмена")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_free_times_for_master(mid: int, date_text: str, service: str = "") -> list[str]:
    hours = get_salon_hours(date_text)
    if hours is None:
        return []
    ot, ct = hours
    today = date.today()
    now   = datetime.now()
    sel   = datetime.strptime(date_text, "%d.%m.%Y").date()
    dur   = get_service_duration(service) if service else 1
    result = []
    for i, t in enumerate(WORKING_TIMES):
        if t < ot or t >= ct:
            continue
        if dur == 2:
            if i+1 >= len(WORKING_TIMES) or WORKING_TIMES[i+1] >= ct:
                continue
            if is_slot_blocked(mid, date_text, WORKING_TIMES[i+1]):
                continue
        if is_slot_blocked(mid, date_text, t):
            continue
        if sel == today:
            sdt = datetime.strptime(f"{date_text} {t}", "%d.%m.%Y %H:%M")
            if sdt <= now:
                continue
        result.append(t)
    return result

def has_free_slots(service: str, date_text: str) -> bool:
    return any(
        get_free_times_for_master(m["id"], date_text, service)
        for m in get_masters_for_service_on_date(service, date_text)
    )

def build_date_keyboard(page: int, service: str) -> tuple[ReplyKeyboardMarkup, int]:
    today = date.today()
    avail = [today + timedelta(days=i) for i in range(15)
             if has_free_slots(service, (today+timedelta(days=i)).strftime("%d.%m.%Y"))]
    psz = 7
    total = max(1, (len(avail)+psz-1)//psz)
    page  = max(0, min(page, total-1))
    chunk = avail[page*psz:(page+1)*psz]
    kb = []
    for i in range(0, len(chunk), 2):
        kb.append([KeyboardButton(text=d.strftime("%d.%m.%Y")) for d in chunk[i:i+2]])
    nav = []
    if page > 0:         nav.append(KeyboardButton(text="<"))
    if page < total-1:   nav.append(KeyboardButton(text=">"))
    if nav: kb.append(nav)
    kb.append([KeyboardButton(text="Отмена")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True), total

def build_services_sel_keyboard(selected: set) -> ReplyKeyboardMarkup:
    svcs = get_active_services()
    rows = [[KeyboardButton(text=("✅ " if s["name"] in selected else "☐ ") + s["name"])] for s in svcs]
    rows.append([KeyboardButton(text="Готово"), KeyboardButton(text="Назад")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def build_masters_kb(masters: list[dict]) -> ReplyKeyboardMarkup:
    rows = []
    for i in range(0, len(masters), 3):
        rows.append([KeyboardButton(text=m["name"]) for m in masters[i:i+3]])
    rows.append([KeyboardButton(text="Назад")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def format_master_info(m: dict) -> str:
    svcs = ", ".join(m["services"]) if m["services"] else "—"
    st   = "✅ активен" if m["is_active"] else "❌ деактивирован"
    sched = m["schedule"]
    if sched is None or sched[0] == "all":
        sg = "каждый день"
    else:
        sg = f"{sched[0]} (с {sched[3]})"
    return f"Мастер: {m['name']}\nСтатус: {st}\nУслуги: {svcs}\nГрафик: {sg}"


# ─── Schedule view ─────────────────────────────────────────────────────────────

def build_day_schedule_html(date_text: str) -> str:
    hours = get_salon_hours(date_text)
    if hours is None:
        return f"<b>{he(date_text)}</b> — салон закрыт"
    ot, ct = hours

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("""
            SELECT b.time, b.service, b.name, b.phone, b.duration,
                   COALESCE(m.name,'Неизвестно'), b.username
            FROM bookings b LEFT JOIN masters m ON b.master_id=m.id
            WHERE b.date=? ORDER BY b.time
        """, (date_text,)).fetchall()

    by_time: dict[str, list] = {}
    for tm, svc, nm, ph, dur, mn, uname in rows:
        by_time.setdefault(tm, []).append((svc, nm, ph, dur, mn, uname))

    lines = [f"<b>📅 {he(date_text)}</b>"]
    for t in WORKING_TIMES:
        if t < ot or t >= ct:
            continue
        if t in by_time:
            lines.append(f"\n  <b>{t}</b> — записаны:")
            for svc, nm, ph, dur, mn, uname in by_time[t]:
                dur_tag = " (2ч)" if dur >= 2 else ""
                count = get_client_visit_count(ph, uname)
                badge = loyalty_badge(count)
                lines.append(f"    {he(svc)}{dur_tag} | Мастер: {he(mn)} | Клиент: {he(nm)}{he(badge)} | {he(ph)}")
        else:
            lines.append(f"  {t} — свободно")
    return "\n".join(lines)


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def send_photo_with_text(msg: Message, path: Path, text: str):
    if path.exists():
        await msg.answer_photo(photo=FSInputFile(path), caption=text)
    else:
        await msg.answer(text)

async def notify_manager(bot: Bot, text: str):
    for uid in get_active_manager_ids():
        try:
            await bot.send_message(chat_id=uid, text=text)
        except Exception:
            logging.exception("Ошибка уведомления менеджера %s", uid)
    if get_notifications_enabled():
        try:
            await bot.send_message(chat_id=ADMIN_ID, text=text)
        except Exception:
            logging.exception("Ошибка уведомления администратора")

async def reminder_loop(bot: Bot):
    while True:
        for b in get_bookings_for_reminder():
            try:
                ml = f"Мастер: {b['master_name']}\n" if b["master_name"] else ""
                await bot.send_message(
                    chat_id=b["user_id"],
                    text=f"Напоминание о записи.\n\nУслуга: {b['service']}\n{ml}"
                         f"Дата: {b['date']}\nВремя: {b['time']}\n\n"
                         "Подтвердите визит, отмените или перезапишитесь.",
                    reply_markup=reminder_keyboard,
                )
                mark_reminder_sent(b["id"])
            except Exception:
                logging.exception("Ошибка напоминания")
        await asyncio.sleep(60)


async def review_loop(bot: Bot):
    while True:
        for b in get_bookings_for_review():
            try:
                uid  = b["user_id"]
                name = b["name"]

                key = StorageKey(bot_id=bot.id, chat_id=uid, user_id=uid)
                cur  = await storage.get_state(key=key)

                # Не перебиваем активный флоу клиента — попробуем позже
                if cur is not None:
                    continue

                await storage.set_state(key=key, state=ReviewState.rating_pending.state)
                await storage.set_data(key=key, data={
                    "review_name":      name,
                    "review_service":   b["service"],
                    "review_master":    b["master_name"],
                    "review_master_id": b["master_id"],
                })

                await bot.send_message(
                    chat_id=uid,
                    text=f"{name}, спасибо что посетили наш салон!\n\n"
                         "Пожалуйста, оцените нашу работу.\n"
                         "5 — очень понравилось, 1 — совершенно не понравилось.",
                    reply_markup=rating_keyboard,
                )
                mark_review_sent(b["id"])
            except Exception:
                logging.exception("Ошибка отправки запроса на отзыв")
        await asyncio.sleep(60)


async def reengagement_loop(bot: Bot):
    while True:
        for u in get_users_for_reengagement():
            try:
                key = StorageKey(bot_id=bot.id, chat_id=u["user_id"], user_id=u["user_id"])
                cur = await storage.get_state(key=key)
                if cur is not None:
                    continue
                await bot.send_message(
                    chat_id=u["user_id"],
                    text="Давно не были в нашем салоне — мы соскучились! 💅\n\n"
                         "Готовы предложить вам маникюр и педикюр от наших мастеров.\n"
                         "Запишитесь прямо сейчас!",
                    reply_markup=reengagement_promo_keyboard,
                )
                mark_reengagement_sent(u["user_id"])
            except Exception:
                logging.exception("Ошибка рассылки повторного визита")
        await asyncio.sleep(3600)


# ─── Client handlers ──────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def start_handler(msg: Message, state: FSMContext):
    await state.clear()
    await send_photo_with_text(msg, BANNER_IMAGE,
        "Добро пожаловать в Nail Studio.\n\nЯ помогу узнать цены, мастеров и записаться.")
    await msg.answer("Выберите действие:", reply_markup=main_keyboard)

async def _try_admin_info_select(msg: Message, state: FSMContext) -> bool:
    """Если менеджер выбирает раздел в режиме info_select — запускает редактирование.
    Возвращает True если перехватил, False если нет."""
    cur = await state.get_state()
    if cur != AdminState.info_select.state or msg.text not in INFO_SECTION_NAMES:
        return False
    section_key, _ = INFO_SECTION_NAMES[msg.text]
    page = get_info_page(section_key)
    cur_text = page["text"] or "(автогенерация)"
    await state.update_data(
        info_section_key=section_key,
        info_section_label=msg.text,
        info_cur_photo=page["photo_file_id"],
    )
    await state.set_state(AdminState.info_text)
    await msg.answer(
        f"Раздел: {msg.text}\nТекущий текст:\n{cur_text}\n\n"
        "Введите новый текст или нажмите «Не менять»:",
        reply_markup=keep_keyboard,
    )
    return True

async def _send_info_page(msg: Message, section: str, default_image: Path, default_text: str):
    page = get_info_page(section)
    text = page["text"] or default_text
    if page["photo_file_id"]:
        await msg.answer_photo(photo=page["photo_file_id"], caption=text)
    else:
        await send_photo_with_text(msg, default_image, text)

@dp.message(F.text == "О салоне")
async def about_handler(msg: Message, state: FSMContext):
    if await _try_admin_info_select(msg, state):
        return
    await _send_info_page(msg, "about", ABOUT_IMAGE,
        "Nail Studio — салон маникюра и педикюра.\n\nРаботаем ежедневно 10:00–20:00.")

@dp.message(F.text == "Услуги и цены")
async def prices_handler(msg: Message, state: FSMContext):
    if await _try_admin_info_select(msg, state):
        return
    page = get_info_page("prices")
    if page["text"]:
        text = page["text"]
    else:
        svcs = get_active_services()
        lines = ["Услуги и цены:\n"]
        for s in svcs:
            dur = " (2 ч)" if s["duration"] == 2 else ""
            lines.append(f"{s['name']}{dur} — {s['price']} ₽")
        text = "\n".join(lines)
    if page["photo_file_id"]:
        await msg.answer_photo(photo=page["photo_file_id"], caption=text)
    else:
        await send_photo_with_text(msg, PRICES_IMAGE, text)

@dp.message(AdminState.admin_home, F.text == "💁‍♀️Мастера")
async def admin_masters_home(msg: Message, state: FSMContext):
    await state.set_state(AdminState.master_menu)
    await msg.answer("Управление мастерами:", reply_markup=admin_masters_keyboard)

@dp.message(F.text == "Мастера")
async def masters_handler(msg: Message, state: FSMContext):
    if await _try_admin_info_select(msg, state):
        return
    active = get_active_masters()
    if not active:
        await send_photo_with_text(msg, MASTERS_IMAGE, "Информация о мастерах скоро появится.")
        return
    lines = ["Наши мастера:\n"]
    for m in active:
        master = get_master_by_id(m["id"])
        svcs = ", ".join(master["services"]) if master["services"] else "—"
        lines.append(f"• {m['name']} — {svcs}")
    await send_photo_with_text(msg, MASTERS_IMAGE, "\n".join(lines))

@dp.message(F.text == "Контакты и адрес")
async def contacts_handler(msg: Message, state: FSMContext):
    if await _try_admin_info_select(msg, state):
        return
    await _send_info_page(msg, "contacts", CONTACTS_IMAGE,
        "Контакты Nail Studio:\n\nАдрес: Москва, ул. Примерная, 10\n"
        "Телефон: +7 999 123-45-67\nTelegram: @nailstudio_manager\n"
        "Время работы: 10:00–20:00")

@dp.message(F.text == "Записаться")
async def start_booking(msg: Message, state: FSMContext):
    if is_client_blocked(msg.from_user.id, msg.from_user.username):
        await msg.answer(
            "Запись через бот недоступна. По вопросам обращайтесь к администратору.",
            reply_markup=main_keyboard,
        )
        return
    await state.set_state(BookingState.choosing_service)
    await msg.answer("Выберите услугу:", reply_markup=build_client_services_keyboard())

@dp.message(F.text == "Отмена")
async def cancel_handler(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Действие отменено.", reply_markup=main_keyboard)


# ─── Booking FSM ──────────────────────────────────────────────────────────────

@dp.message(BookingState.choosing_service)
async def choose_service(msg: Message, state: FSMContext):
    svc_names = [s["name"] for s in get_active_services()]
    if msg.text not in svc_names:
        await msg.answer("Выберите услугу кнопкой.", reply_markup=build_client_services_keyboard())
        return
    svc = msg.text
    if not any(has_free_slots(svc, (date.today()+timedelta(days=i)).strftime("%d.%m.%Y")) for i in range(15)):
        await msg.answer("Сейчас нет свободных мастеров для этой услуги.", reply_markup=main_keyboard)
        await state.clear()
        return
    await state.update_data(service=svc, date_page=0)
    await state.set_state(BookingState.entering_date)
    kb, _ = build_date_keyboard(0, svc)
    await msg.answer("Выберите дату:", reply_markup=kb)

@dp.message(BookingState.entering_date)
async def enter_date(msg: Message, state: FSMContext):
    data = await state.get_data()
    svc  = data["service"]
    page = data.get("date_page", 0)
    if msg.text in (">","<"):
        np = page + (1 if msg.text==">" else -1)
        await state.update_data(date_page=np)
        kb, _ = build_date_keyboard(np, svc)
        await msg.answer("Выберите дату:", reply_markup=kb)
        return
    d = parse_booking_date(msg.text.strip())
    if d is None:
        kb, _ = build_date_keyboard(page, svc)
        await msg.answer("Выберите дату кнопкой.", reply_markup=kb)
        return
    ok, err = is_date_available(d)
    if not ok:
        kb, _ = build_date_keyboard(page, svc)
        await msg.answer(err, reply_markup=kb)
        return
    dt = d.strftime("%d.%m.%Y")
    masters = get_masters_for_service_on_date(svc, dt)
    if not masters:
        kb, _ = build_date_keyboard(page, svc)
        await msg.answer("На эту дату нет мастеров. Выберите другую.", reply_markup=kb)
        return
    await state.update_data(date=dt)
    if len(masters) == 1:
        m = masters[0]
        times = get_free_times_for_master(m["id"], dt, svc)
        if not times:
            kb, _ = build_date_keyboard(page, svc)
            await msg.answer("На эту дату нет свободных окон. Выберите другую.", reply_markup=kb)
            return
        await state.update_data(master_id=m["id"], master_name=m["name"])
        await state.set_state(BookingState.choosing_time)
        await msg.answer(f"Ваш мастер: {m['name']}\n\nВыберите время:", reply_markup=build_times_keyboard(times))
    else:
        await state.update_data(available_masters=masters)
        await state.set_state(BookingState.choosing_master)
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=m["name"])] for m in masters]+[[KeyboardButton(text="Отмена")]],
            resize_keyboard=True)
        await msg.answer("Выберите мастера:", reply_markup=kb)

@dp.message(BookingState.choosing_master)
async def choose_master(msg: Message, state: FSMContext):
    data = await state.get_data()
    av   = data.get("available_masters", [])
    sel  = next((m for m in av if m["name"]==msg.text), None)
    if not sel:
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=m["name"])] for m in av]+[[KeyboardButton(text="Отмена")]],
            resize_keyboard=True)
        await msg.answer("Выберите кнопкой.", reply_markup=kb)
        return
    times = get_free_times_for_master(sel["id"], data["date"], data["service"])
    if not times:
        await state.set_state(BookingState.entering_date)
        kb, _ = build_date_keyboard(data.get("date_page",0), data["service"])
        await msg.answer("Нет свободных окон. Выберите другую дату.", reply_markup=kb)
        return
    await state.update_data(master_id=sel["id"], master_name=sel["name"])
    await state.set_state(BookingState.choosing_time)
    await msg.answer(f"Мастер: {sel['name']}\n\nВыберите время:", reply_markup=build_times_keyboard(times))

@dp.message(BookingState.choosing_time)
async def choose_time(msg: Message, state: FSMContext):
    data  = await state.get_data()
    times = get_free_times_for_master(data["master_id"], data["date"], data["service"])
    if msg.text not in times:
        await msg.answer("Выберите время кнопкой.", reply_markup=build_times_keyboard(times))
        return
    await state.update_data(time=msg.text)
    await state.set_state(BookingState.entering_name)
    await msg.answer("Введите ваше имя:")

@dp.message(BookingState.entering_name)
async def enter_name(msg: Message, state: FSMContext):
    name = msg.text.strip()
    name = name[0].upper() + name[1:] if name else name
    if len(name) < 2:
        await msg.answer("Имя слишком короткое.")
        return
    if len(name) > 40:
        await msg.answer("Имя слишком длинное.")
        return
    if contains_banned_words(name):
        await msg.answer("Некорректное имя.")
        return
    await state.update_data(name=name)
    await state.set_state(BookingState.entering_phone)
    await msg.answer("Введите номер телефона:")

@dp.message(BookingState.entering_phone)
async def enter_phone(msg: Message, state: FSMContext):
    phone = msg.text.strip()
    if not is_valid_phone(phone):
        await msg.answer("Формат: 8XXXXXXXXXX или +7XXXXXXXXXX")
        return
    await state.update_data(phone=phone)
    data = await state.get_data()
    dur  = get_service_duration(data["service"])
    dur_note = " (займёт 2 часа)" if dur == 2 else ""
    await state.set_state(BookingState.confirming)
    await msg.answer(
        f"Проверьте запись:\n\n"
        f"Услуга: {data['service']}{dur_note}\n"
        f"Мастер: {data['master_name']}\n"
        f"Дата: {data['date']}\n"
        f"Время: {data['time']}\n"
        f"Имя: {data['name']}\n"
        f"Телефон: {phone}\n\nПодтвердить?",
        reply_markup=confirm_keyboard)

@dp.message(BookingState.confirming)
async def confirm_booking(msg: Message, state: FSMContext):
    if msg.text == "Отменить запись":
        await state.clear()
        await msg.answer("Запись отменена.", reply_markup=main_keyboard)
        return
    if msg.text != "Подтвердить запись":
        await msg.answer("Нажмите кнопку.", reply_markup=confirm_keyboard)
        return
    data  = await state.get_data()
    booking_id = save_booking(msg.from_user.id, msg.from_user.username,
                              data["master_id"], data["service"],
                              data["date"], data["time"], data["name"], data["phone"])
    if not booking_id:
        await state.set_state(BookingState.entering_date)
        kb, _ = build_date_keyboard(0, data["service"])
        await msg.answer("Время уже занято. Выберите другую дату.", reply_markup=kb)
        return

    asyncio.create_task(sheets_add_booking(
        booking_id,
        datetime.now().strftime("%d.%m.%Y %H:%M"),
        data["date"], data["time"], data["service"], data["master_name"],
        data["name"], data["phone"], msg.from_user.username,
    ))

    tg = f"@{msg.from_user.username}" if msg.from_user.username else "Нет username"
    dur = get_service_duration(data["service"])
    dur_tag = " (2ч)" if dur == 2 else ""
    count = get_client_visit_count(data["phone"], msg.from_user.username)
    badge = loyalty_badge(count)
    await notify_manager(msg.bot,
        f"Новая запись\n\nУслуга: {data['service']}{dur_tag}\nМастер: {data['master_name']}\n"
        f"Дата: {data['date']}\nВремя: {data['time']}\n"
        f"Клиент: {data['name']}{badge}\nТелефон: {data['phone']}\nTelegram: {tg}")
    await msg.answer(
        f"Вы записаны!\n\nУслуга: {data['service']}{dur_tag}\nМастер: {data['master_name']}\n"
        f"Дата: {data['date']}\nВремя: {data['time']}\n\nЖдём вас!",
        reply_markup=main_keyboard)
    await state.clear()


# ─── My Bookings / Cancel ─────────────────────────────────────────────────────

@dp.message(F.text == "Мои записи")
async def my_bookings(msg: Message):
    past, active = get_all_bookings_split(msg.from_user.id)
    if not past and not active:
        await msg.answer("У вас ещё не было записей.", reply_markup=main_keyboard)
        return
    lines = []
    if active:
        lines.append("📅 Актуальные записи:\n")
        for i, b in enumerate(active, 1):
            lines.append(f"{i}. {b['date']}, {b['time']} — {b['service']} (мастер: {b['master_name']})")
    if past:
        if active:
            lines.append("")
        lines.append("🕐 Прошлые записи:\n")
        for i, b in enumerate(reversed(past), 1):
            lines.append(f"{i}. {b['date']}, {b['time']} — {b['service']} (мастер: {b['master_name']})")
    await msg.answer("\n".join(lines), reply_markup=main_keyboard)

@dp.message(F.text == "Отменить запись")
async def cancel_booking_menu(msg: Message, state: FSMContext):
    bs = get_all_future_bookings_for_user(msg.from_user.id)
    if not bs:
        await msg.answer("У вас нет активных записей.", reply_markup=main_keyboard)
        return
    if len(bs) == 1:
        b = bs[0]
        await state.set_state(CancelBookingState.confirming_single)
        await state.update_data(cancel_booking_id=b["id"])
        await msg.answer(
            f"Отменить запись?\n\nУслуга: {b['service']}\nМастер: {b['master_name']}\n"
            f"Дата: {b['date']}\nВремя: {b['time']}",
            reply_markup=confirm_cancel_keyboard)
        return
    rows = [[KeyboardButton(text=f"{i}. {b['date']} {b['time']} {b['service']}")] for i,b in enumerate(bs,1)]
    rows.append([KeyboardButton(text="Отмена")])
    lines = ["Выберите запись:\n"] + [f"{i}. {b['date']} {b['time']} — {b['service']}" for i,b in enumerate(bs,1)]
    await state.set_state(CancelBookingState.choosing_booking)
    await state.update_data(cancel_bookings=bs)
    await msg.answer("\n".join(lines), reply_markup=ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True))

@dp.message(CancelBookingState.confirming_single)
async def confirm_single_cancel(msg: Message, state: FSMContext):
    if msg.text == "Нет, назад":
        await state.clear()
        await msg.answer("Отмена записи отменена.", reply_markup=main_keyboard)
        return
    if msg.text != "Да, отменить":
        await msg.answer("Нажмите кнопку.", reply_markup=confirm_cancel_keyboard)
        return
    data = await state.get_data()
    bid  = data["cancel_booking_id"]
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("""
            SELECT b.service,b.date,b.time,b.name,b.phone,COALESCE(m.name,'')
            FROM bookings b LEFT JOIN masters m ON b.master_id=m.id WHERE b.id=?
        """, (bid,)).fetchone()
    await state.clear()
    if not row:
        await msg.answer("Запись не найдена.", reply_markup=main_keyboard)
        return
    svc,dt,tm,nm,ph,mn = row
    archive_cancelled_booking(bid, "client")
    delete_booking_by_id(bid)
    asyncio.create_task(sheets_set_status(bid, "отменена"))
    await msg.answer("Запись отменена.", reply_markup=main_keyboard)
    await notify_manager(msg.bot, f"Клиент отменил запись\n\nУслуга: {svc}\nМастер: {mn}\nДата: {dt}\nВремя: {tm}\nИмя: {nm}\nТелефон: {ph}")

@dp.message(CancelBookingState.choosing_booking)
async def choose_booking_cancel(msg: Message, state: FSMContext):
    if msg.text == "Отмена":
        await state.clear()
        await msg.answer("Отменено.", reply_markup=main_keyboard)
        return
    data = await state.get_data()
    bs   = data.get("cancel_bookings", [])
    idx  = None
    t    = msg.text or ""
    if t and t[0].isdigit():
        dp_ = t.find(".")
        if dp_ != -1:
            try: idx = int(t[:dp_]) - 1
            except ValueError: pass
    if idx is None or not (0 <= idx < len(bs)):
        await msg.answer("Выберите кнопкой.")
        return
    b = bs[idx]
    archive_cancelled_booking(b["id"], "client")
    delete_booking_by_id(b["id"])
    asyncio.create_task(sheets_set_status(b["id"], "отменена"))
    await state.clear()
    await msg.answer(f"Запись отменена.\n\nУслуга: {b['service']}\nМастер: {b['master_name']}\nДата: {b['date']}\nВремя: {b['time']}", reply_markup=main_keyboard)
    await notify_manager(msg.bot, f"Клиент отменил запись\n\nУслуга: {b['service']}\nМастер: {b['master_name']}\nДата: {b['date']}\nВремя: {b['time']}\nИмя: {b['name']}\nТелефон: {b['phone']}")


# ─── Reminder handlers ────────────────────────────────────────────────────────

@dp.message(F.text == "Подтвердить визит")
async def confirm_visit(msg: Message):
    b = get_nearest_booking_for_user(msg.from_user.id)
    if not b:
        await msg.answer("Запись не найдена.", reply_markup=main_keyboard)
        return
    mark_booking_confirmed(b["id"])
    await msg.answer(f"Визит подтверждён.\n\nУслуга: {b['service']}\nМастер: {b['master_name']}\nДата: {b['date']}\nВремя: {b['time']}", reply_markup=main_keyboard)
    await notify_manager(msg.bot, f"Клиент подтвердил визит\n\nУслуга: {b['service']}\nМастер: {b['master_name']}\nДата: {b['date']}\nВремя: {b['time']}\nИмя: {b['name']}\nТелефон: {b['phone']}")

@dp.message(F.text == "Отменить визит")
async def cancel_visit(msg: Message):
    b = get_nearest_booking_for_user(msg.from_user.id)
    if not b:
        await msg.answer("Запись не найдена.", reply_markup=main_keyboard)
        return
    archive_cancelled_booking(b["id"], "reminder")
    delete_booking_by_id(b["id"])
    asyncio.create_task(sheets_set_status(b["id"], "отменена"))
    await msg.answer("Запись отменена.", reply_markup=main_keyboard)
    await notify_manager(msg.bot, f"Клиент отменил запись\n\nУслуга: {b['service']}\nМастер: {b['master_name']}\nДата: {b['date']}\nВремя: {b['time']}\nИмя: {b['name']}\nТелефон: {b['phone']}")

@dp.message(F.text == "Перезаписаться")
async def reschedule(msg: Message, state: FSMContext):
    b = get_nearest_booking_for_user(msg.from_user.id)
    if b:
        archive_cancelled_booking(b["id"], "reschedule")
        delete_booking_by_id(b["id"])
        asyncio.create_task(sheets_set_status(b["id"], "перезапись"))
        await notify_manager(msg.bot, f"Клиент перезаписывается.\n\nУслуга: {b['service']}\nМастер: {b['master_name']}\nДата: {b['date']}\nВремя: {b['time']}\nИмя: {b['name']}")
    await state.set_state(BookingState.choosing_service)
    await msg.answer("Выберите услугу:", reply_markup=build_client_services_keyboard())


# ─── Admin handlers ───────────────────────────────────────────────────────────

@dp.message(Command("admin"))
async def admin_entry(msg: Message, state: FSMContext):
    if not is_manager_or_admin(msg.from_user.id):
        return
    await state.set_state(AdminState.admin_home)
    await msg.answer("Режим редактирования:", reply_markup=admin_kb(msg.from_user.id))

@dp.message(F.text == "Выход из редактирования")
async def admin_exit(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Вышли из редактирования.", reply_markup=main_keyboard)

async def go_home(msg, state):
    await state.set_state(AdminState.admin_home)
    await msg.answer("Главное меню:", reply_markup=admin_kb(msg.from_user.id))

async def go_masters(msg, state):
    await state.set_state(AdminState.master_menu)
    await msg.answer("Управление мастерами:", reply_markup=admin_masters_keyboard)

# admin_home routes
@dp.message(AdminState.admin_home, F.text == "📝Расписание салона")
async def admin_salon_section(msg: Message, state: FSMContext):
    await state.set_state(AdminState.salon_menu)
    await msg.answer("Расписание салона:", reply_markup=admin_salon_keyboard)

@dp.message(AdminState.admin_home, F.text == "🗓️Расписание записей")
async def admin_schedule_section(msg: Message, state: FSMContext):
    await state.set_state(AdminState.schedule_view)
    await msg.answer("За какой период показать расписание?", reply_markup=schedule_period_keyboard)


# ── Расписание записей ──

@dp.message(AdminState.schedule_view)
async def schedule_view_handler(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await go_home(msg, state)
        return
    periods = {"Сегодня": 1, "3 дня": 3, "Неделя": 7, "2 недели": 14}
    if msg.text not in periods:
        await msg.answer("Выберите период кнопкой.", reply_markup=schedule_period_keyboard)
        return
    days  = periods[msg.text]
    today = date.today()

    day_htmls = [
        build_day_schedule_html((today + timedelta(days=i)).strftime("%d.%m.%Y"))
        for i in range(days)
    ]

    if days == 1:
        await msg.answer(day_htmls[0], parse_mode="HTML")
        return

    # Несколько дней: каждый день — свёрнутая цитата, всё в одном сообщении.
    # Если суммарно слишком длинно — отправляем частями.
    MAX = 3800
    wrapped = [f"<blockquote expandable>{h}</blockquote>" for h in day_htmls]
    chunk_parts: list[str] = []
    chunk_len = 0
    for w in wrapped:
        if chunk_len + len(w) + 2 > MAX and chunk_parts:
            await msg.answer("\n\n".join(chunk_parts), parse_mode="HTML")
            chunk_parts, chunk_len = [], 0
        chunk_parts.append(w)
        chunk_len += len(w) + 2
    if chunk_parts:
        await msg.answer("\n\n".join(chunk_parts), parse_mode="HTML")


# ── Мастера меню ──

@dp.message(AdminState.master_menu, F.text == "Назад")
async def masters_back(msg: Message, state: FSMContext):
    await go_home(msg, state)

@dp.message(AdminState.master_menu, F.text == "👯Список мастеров")
async def masters_list(msg: Message, state: FSMContext):
    ms = get_all_masters()
    if not ms:
        await msg.answer("Мастеров нет.", reply_markup=admin_masters_keyboard)
        return
    lines = []
    for m in ms:
        master = get_master_by_id(m["id"])
        avg    = get_master_avg_rating(m["id"])
        avg_str = f"{avg}/5" if avg is not None else "нет отзывов"
        lines.append(format_master_info(master) + f"\nОценка: {avg_str}")
    await msg.answer("\n\n".join(lines), reply_markup=admin_masters_keyboard)

# ── Внести мастера ──

@dp.message(AdminState.master_menu, F.text == "🟢Внести мастера")
async def add_master_start(msg: Message, state: FSMContext):
    await state.set_state(AdminState.adding_name)
    await msg.answer("Введите имя нового мастера:", reply_markup=back_keyboard)

@dp.message(AdminState.adding_name)
async def add_master_name(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await go_masters(msg, state)
        return
    name = msg.text.strip()
    if len(name) < 2:
        await msg.answer("Минимум 2 символа.")
        return
    await state.update_data(new_name=name, new_svcs=[])
    await state.set_state(AdminState.adding_services)
    await msg.answer(f"Мастер: {name}\n\nВыберите услуги (можно несколько):",
                     reply_markup=build_services_sel_keyboard(set()))

@dp.message(AdminState.adding_services)
async def add_master_services(msg: Message, state: FSMContext):
    data = await state.get_data()
    sel: set = set(data.get("new_svcs", []))
    if msg.text == "Назад":
        await state.set_state(AdminState.adding_name)
        await msg.answer("Введите имя:", reply_markup=back_keyboard)
        return
    if msg.text == "Готово":
        if not sel:
            await msg.answer("Выберите хотя бы одну услугу.", reply_markup=build_services_sel_keyboard(sel))
            return
        await state.update_data(new_svcs=list(sel))
        await state.set_state(AdminState.adding_schedule_type)
        await msg.answer("Выберите график:", reply_markup=schedule_type_keyboard)
        return
    for s in [sv["name"] for sv in get_active_services()]:
        if msg.text in (f"✅ {s}", f"☐ {s}"):
            sel.discard(s) if s in sel else sel.add(s)
            await state.update_data(new_svcs=list(sel))
            await msg.answer("Выберите услуги:", reply_markup=build_services_sel_keyboard(sel))
            return
    await msg.answer("Нажмите на услугу или «Готово».", reply_markup=build_services_sel_keyboard(sel))

@dp.message(AdminState.adding_schedule_type)
async def add_sched_type(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        data = await state.get_data()
        await state.set_state(AdminState.adding_services)
        await msg.answer("Выберите услуги:", reply_markup=build_services_sel_keyboard(set(data.get("new_svcs",[]))))
        return
    if msg.text == "Каждый день":
        await _finish_add_master(msg, state, "all", None, None, None)
        return
    if msg.text in ("2/2", "5/2"):
        w, o = map(int, msg.text.split("/"))
        await state.update_data(sw=w, so=o)
        await state.set_state(AdminState.adding_sched_start)
        await msg.answer("Введите дату начала (ДД.ММ.ГГГГ):", reply_markup=back_keyboard)
        return
    if msg.text == "Другой (X/Y)":
        await state.set_state(AdminState.adding_custom_sched)
        await msg.answer("Введите паттерн X/Y (например 3/1):", reply_markup=back_keyboard)
        return
    await msg.answer("Выберите кнопкой.", reply_markup=schedule_type_keyboard)

@dp.message(AdminState.adding_custom_sched)
async def add_custom_sched(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.adding_schedule_type)
        await msg.answer("Выберите график:", reply_markup=schedule_type_keyboard)
        return
    p = _parse_xy(msg.text)
    if not p:
        await msg.answer("Формат: X/Y, например 3/1")
        return
    w, o = p
    await state.update_data(sw=w, so=o)
    await state.set_state(AdminState.adding_sched_start)
    await msg.answer("Введите дату начала (ДД.ММ.ГГГГ):", reply_markup=back_keyboard)

@dp.message(AdminState.adding_sched_start)
async def add_sched_start(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.adding_schedule_type)
        await msg.answer("Выберите график:", reply_markup=schedule_type_keyboard)
        return
    d = parse_date_flexible(msg.text.strip())
    if not d:
        await msg.answer("Введите дату ДД.ММ.ГГГГ")
        return
    data = await state.get_data()
    await _finish_add_master(msg, state, f"{data['sw']}/{data['so']}", data["sw"], data["so"], d.strftime("%d.%m.%Y"))

async def _finish_add_master(msg, state, stype, w, o, sd):
    data = await state.get_data()
    name = data["new_name"]
    svcs = data.get("new_svcs", [])
    mid  = add_master(name)
    set_master_services(mid, svcs)
    set_master_schedule(mid, stype, w, o, sd)
    sg = "каждый день" if stype == "all" else f"{stype} с {sd}"
    await go_masters(msg, state)
    await msg.answer(f"✅ Мастер {name} добавлен.\nУслуги: {', '.join(svcs)}\nГрафик: {sg}")


# ── Смотреть оценку ──

@dp.message(AdminState.master_menu, F.text == "⭐Смотреть оценку")
async def ratings_start(msg: Message, state: FSMContext):
    ms = get_all_masters()
    if not ms:
        await msg.answer("Мастеров нет.", reply_markup=admin_masters_keyboard)
        return
    await state.set_state(AdminState.viewing_ratings_select)
    await msg.answer("Выберите мастера:", reply_markup=build_masters_kb(ms))

@dp.message(AdminState.viewing_ratings_select)
async def ratings_select(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await go_masters(msg, state)
        return
    ms  = get_all_masters()
    sel = next((m for m in ms if m["name"] == msg.text), None)
    if not sel:
        await msg.answer("Выберите кнопкой.", reply_markup=build_masters_kb(ms))
        return

    master  = get_master_by_id(sel["id"])
    reviews = get_master_reviews(sel["id"])
    avg     = get_master_avg_rating(sel["id"])

    svcs    = ", ".join(master["services"]) if master["services"] else "—"
    avg_str = f"{avg}/5" if avg is not None else "нет отзывов"

    header = (
        f"<b>{he(master['name'])}</b>\n"
        f"Услуги: {he(svcs)}\n"
        f"Средняя оценка: {avg_str} (на основе {len(reviews)} отзыв{'а' if 2 <= len(reviews) % 10 <= 4 and len(reviews) % 100 not in range(11,15) else 'ов' if len(reviews) % 10 in (0,5,6,7,8,9) or len(reviews) % 100 in range(11,15) else 'а'})"
    )

    if not reviews:
        await go_masters(msg, state)
        await msg.answer(header, parse_mode="HTML", reply_markup=admin_masters_keyboard)
        return

    review_lines = []
    for r in reviews:
        stars   = "⭐" * r["rating"]
        comment = f"\n      {he(r['comment'])}" if r["comment"] else ""
        review_lines.append(
            f"  {stars} {r['rating']}/5 | {he(r['client_name'])} | {he(r['service'])}\n"
            f"  📅 {r['created_at']}{comment}"
        )

    reviews_block = f"<blockquote expandable>Отзывы ({len(reviews)}):\n\n" + "\n\n".join(review_lines) + "</blockquote>"
    await go_masters(msg, state)
    await msg.answer(f"{header}\n\n{reviews_block}", parse_mode="HTML", reply_markup=admin_masters_keyboard)


# ── Деактивировать ──

@dp.message(AdminState.master_menu, F.text == "🔴Удалить")
async def deact_start(msg: Message, state: FSMContext):
    ms = get_active_masters()
    if not ms:
        await msg.answer("Нет активных мастеров.", reply_markup=admin_masters_keyboard)
        return
    await state.set_state(AdminState.deactivating_select)
    await msg.answer("Выберите мастера:", reply_markup=build_masters_kb(ms))

@dp.message(AdminState.deactivating_select)
async def deact_select(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await go_masters(msg, state)
        return
    ms  = get_all_masters()
    sel = next((m for m in ms if m["name"]==msg.text), None)
    if not sel:
        await msg.answer("Выберите кнопкой.", reply_markup=build_masters_kb(get_active_masters()))
        return
    await state.update_data(deact_id=sel["id"], deact_name=sel["name"])
    await state.set_state(AdminState.deactivating_confirm)
    await msg.answer(f"Удалить мастера {sel['name']}?", reply_markup=yes_no_keyboard)

@dp.message(AdminState.deactivating_confirm)
async def deact_confirm(msg: Message, state: FSMContext):
    data = await state.get_data()
    if msg.text == "Да":
        deactivate_master(data["deact_id"])
        await go_masters(msg, state)
        await msg.answer(f"Мастер {data['deact_name']} удалён.")
    elif msg.text == "Нет":
        await go_masters(msg, state)
    else:
        await msg.answer("Да или Нет?", reply_markup=yes_no_keyboard)


# ── Редактировать ──

@dp.message(AdminState.master_menu, F.text == "🟡Редактировать")
async def edit_start(msg: Message, state: FSMContext):
    ms = get_active_masters()
    if not ms:
        await msg.answer("Нет мастеров.", reply_markup=admin_masters_keyboard)
        return
    await state.set_state(AdminState.editing_select)
    await msg.answer("Выберите мастера:", reply_markup=build_masters_kb(ms))

@dp.message(AdminState.editing_select)
async def edit_select(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await go_masters(msg, state)
        return
    ms  = get_all_masters()
    sel = next((m for m in ms if m["name"]==msg.text), None)
    if not sel:
        await msg.answer("Выберите кнопкой.", reply_markup=build_masters_kb(get_active_masters()))
        return
    await state.update_data(edit_id=sel["id"])
    await state.set_state(AdminState.editing_menu)
    await msg.answer(format_master_info(get_master_by_id(sel["id"]))+"\n\nЧто редактируем?",
                     reply_markup=editing_master_menu_kb)

@dp.message(AdminState.editing_menu)
async def edit_menu(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await go_masters(msg, state)
        return
    if msg.text == "Редактировать расписание":
        await state.set_state(AdminState.editing_sched_menu)
        await msg.answer("Что изменить?", reply_markup=editing_schedule_menu_kb)
        return
    if msg.text == "Редактировать услуги":
        await state.set_state(AdminState.editing_svcs_menu)
        await msg.answer("Что сделать?", reply_markup=editing_services_menu_kb)
        return
    await msg.answer("Выберите кнопкой.", reply_markup=editing_master_menu_kb)

# edit schedule menu
@dp.message(AdminState.editing_sched_menu)
async def edit_sched_menu(msg: Message, state: FSMContext):
    data = await state.get_data()
    mid  = data["edit_id"]
    if msg.text == "Назад":
        await state.set_state(AdminState.editing_menu)
        await msg.answer(format_master_info(get_master_by_id(mid))+"\n\nЧто редактируем?", reply_markup=editing_master_menu_kb)
        return
    if msg.text == "Другой режим работы":
        await state.set_state(AdminState.editing_sched_type)
        await msg.answer("Новый тип графика:", reply_markup=schedule_type_keyboard)
        return
    if msg.text == "Отпуск":
        await state.set_state(AdminState.editing_vacation)
        await msg.answer("Введите период отпуска ДД.ММ.ГГГГ - ДД.ММ.ГГГГ\nНапример: 01.06.2025 - 14.06.2025", reply_markup=back_keyboard)
        return
    if msg.text == "Выходной":
        await state.set_state(AdminState.editing_day_off)
        await msg.answer("Введите дату выходного (ДД.ММ.ГГГГ):", reply_markup=back_keyboard)
        return
    await msg.answer("Выберите кнопкой.", reply_markup=editing_schedule_menu_kb)

@dp.message(AdminState.editing_sched_type)
async def edit_sched_type(msg: Message, state: FSMContext):
    data = await state.get_data()
    mid  = data["edit_id"]
    if msg.text == "Назад":
        await state.set_state(AdminState.editing_sched_menu)
        await msg.answer("Что изменить?", reply_markup=editing_schedule_menu_kb)
        return
    if msg.text == "Каждый день":
        set_master_schedule(mid, "all")
        await state.set_state(AdminState.editing_menu)
        await msg.answer(f"График обновлён.\n\n{format_master_info(get_master_by_id(mid))}", reply_markup=editing_master_menu_kb)
        return
    if msg.text in ("2/2", "5/2"):
        w, o = map(int, msg.text.split("/"))
        await state.update_data(sw=w, so=o)
        await state.set_state(AdminState.editing_sched_start)
        await msg.answer("Дата начала нового графика (ДД.ММ.ГГГГ):", reply_markup=back_keyboard)
        return
    if msg.text == "Другой (X/Y)":
        await state.set_state(AdminState.editing_custom_sched)
        await msg.answer("Паттерн X/Y:", reply_markup=back_keyboard)
        return
    await msg.answer("Выберите кнопкой.", reply_markup=schedule_type_keyboard)

@dp.message(AdminState.editing_custom_sched)
async def edit_custom_sched(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.editing_sched_type)
        await msg.answer("Новый тип графика:", reply_markup=schedule_type_keyboard)
        return
    p = _parse_xy(msg.text)
    if not p:
        await msg.answer("Формат X/Y, например 3/1")
        return
    await state.update_data(sw=p[0], so=p[1])
    await state.set_state(AdminState.editing_sched_start)
    await msg.answer("Дата начала (ДД.ММ.ГГГГ):", reply_markup=back_keyboard)

@dp.message(AdminState.editing_sched_start)
async def edit_sched_start(msg: Message, state: FSMContext):
    data = await state.get_data()
    mid  = data["edit_id"]
    if msg.text == "Назад":
        await state.set_state(AdminState.editing_sched_type)
        await msg.answer("Новый тип графика:", reply_markup=schedule_type_keyboard)
        return
    d = parse_date_flexible(msg.text.strip())
    if not d:
        await msg.answer("Введите ДД.ММ.ГГГГ")
        return
    w, o = data["sw"], data["so"]
    set_master_schedule(mid, f"{w}/{o}", w, o, d.strftime("%d.%m.%Y"))
    await state.set_state(AdminState.editing_menu)
    await msg.answer(f"График обновлён.\n\n{format_master_info(get_master_by_id(mid))}", reply_markup=editing_master_menu_kb)

@dp.message(AdminState.editing_vacation)
async def edit_vacation(msg: Message, state: FSMContext):
    data = await state.get_data()
    mid  = data["edit_id"]
    if msg.text == "Назад":
        await state.set_state(AdminState.editing_sched_menu)
        await msg.answer("Что изменить?", reply_markup=editing_schedule_menu_kb)
        return
    r = parse_date_range(msg.text)
    if not r:
        await msg.answer("Формат: ДД.ММ.ГГГГ - ДД.ММ.ГГГГ\nНапример: 01.06.2025 - 14.06.2025")
        return
    s, e = r
    add_master_vacation(mid, s, e)
    days = (e-s).days+1
    await state.set_state(AdminState.editing_menu)
    await msg.answer(f"Отпуск сохранён: {s.strftime('%d.%m.%Y')} — {e.strftime('%d.%m.%Y')} ({days} дн.)", reply_markup=editing_master_menu_kb)

@dp.message(AdminState.editing_day_off)
async def edit_day_off(msg: Message, state: FSMContext):
    data = await state.get_data()
    mid  = data["edit_id"]
    if msg.text == "Назад":
        await state.set_state(AdminState.editing_sched_menu)
        await msg.answer("Что изменить?", reply_markup=editing_schedule_menu_kb)
        return
    d = parse_date_flexible(msg.text.strip())
    if not d:
        await msg.answer("Введите ДД.ММ.ГГГГ")
        return
    add_master_day_off(mid, d.strftime("%d.%m.%Y"))
    await state.set_state(AdminState.editing_menu)
    m = get_master_by_id(mid)
    await msg.answer(f"Выходной {d.strftime('%d.%m.%Y')} для {m['name']} сохранён.", reply_markup=editing_master_menu_kb)

# edit services
@dp.message(AdminState.editing_svcs_menu)
async def edit_svcs_menu(msg: Message, state: FSMContext):
    data = await state.get_data()
    mid  = data["edit_id"]
    master = get_master_by_id(mid)
    if msg.text == "Назад":
        await state.set_state(AdminState.editing_menu)
        await msg.answer(format_master_info(master)+"\n\nЧто редактируем?", reply_markup=editing_master_menu_kb)
        return
    if msg.text == "Добавить услугу":
        missing = [s for s in SERVICES if s not in master["services"]]
        if not missing:
            await msg.answer("У мастера уже все услуги.", reply_markup=editing_services_menu_kb)
            return
        await state.set_state(AdminState.adding_service)
        rows = [[KeyboardButton(text=s)] for s in missing]+[[KeyboardButton(text="Назад")]]
        await msg.answer("Выберите услугу для добавления:", reply_markup=ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True))
        return
    if msg.text == "Убрать услугу":
        if not master["services"]:
            await msg.answer("У мастера нет услуг.", reply_markup=editing_services_menu_kb)
            return
        await state.set_state(AdminState.removing_svc_select)
        rows = [[KeyboardButton(text=s)] for s in master["services"]]+[[KeyboardButton(text="Назад")]]
        await msg.answer("Выберите услугу для удаления:", reply_markup=ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True))
        return
    await msg.answer("Выберите кнопкой.", reply_markup=editing_services_menu_kb)

@dp.message(AdminState.adding_service)
async def adding_service_h(msg: Message, state: FSMContext):
    data = await state.get_data()
    mid  = data["edit_id"]
    if msg.text == "Назад":
        await state.set_state(AdminState.editing_svcs_menu)
        await msg.answer("Что сделать?", reply_markup=editing_services_menu_kb)
        return
    if msg.text not in [sv["name"] for sv in get_active_services()]:
        await msg.answer("Выберите кнопкой.")
        return
    add_service_to_master(mid, msg.text)
    await state.set_state(AdminState.editing_svcs_menu)
    await msg.answer(f"Услуга «{msg.text}» добавлена.", reply_markup=editing_services_menu_kb)

@dp.message(AdminState.removing_svc_select)
async def removing_svc_select_h(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.editing_svcs_menu)
        await msg.answer("Что сделать?", reply_markup=editing_services_menu_kb)
        return
    if msg.text not in [sv["name"] for sv in get_active_services()]:
        await msg.answer("Выберите кнопкой.")
        return
    await state.update_data(rm_svc=msg.text)
    await state.set_state(AdminState.removing_svc_confirm)
    data = await state.get_data()
    m = get_master_by_id(data["edit_id"])
    await msg.answer(f"Убрать «{msg.text}» у мастера {m['name']}?", reply_markup=yes_no_keyboard)

@dp.message(AdminState.removing_svc_confirm)
async def removing_svc_confirm_h(msg: Message, state: FSMContext):
    data = await state.get_data()
    if msg.text == "Да":
        remove_service_from_master(data["edit_id"], data["rm_svc"])
        await state.set_state(AdminState.editing_svcs_menu)
        await msg.answer(f"Услуга «{data['rm_svc']}» убрана.", reply_markup=editing_services_menu_kb)
    elif msg.text == "Нет":
        await state.set_state(AdminState.editing_svcs_menu)
        await msg.answer("Отменено.", reply_markup=editing_services_menu_kb)
    else:
        await msg.answer("Да или Нет?", reply_markup=yes_no_keyboard)


# ── Расписание салона ──

@dp.message(AdminState.salon_menu, F.text == "Назад")
async def salon_back(msg: Message, state: FSMContext):
    await go_home(msg, state)

@dp.message(AdminState.salon_menu, F.text == "Отменить изменение расписания")
async def salon_cancel_start(msg: Message, state: FSMContext):
    overrides = get_salon_overrides()
    if not overrides:
        await msg.answer("Нет внесённых изменений расписания.", reply_markup=admin_salon_keyboard)
        return
    await state.set_state(AdminState.salon_cancel_select)
    rows = [[KeyboardButton(text=o["desc"])] for o in overrides]
    rows.append([KeyboardButton(text="Назад")])
    await msg.answer(
        "Выберите изменение для отмены:",
        reply_markup=ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True),
    )

@dp.message(AdminState.salon_cancel_select)
async def salon_cancel_select_h(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.salon_menu)
        await msg.answer("Расписание салона:", reply_markup=admin_salon_keyboard)
        return
    overrides = get_salon_overrides()
    sel = next((o for o in overrides if o["desc"] == msg.text), None)
    if not sel:
        rows = [[KeyboardButton(text=o["desc"])] for o in overrides]
        rows.append([KeyboardButton(text="Назад")])
        await msg.answer("Выберите кнопкой.", reply_markup=ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True))
        return
    await state.update_data(cancel_ovr_date=sel["date"], cancel_ovr_desc=sel["desc"])
    await state.set_state(AdminState.salon_cancel_confirm)
    await msg.answer(f"Отменить изменение: {sel['desc']}?", reply_markup=yes_no_keyboard)

@dp.message(AdminState.salon_cancel_confirm)
async def salon_cancel_confirm_h(msg: Message, state: FSMContext):
    data = await state.get_data()
    if msg.text == "Да":
        remove_salon_override(data["cancel_ovr_date"])
        await state.set_state(AdminState.salon_menu)
        await msg.answer(
            f"Изменение для {data['cancel_ovr_date']} отменено. "
            "День вернулся к стандартному расписанию.",
            reply_markup=admin_salon_keyboard,
        )
    elif msg.text == "Нет":
        await state.set_state(AdminState.salon_menu)
        await msg.answer("Отменено.", reply_markup=admin_salon_keyboard)
    else:
        await msg.answer("Нажмите «Да» или «Нет».", reply_markup=yes_no_keyboard)

@dp.message(AdminState.salon_menu, F.text == "Выходной день")
async def salon_day_off_start(msg: Message, state: FSMContext):
    await state.set_state(AdminState.salon_day_off)
    await msg.answer("Дата выходного (ДД.ММ.ГГГГ):", reply_markup=back_keyboard)

@dp.message(AdminState.salon_menu, F.text == "Изменить часы работы")
async def salon_hours_start(msg: Message, state: FSMContext):
    await state.set_state(AdminState.salon_hours_date)
    await msg.answer("Введите дату (ДД.ММ.ГГГГ):", reply_markup=back_keyboard)

@dp.message(AdminState.salon_day_off)
async def salon_day_off_h(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.salon_menu)
        await msg.answer("Расписание салона:", reply_markup=admin_salon_keyboard)
        return
    d = parse_date_flexible(msg.text.strip())
    if not d:
        await msg.answer("Введите ДД.ММ.ГГГГ")
        return
    await state.update_data(sal_off_date=d.strftime("%d.%m.%Y"))
    await state.set_state(AdminState.salon_day_off_confirm)
    await msg.answer(
        f"Внести выходной день {d.strftime('%d.%m.%Y')} для всего салона?",
        reply_markup=yes_no_keyboard,
    )

@dp.message(AdminState.salon_day_off_confirm)
async def salon_day_off_confirm_h(msg: Message, state: FSMContext):
    data = await state.get_data()
    if msg.text == "Да":
        set_salon_day_off(data["sal_off_date"])
        await state.set_state(AdminState.salon_menu)
        await msg.answer(f"Выходной {data['sal_off_date']} сохранён.", reply_markup=admin_salon_keyboard)
    elif msg.text == "Нет":
        await state.set_state(AdminState.salon_menu)
        await msg.answer("Отменено.", reply_markup=admin_salon_keyboard)
    else:
        await msg.answer("Нажмите «Да» или «Нет».", reply_markup=yes_no_keyboard)

@dp.message(AdminState.salon_hours_date)
async def salon_hours_date_h(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.salon_menu)
        await msg.answer("Расписание салона:", reply_markup=admin_salon_keyboard)
        return
    d = parse_date_flexible(msg.text.strip())
    if not d:
        await msg.answer("Введите ДД.ММ.ГГГГ")
        return
    await state.update_data(sal_date=d.strftime("%d.%m.%Y"))
    await state.set_state(AdminState.salon_hours_val)
    await msg.answer("Часы работы ЧЧ:ММ-ЧЧ:ММ (например 11:00-18:00):", reply_markup=back_keyboard)

@dp.message(AdminState.salon_hours_val)
async def salon_hours_val_h(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.salon_hours_date)
        await msg.answer("Введите дату:", reply_markup=back_keyboard)
        return
    m = re.fullmatch(r"(\d{2}:\d{2})-(\d{2}:\d{2})", msg.text.strip())
    if not m:
        await msg.answer("Формат ЧЧ:ММ-ЧЧ:ММ")
        return
    ot, ct = m.group(1), m.group(2)
    if parse_time(ot) >= parse_time(ct):
        await msg.answer("Открытие должно быть раньше закрытия.")
        return
    data = await state.get_data()
    await state.update_data(sal_ot=ot, sal_ct=ct)
    await state.set_state(AdminState.salon_hours_confirm)
    await msg.answer(
        f"Изменить часы работы {data['sal_date']} на {ot}–{ct}?",
        reply_markup=yes_no_keyboard,
    )

@dp.message(AdminState.salon_hours_confirm)
async def salon_hours_confirm_h(msg: Message, state: FSMContext):
    data = await state.get_data()
    if msg.text == "Да":
        set_salon_hours(data["sal_date"], data["sal_ot"], data["sal_ct"])
        await state.set_state(AdminState.salon_menu)
        await msg.answer(f"Часы {data['sal_date']}: {data['sal_ot']}–{data['sal_ct']} сохранены.", reply_markup=admin_salon_keyboard)
    elif msg.text == "Нет":
        await state.set_state(AdminState.salon_menu)
        await msg.answer("Отменено.", reply_markup=admin_salon_keyboard)
    else:
        await msg.answer("Нажмите «Да» или «Нет».", reply_markup=yes_no_keyboard)


# ─── Review Handlers ──────────────────────────────────────────────────────────

@dp.message(ReviewState.rating_pending)
async def review_rating_handler(msg: Message, state: FSMContext):
    if msg.text not in ("5", "4", "3", "2", "1"):
        await msg.answer("Нажмите одну из кнопок с оценкой.", reply_markup=rating_keyboard)
        return
    await state.update_data(review_rating=msg.text)
    await state.set_state(ReviewState.comment_pending)
    await msg.answer(
        "Вы можете оставить письменный отзыв — просто напишите его.\n"
        "Или отправьте оценку без комментария.",
        reply_markup=review_comment_keyboard,
    )


@dp.message(ReviewState.comment_pending)
async def review_comment_handler(msg: Message, state: FSMContext):
    data   = await state.get_data()
    rating = data.get("review_rating", "?")
    name   = data.get("review_name", "Клиент")
    svc    = data.get("review_service", "")
    master = data.get("review_master", "")

    if msg.text == "Назад":
        await state.set_state(ReviewState.rating_pending)
        await msg.answer("Выберите оценку:", reply_markup=rating_keyboard)
        return

    if msg.text == "Отправить оценку без комментария":
        comment      = None
        comment_line = "Без комментария"
    else:
        comment      = msg.text.strip()
        comment_line = f"Комментарий: {comment}"

    master_id = data.get("review_master_id")
    if master_id:
        save_review(master_id, name, svc, int(rating), comment)

    await state.clear()
    await msg.answer("Спасибо за ваш отзыв!", reply_markup=main_keyboard)
    await notify_manager(
        msg.bot,
        f"⭐ Новый отзыв\n\n"
        f"Клиент: {name}\n"
        f"Услуга: {svc}\n"
        f"Мастер: {master}\n"
        f"Оценка: {rating}/5\n"
        f"{comment_line}",
    )


# ─── Admin: Услуги ────────────────────────────────────────────────────────────

@dp.message(AdminState.admin_home, F.text == "💅Услуги")
async def admin_svc_section(msg: Message, state: FSMContext):
    await state.set_state(AdminState.svc_menu)
    await msg.answer("Управление услугами:", reply_markup=svc_menu_keyboard)

@dp.message(AdminState.svc_menu, F.text == "Назад")
async def svc_back(msg: Message, state: FSMContext):
    await go_home(msg, state)

@dp.message(AdminState.svc_menu, F.text == "Добавить услугу")
async def svc_add_start(msg: Message, state: FSMContext):
    await state.set_state(AdminState.svc_adding_name)
    await msg.answer("Введите название новой услуги:", reply_markup=back_keyboard)

@dp.message(AdminState.svc_adding_name)
async def svc_add_name(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.svc_menu)
        await msg.answer("Управление услугами:", reply_markup=svc_menu_keyboard)
        return
    await state.update_data(new_svc_name=msg.text.strip())
    await state.set_state(AdminState.svc_adding_price)
    await msg.answer("Введите цену (только цифры):", reply_markup=back_keyboard)

@dp.message(AdminState.svc_adding_price)
async def svc_add_price(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.svc_adding_name)
        await msg.answer("Введите название услуги:", reply_markup=back_keyboard)
        return
    if not msg.text.isdigit():
        await msg.answer("Введите только цифры, например: 1500")
        return
    await state.update_data(new_svc_price=int(msg.text))
    await state.set_state(AdminState.svc_adding_duration)
    await msg.answer("Длительность услуги:", reply_markup=svc_duration_keyboard)

@dp.message(AdminState.svc_adding_duration)
async def svc_add_duration(msg: Message, state: FSMContext):
    mapping = {"1 час": 1, "2 часа": 2}
    if msg.text not in mapping:
        await msg.answer("Выберите кнопкой.", reply_markup=svc_duration_keyboard)
        return
    await state.update_data(new_svc_dur=mapping[msg.text])
    data = await state.get_data()
    await state.set_state(AdminState.svc_adding_confirm)
    dur_label = "2 часа" if data["new_svc_dur"] == 2 else "1 час"
    await msg.answer(
        f"Сохранить услугу?\n\nНазвание: {data['new_svc_name']}\nЦена: {data['new_svc_price']} ₽\nДлительность: {dur_label}",
        reply_markup=yes_no_keyboard,
    )

@dp.message(AdminState.svc_adding_confirm)
async def svc_add_confirm(msg: Message, state: FSMContext):
    data = await state.get_data()
    if msg.text == "Да":
        add_service_to_catalog(data["new_svc_name"], data["new_svc_price"], data["new_svc_dur"])
        await state.set_state(AdminState.svc_menu)
        await msg.answer(f"Услуга «{data['new_svc_name']}» добавлена.", reply_markup=svc_menu_keyboard)
    elif msg.text == "Нет":
        await state.set_state(AdminState.svc_menu)
        await msg.answer("Отменено.", reply_markup=svc_menu_keyboard)
    else:
        await msg.answer("Да или Нет?", reply_markup=yes_no_keyboard)

@dp.message(AdminState.svc_menu, F.text == "Убрать услугу")
async def svc_remove_start(msg: Message, state: FSMContext):
    svcs = get_active_services()
    if not svcs:
        await msg.answer("Нет активных услуг.", reply_markup=svc_menu_keyboard)
        return
    rows = [[KeyboardButton(text=s["name"])] for s in svcs] + [[KeyboardButton(text="Назад")]]
    await state.set_state(AdminState.svc_remove_select)
    await msg.answer("Выберите услугу для удаления:", reply_markup=ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True))

@dp.message(AdminState.svc_remove_select)
async def svc_remove_select(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.svc_menu)
        await msg.answer("Управление услугами:", reply_markup=svc_menu_keyboard)
        return
    svcs = get_active_services()
    sel = next((s for s in svcs if s["name"] == msg.text), None)
    if not sel:
        await msg.answer("Выберите кнопкой.")
        return
    await state.update_data(rm_svc_id=sel["id"], rm_svc_name=sel["name"])
    await state.set_state(AdminState.svc_remove_confirm)
    await msg.answer(f"Убрать услугу «{sel['name']}»?", reply_markup=yes_no_keyboard)

@dp.message(AdminState.svc_remove_confirm)
async def svc_remove_confirm(msg: Message, state: FSMContext):
    data = await state.get_data()
    if msg.text == "Да":
        deactivate_service(data["rm_svc_id"])
        await state.set_state(AdminState.svc_menu)
        await msg.answer(f"Услуга «{data['rm_svc_name']}» убрана.", reply_markup=svc_menu_keyboard)
    elif msg.text == "Нет":
        await state.set_state(AdminState.svc_menu)
        await msg.answer("Отменено.", reply_markup=svc_menu_keyboard)
    else:
        await msg.answer("Да или Нет?", reply_markup=yes_no_keyboard)

@dp.message(AdminState.svc_menu, F.text == "Править услугу")
async def svc_edit_start(msg: Message, state: FSMContext):
    svcs = get_active_services()
    if not svcs:
        await msg.answer("Нет активных услуг.", reply_markup=svc_menu_keyboard)
        return
    rows = [[KeyboardButton(text=s["name"])] for s in svcs] + [[KeyboardButton(text="Назад")]]
    await state.set_state(AdminState.svc_edit_select)
    await msg.answer("Выберите услугу:", reply_markup=ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True))

@dp.message(AdminState.svc_edit_select)
async def svc_edit_select_h(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.svc_menu)
        await msg.answer("Управление услугами:", reply_markup=svc_menu_keyboard)
        return
    svcs = get_active_services()
    sel = next((s for s in svcs if s["name"] == msg.text), None)
    if not sel:
        await msg.answer("Выберите кнопкой.")
        return
    await state.update_data(edit_svc_id=sel["id"], edit_svc_name=sel["name"], edit_svc_price=sel["price"])
    await state.set_state(AdminState.svc_edit_action)
    await msg.answer(f"Услуга: {sel['name']}\nЦена: {sel['price']} ₽\n\nЧто изменить?", reply_markup=svc_edit_action_keyboard)

@dp.message(AdminState.svc_edit_action)
async def svc_edit_action_h(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.svc_menu)
        await msg.answer("Управление услугами:", reply_markup=svc_menu_keyboard)
        return
    if msg.text == "Изменить название":
        await state.set_state(AdminState.svc_edit_name)
        await msg.answer("Введите новое название:", reply_markup=back_keyboard)
        return
    if msg.text == "Изменить цену":
        await state.set_state(AdminState.svc_edit_price)
        await msg.answer("Введите новую цену (только цифры):", reply_markup=back_keyboard)
        return
    await msg.answer("Выберите кнопкой.", reply_markup=svc_edit_action_keyboard)

@dp.message(AdminState.svc_edit_name)
async def svc_edit_name_h(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.svc_edit_action)
        await msg.answer("Что изменить?", reply_markup=svc_edit_action_keyboard)
        return
    data = await state.get_data()
    await state.update_data(edit_new_name=msg.text.strip())
    await state.set_state(AdminState.svc_edit_confirm)
    await msg.answer(
        f"Переименовать «{data['edit_svc_name']}» → «{msg.text.strip()}»?\nСохранить?",
        reply_markup=yes_no_keyboard,
    )

@dp.message(AdminState.svc_edit_price)
async def svc_edit_price_h(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.svc_edit_action)
        await msg.answer("Что изменить?", reply_markup=svc_edit_action_keyboard)
        return
    if not msg.text.isdigit():
        await msg.answer("Введите только цифры.")
        return
    data = await state.get_data()
    await state.update_data(edit_new_price=int(msg.text))
    await state.set_state(AdminState.svc_edit_confirm)
    await msg.answer(
        f"Изменить цену «{data['edit_svc_name']}»: {data['edit_svc_price']} ₽ → {msg.text} ₽?\nСохранить?",
        reply_markup=yes_no_keyboard,
    )

@dp.message(AdminState.svc_edit_confirm)
async def svc_edit_confirm_h(msg: Message, state: FSMContext):
    data = await state.get_data()
    if msg.text == "Да":
        if "edit_new_name" in data:
            edit_service_name(data["edit_svc_id"], data["edit_new_name"])
            result = f"Название изменено на «{data['edit_new_name']}»."
        else:
            edit_service_price(data["edit_svc_id"], data["edit_new_price"])
            result = f"Цена изменена на {data['edit_new_price']} ₽."
        await state.set_state(AdminState.svc_menu)
        await msg.answer(result, reply_markup=svc_menu_keyboard)
    elif msg.text == "Нет":
        await state.set_state(AdminState.svc_menu)
        await msg.answer("Отменено.", reply_markup=svc_menu_keyboard)
    else:
        await msg.answer("Да или Нет?", reply_markup=yes_no_keyboard)


# ─── Admin: Информация о салоне ───────────────────────────────────────────────

@dp.message(AdminState.admin_home, F.text == "📄О салоне")
async def admin_info_section(msg: Message, state: FSMContext):
    await state.set_state(AdminState.info_select)
    await msg.answer("Выберите раздел для редактирования:", reply_markup=info_sections_keyboard)

@dp.message(AdminState.info_select)
async def info_select_h(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await go_home(msg, state)
        return
    await msg.answer("Выберите раздел кнопкой.", reply_markup=info_sections_keyboard)

@dp.message(AdminState.info_text)
async def info_text_h(msg: Message, state: FSMContext):
    data = await state.get_data()
    if msg.text == "Не менять":
        await state.update_data(info_new_text=None)
    else:
        await state.update_data(info_new_text=msg.text.strip())
    await state.set_state(AdminState.info_photo)
    await msg.answer(
        "Прикрепите новое фото для раздела.\nИли нажмите «Не менять» чтобы оставить текущее.",
        reply_markup=keep_keyboard,
    )

@dp.message(AdminState.info_photo)
async def info_photo_h(msg: Message, state: FSMContext):
    data = await state.get_data()
    if msg.photo:
        await state.update_data(info_new_photo=msg.photo[-1].file_id)
    elif msg.text == "Не менять":
        await state.update_data(info_new_photo=None)
    else:
        await msg.answer("Отправьте фото или нажмите «Не менять».", reply_markup=keep_keyboard)
        return

    data = await state.get_data()
    new_text  = data.get("info_new_text")
    new_photo = data.get("info_new_photo")
    changes = []
    if new_text:  changes.append("текст")
    if new_photo: changes.append("фото")
    summary = " и ".join(changes) if changes else "ничего"
    await state.set_state(AdminState.info_confirm)
    await msg.answer(
        f"Раздел «{data['info_section_label']}».\nИзменений: {summary}.\nСохранить?",
        reply_markup=yes_no_keyboard,
    )

@dp.message(AdminState.info_confirm)
async def info_confirm_h(msg: Message, state: FSMContext):
    data = await state.get_data()
    if msg.text == "Да":
        page     = get_info_page(data["info_section_key"])
        new_text = data.get("info_new_text") or page["text"]
        new_photo= data.get("info_new_photo") or page["photo_file_id"]
        save_info_page(data["info_section_key"], new_text, new_photo)
        log_action(msg.from_user.id, msg.from_user.full_name, "info_edit", data["info_section_label"])
        await go_home(msg, state)
        await msg.answer(f"Раздел «{data['info_section_label']}» обновлён.", reply_markup=admin_kb(msg.from_user.id))
    elif msg.text == "Нет":
        await go_home(msg, state)
        await msg.answer("Отменено.", reply_markup=admin_kb(msg.from_user.id))
    else:
        await msg.answer("Да или Нет?", reply_markup=yes_no_keyboard)


# ─── Admin: Блокировка клиентов ───────────────────────────────────────────────

@dp.message(AdminState.admin_home, F.text == "⛔Блок пользователя")
async def admin_block_section(msg: Message, state: FSMContext):
    await state.set_state(AdminState.block_menu)
    await msg.answer("Управление блокировками:", reply_markup=block_menu_keyboard)

@dp.message(AdminState.block_menu)
async def admin_block_menu_h(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await go_home(msg, state)
        return
    if msg.text == "Заблокировать клиента":
        await state.set_state(AdminState.block_input)
        await msg.answer(
            "Введите номер телефона (8XXXXXXXXXX или +7XXXXXXXXXX)\nили @username клиента:",
            reply_markup=back_keyboard,
        )
        return
    if msg.text == "Заблокированные клиенты":
        blocked = get_blocked_clients()
        if not blocked:
            await msg.answer("Нет заблокированных клиентов.", reply_markup=block_menu_keyboard)
            return
        await state.set_state(AdminState.blocked_list)
        await state.update_data(blocked_clients=blocked)
        rows = [
            [KeyboardButton(text=f"{i}. {b['name'] or '—'} | {b['phone'] or b['username'] or '—'}")]
            for i, b in enumerate(blocked, 1)
        ]
        rows.append([KeyboardButton(text="Назад")])
        lines = ["Заблокированные клиенты:\n"]
        for i, b in enumerate(blocked, 1):
            ident = b["phone"] or (f"@{b['username']}" if b["username"] else "—")
            lines.append(f"{i}. {b['name'] or '—'} | {ident} | с {b['blocked_at']}")
        await msg.answer(
            "\n".join(lines),
            reply_markup=ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True),
        )
        return
    await msg.answer("Выберите кнопкой.", reply_markup=block_menu_keyboard)

@dp.message(AdminState.block_input)
async def admin_block_input_h(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.block_menu)
        await msg.answer("Управление блокировками:", reply_markup=block_menu_keyboard)
        return
    identity = get_client_identity(msg.text.strip())
    if identity:
        await state.update_data(
            block_user_id=identity["user_id"],
            block_username=identity["username"],
            block_phone=identity["phone"],
            block_name=identity["name"],
        )
        phone_str   = identity["phone"] or "—"
        uname_str   = f"@{identity['username']}" if identity["username"] else "—"
        await msg.answer(
            f"Заблокировать клиента?\n\n"
            f"Имя: {identity['name']}\n"
            f"Телефон: {phone_str}\n"
            f"Telegram: {uname_str}",
            reply_markup=yes_no_keyboard,
        )
    else:
        q = msg.text.strip()
        uname = q[1:] if q.startswith("@") else (None if is_valid_phone(q) else q)
        phone = normalize_phone(q) if is_valid_phone(q) else None
        await state.update_data(
            block_user_id=None,
            block_username=uname,
            block_phone=phone,
            block_name=None,
        )
        ident_str = f"@{uname}" if uname else phone
        await msg.answer(
            f"Клиент не найден в истории записей.\n"
            f"Всё равно заблокировать {ident_str}?",
            reply_markup=yes_no_keyboard,
        )
    await state.set_state(AdminState.block_confirm)

@dp.message(AdminState.block_confirm)
async def admin_block_confirm_h(msg: Message, state: FSMContext):
    if msg.text == "Нет":
        await state.set_state(AdminState.block_menu)
        await msg.answer("Отменено.", reply_markup=block_menu_keyboard)
        return
    if msg.text != "Да":
        await msg.answer("Нажмите «Да» или «Нет».", reply_markup=yes_no_keyboard)
        return
    data = await state.get_data()
    block_client(
        data.get("block_user_id"),
        data.get("block_username"),
        data.get("block_phone"),
        data.get("block_name"),
    )
    await state.set_state(AdminState.block_menu)
    name_str  = data.get("block_name") or data.get("block_phone") or data.get("block_username") or "клиент"
    phone_str = data.get("block_phone") or data.get("block_username") or ""
    log_action(msg.from_user.id, msg.from_user.full_name, "block", f"{name_str} ({phone_str})")
    await msg.answer(f"✅ {name_str} заблокирован. Запись через бот недоступна.", reply_markup=block_menu_keyboard)

@dp.message(AdminState.blocked_list)
async def admin_blocked_list_h(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.block_menu)
        await msg.answer("Управление блокировками:", reply_markup=block_menu_keyboard)
        return
    data    = await state.get_data()
    blocked = data.get("blocked_clients", [])
    idx     = None
    t       = msg.text or ""
    if t and t[0].isdigit():
        dp_ = t.find(".")
        if dp_ != -1:
            try:
                idx = int(t[:dp_]) - 1
            except ValueError:
                pass
    if idx is None or not (0 <= idx < len(blocked)):
        await msg.answer("Выберите кнопкой.")
        return
    sel = blocked[idx]
    await state.update_data(unblock_id=sel["id"], unblock_name=sel["name"])
    name_str  = sel["name"] or "—"
    ident_str = sel["phone"] or (f"@{sel['username']}" if sel["username"] else "—")
    await state.set_state(AdminState.unblock_confirm)
    await msg.answer(
        f"Разблокировать клиента?\n\nИмя: {name_str}\n{ident_str}",
        reply_markup=yes_no_keyboard,
    )

@dp.message(AdminState.unblock_confirm)
async def admin_unblock_confirm_h(msg: Message, state: FSMContext):
    if msg.text == "Нет":
        await state.set_state(AdminState.block_menu)
        await msg.answer("Отменено.", reply_markup=block_menu_keyboard)
        return
    if msg.text != "Да":
        await msg.answer("Нажмите «Да» или «Нет».", reply_markup=yes_no_keyboard)
        return
    data = await state.get_data()
    unblock_client(data["unblock_id"])
    name_str = data.get("unblock_name") or "Клиент"
    log_action(msg.from_user.id, msg.from_user.full_name, "unblock", name_str)
    await state.set_state(AdminState.block_menu)
    await msg.answer(f"✅ {name_str} разблокирован.", reply_markup=block_menu_keyboard)


# ─── Admin: Поиск по клиенту ──────────────────────────────────────────────────

def _build_client_history_html(result: dict) -> str:
    name   = he(result["name"])
    phone  = he(normalize_phone(result["phone"]))
    uname  = f"@{he(result['username'])}" if result["username"] else "—"
    active_count = len(result["active"])
    total        = active_count + len(result["cancelled"])
    badge        = he(loyalty_badge(active_count))

    lines = [
        f"<b>Клиент: {name}{badge}</b>",
        f"Телефон: {phone}",
        f"Telegram: {uname}",
        f"Всего визитов: {total}",
    ]

    if result["active"]:
        lines.append("\n<b>Записи (активные и прошедшие):</b>")
        for b in sorted(result["active"], key=lambda x: (x["date"], x["time"])):
            dur = " (2ч)" if b["duration"] >= 2 else ""
            lines.append(
                f"  {he(b['date'])} {he(b['time'])} — {he(b['service'])}{dur}\n"
                f"  Мастер: {he(b['master_name'])}"
            )

    if result["cancelled"]:
        reason_map = {
            "client":    "клиент",
            "reminder":  "через напоминание",
            "reschedule": "перезапись",
        }
        lines.append("\n<b>Отменённые записи:</b>")
        for b in sorted(result["cancelled"], key=lambda x: (x["date"], x["time"]), reverse=True):
            dur    = " (2ч)" if b["duration"] >= 2 else ""
            reason = reason_map.get(b["cancel_reason"], he(b["cancel_reason"]))
            lines.append(
                f"  {he(b['date'])} {he(b['time'])} — {he(b['service'])}{dur}\n"
                f"  Мастер: {he(b['master_name'])} | Отменена: {he(b['cancelled_at'])} ({reason})"
            )

    return "\n".join(lines)


@dp.message(AdminState.admin_home, F.text == "🔍Поиск по клиенту")
async def admin_client_search_start(msg: Message, state: FSMContext):
    await state.set_state(AdminState.client_search_input)
    await msg.answer(
        "Введите номер телефона клиента (8XXXXXXXXXX или +7XXXXXXXXXX)\n"
        "или @username:",
        reply_markup=back_keyboard,
    )

@dp.message(AdminState.client_search_input)
async def admin_client_search_handler(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await go_home(msg, state)
        return
    result = get_client_history(msg.text.strip())
    if not result:
        await msg.answer(
            "Клиент не найден. Проверьте номер или @username.",
            reply_markup=back_keyboard,
        )
        return
    html = _build_client_history_html(result)
    await msg.answer(
        f"<blockquote expandable>{html}</blockquote>",
        parse_mode="HTML",
        reply_markup=back_keyboard,
    )


# ─── Admin: Рассылка объявлений ───────────────────────────────────────────────

@dp.message(AdminState.admin_home, F.text == "✏️Создать объявление")
async def broadcast_start(msg: Message, state: FSMContext):
    await state.set_state(AdminState.broadcast_text)
    await msg.answer("Введите текст объявления:", reply_markup=back_keyboard)

@dp.message(AdminState.broadcast_text)
async def broadcast_text_h(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await go_home(msg, state)
        return
    await state.update_data(broadcast_text=msg.text.strip())
    await state.set_state(AdminState.broadcast_media)
    await msg.answer(
        "Прикрепите фото или видео к объявлению.\n"
        "Или нажмите «Без медиафайла».",
        reply_markup=broadcast_media_keyboard,
    )

@dp.message(AdminState.broadcast_media)
async def broadcast_media_h(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.broadcast_text)
        await msg.answer("Введите текст объявления:", reply_markup=back_keyboard)
        return

    if msg.photo:
        await state.update_data(broadcast_media_type="photo", broadcast_media_id=msg.photo[-1].file_id)
    elif msg.video:
        await state.update_data(broadcast_media_type="video", broadcast_media_id=msg.video.file_id)
    elif msg.text == "Без медиафайла":
        await state.update_data(broadcast_media_type=None, broadcast_media_id=None)
    else:
        await msg.answer("Отправьте фото, видео или нажмите «Без медиафайла».",
                         reply_markup=broadcast_media_keyboard)
        return

    with sqlite3.connect(DB_PATH) as conn:
        count = conn.execute("SELECT COUNT(DISTINCT user_id) FROM bookings").fetchone()[0]

    data        = await state.get_data()
    media_label = "с медиафайлом" if data.get("broadcast_media_type") else "без медиафайла"
    await state.set_state(AdminState.broadcast_confirm)
    await msg.answer(
        f"Объявление ({media_label}) будет отправлено {count} пользователям.\n\n"
        f"Текст:\n{data['broadcast_text']}\n\n"
        "Отправить?",
        reply_markup=yes_no_keyboard,
    )

@dp.message(AdminState.broadcast_confirm)
async def broadcast_confirm_h(msg: Message, state: FSMContext):
    if msg.text == "Нет":
        await go_home(msg, state)
        await msg.answer("Объявление отменено.")
        return
    if msg.text != "Да":
        await msg.answer("Нажмите «Да» или «Нет».", reply_markup=yes_no_keyboard)
        return

    data       = await state.get_data()
    text       = data["broadcast_text"]
    media_type = data.get("broadcast_media_type")
    media_id   = data.get("broadcast_media_id")

    with sqlite3.connect(DB_PATH) as conn:
        user_ids = [r[0] for r in conn.execute("SELECT DISTINCT user_id FROM bookings").fetchall()]

    log_action(msg.from_user.id, msg.from_user.full_name, "broadcast", text[:60])
    await go_home(msg, state)
    await msg.answer(f"Начинаю рассылку {len(user_ids)} пользователям…")

    sent, failed = 0, 0
    for uid in user_ids:
        try:
            if media_type == "photo":
                await msg.bot.send_photo(chat_id=uid, photo=media_id, caption=text)
            elif media_type == "video":
                await msg.bot.send_video(chat_id=uid, video=media_id, caption=text)
            else:
                await msg.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await msg.bot.send_message(
        chat_id=msg.from_user.id,
        text=f"Рассылка завершена.\n✅ Доставлено: {sent}\n❌ Ошибок: {failed}",
        reply_markup=admin_kb(msg.from_user.id),
    )


# ─── Admin: Уведомления (только для администратора) ──────────────────────────

@dp.message(AdminState.admin_home, F.text == "Уведомления")
async def admin_notifications_toggle(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    new_val = toggle_notifications()
    status  = "включены ✅" if new_val else "отключены 🔕"
    await msg.answer(f"Уведомления о записях {status}.", reply_markup=admin_super_keyboard)


# ─── Admin: Менеджеры (только для администратора) ─────────────────────────────

@dp.message(AdminState.admin_home, F.text == "Менеджеры")
async def admin_managers_section(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await state.set_state(AdminState.managers_menu)
    await msg.answer("Управление менеджерами:", reply_markup=managers_menu_keyboard)


@dp.message(AdminState.managers_menu)
async def admin_managers_menu_h(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await go_home(msg, state)
        return

    if msg.text == "Добавить менеджера":
        await state.set_state(AdminState.manager_add_input)
        await msg.answer(
            "Введите Telegram ID и имя менеджера через пробел.\n"
            "Пример: <code>123456789 Иван</code>\n\n"
            "Telegram ID можно узнать через @userinfobot.",
            parse_mode="HTML",
        )
        return

    if msg.text == "Список менеджеров":
        managers = get_all_managers()
        if not managers:
            await msg.answer("Менеджеров нет.", reply_markup=managers_menu_keyboard)
            return
        lines = []
        buttons = []
        for i, m in enumerate(managers, 1):
            status = "активен" if m["is_active"] else "деактивирован"
            lines.append(f"{i}. {he(m['name'])} ({status}) [{m['user_id']}]")
            if m["is_active"]:
                buttons.append([KeyboardButton(text=f"{i}. {m['name']}")])
        buttons.append([KeyboardButton(text="Назад")])
        kb = ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)
        await state.update_data(managers_list=managers)
        await state.set_state(AdminState.manager_deactivate_select)
        text_out = "\n".join(lines) + "\n\nВыберите активного менеджера для деактивации:"
        await msg.answer(text_out, reply_markup=kb)
        return

    if msg.text == "Лог действий":
        logs = get_action_log(50)
        if not logs:
            await msg.answer("Лог пуст.", reply_markup=managers_menu_keyboard)
            await state.set_state(AdminState.manager_log_view)
            return
        lines = []
        for entry in logs:
            action_map = {
                "block":     "Заблокировал клиента",
                "unblock":   "Разблокировал клиента",
                "info_edit": "Редактировал инфо-страницу",
                "broadcast": "Отправил объявление",
            }
            action_label = action_map.get(entry["action"], entry["action"])
            detail = f": {he(entry['details'])}" if entry["details"] else ""
            lines.append(f"{he(entry['logged_at'])} — {he(entry['manager_name'])}\n  {action_label}{detail}")
        body = "\n\n".join(lines)
        log_kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Назад")]], resize_keyboard=True)
        await state.set_state(AdminState.manager_log_view)
        await msg.answer(
            f"<blockquote expandable>{body}</blockquote>",
            parse_mode="HTML",
            reply_markup=log_kb,
        )
        return

    await msg.answer("Выберите действие кнопкой.", reply_markup=managers_menu_keyboard)


@dp.message(AdminState.manager_add_input)
async def admin_manager_add_h(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.managers_menu)
        await msg.answer("Управление менеджерами:", reply_markup=managers_menu_keyboard)
        return
    parts = (msg.text or "").strip().split(None, 1)
    if len(parts) < 2 or not parts[0].isdigit():
        await msg.answer(
            "Неверный формат. Введите: <code>TelegramID Имя</code>\nПример: <code>123456789 Иван</code>",
            parse_mode="HTML",
        )
        return
    new_uid  = int(parts[0])
    new_name = parts[1].strip()
    if new_uid == ADMIN_ID:
        await msg.answer("Это ID администратора, добавлять не нужно.")
        return
    existing = [m for m in get_all_managers() if m["user_id"] == new_uid and m["is_active"]]
    if existing:
        await msg.answer(f"Менеджер с ID {new_uid} уже активен.", reply_markup=managers_menu_keyboard)
        await state.set_state(AdminState.managers_menu)
        return
    add_manager(new_uid, new_name)
    await state.set_state(AdminState.managers_menu)
    await msg.answer(f"✅ Менеджер {he(new_name)} ({new_uid}) добавлен.", reply_markup=managers_menu_keyboard)


@dp.message(AdminState.manager_deactivate_select)
async def admin_manager_deact_select_h(msg: Message, state: FSMContext):
    if msg.text == "Назад":
        await state.set_state(AdminState.managers_menu)
        await msg.answer("Управление менеджерами:", reply_markup=managers_menu_keyboard)
        return
    data     = await state.get_data()
    managers = data.get("managers_list", [])
    idx      = None
    t        = msg.text or ""
    if t and t[0].isdigit():
        dp_ = t.find(".")
        if dp_ != -1:
            try:
                idx = int(t[:dp_]) - 1
            except ValueError:
                pass
    if idx is None or not (0 <= idx < len(managers)):
        await msg.answer("Выберите кнопкой.")
        return
    sel = managers[idx]
    if not sel["is_active"]:
        await msg.answer("Этот менеджер уже деактивирован.")
        return
    await state.update_data(deact_manager_id=sel["id"], deact_manager_name=sel["name"])
    await state.set_state(AdminState.manager_deactivate_confirm)
    await msg.answer(
        f"Деактивировать менеджера {he(sel['name'])} (ID {sel['user_id']})?",
        reply_markup=yes_no_keyboard,
    )


@dp.message(AdminState.manager_deactivate_confirm)
async def admin_manager_deact_confirm_h(msg: Message, state: FSMContext):
    if msg.text == "Нет":
        await state.set_state(AdminState.managers_menu)
        await msg.answer("Отменено.", reply_markup=managers_menu_keyboard)
        return
    if msg.text != "Да":
        await msg.answer("Нажмите «Да» или «Нет».", reply_markup=yes_no_keyboard)
        return
    data = await state.get_data()
    deactivate_manager(data["deact_manager_id"])
    await state.set_state(AdminState.managers_menu)
    await msg.answer(f"✅ Менеджер {he(data['deact_manager_name'])} деактивирован.", reply_markup=managers_menu_keyboard)


@dp.message(AdminState.manager_log_view)
async def admin_manager_log_view_h(msg: Message, state: FSMContext):
    await state.set_state(AdminState.managers_menu)
    await msg.answer("Управление менеджерами:", reply_markup=managers_menu_keyboard)


# ─── Fallback ──────────────────────────────────────────────────────────────────

@dp.message()
async def fallback(msg: Message):
    await msg.answer("Выберите действие в меню.", reply_markup=main_keyboard)


# ─── Main ─────────────────────────────────────────────────────────────────────

async def main():
    logging.basicConfig(level=logging.INFO)
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN не найден")
    init_db()
    bot = Bot(token=TOKEN)
    asyncio.create_task(reminder_loop(bot))
    asyncio.create_task(review_loop(bot))
    asyncio.create_task(reengagement_loop(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
