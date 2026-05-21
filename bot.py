import asyncio
import logging
import sqlite3
from os import getenv
from pathlib import Path
from datetime import datetime, date, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    FSInputFile,
)


TOKEN = getenv("BOT_TOKEN")
MANAGER_CHAT_ID = 6430611356

BASE_DIR = Path(__file__).resolve().parent
IMAGES_DIR = BASE_DIR / "images"
DB_PATH = BASE_DIR / "bookings.db"

BANNER_IMAGE = IMAGES_DIR / "banner.jpg"
ABOUT_IMAGE = IMAGES_DIR / "about.jpg"
PRICES_IMAGE = IMAGES_DIR / "prices.jpg"
MASTERS_IMAGE = IMAGES_DIR / "masters.jpg"
CONTACTS_IMAGE = IMAGES_DIR / "contacts.jpg"

storage = MemoryStorage()
dp = Dispatcher(storage=storage)

WORKING_TIMES = ["10:00", "12:00", "14:00", "16:00", "18:00", "20:00"]

BANNED_WORDS = [
    "хуй",
    "хуе",
    "хуё",
    "пизд",
    "еба",
    "ёба",
    "еби",
    "ебл",
    "сука",
    "сучк",
    "бля",
    "бляд",
    "блять",
    "гандон",
    "мудак",
    "мраз",
    "долба",
    "дебил",
    "идиот",
    "урод",
]


class BookingState(StatesGroup):
    choosing_service = State()
    entering_date = State()
    choosing_time = State()
    entering_name = State()
    entering_phone = State()
    confirming = State()


main_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="О салоне"), KeyboardButton(text="Услуги и цены")],
        [KeyboardButton(text="Мастера"), KeyboardButton(text="Записаться")],
        [KeyboardButton(text="Контакты и адрес")],
    ],
    resize_keyboard=True,
)

services_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Маникюр без покрытия")],
        [KeyboardButton(text="Маникюр с гель-лаком")],
        [KeyboardButton(text="Педикюр без покрытия")],
        [KeyboardButton(text="Педикюр с гель-лаком")],
        [KeyboardButton(text="Отмена")],
    ],
    resize_keyboard=True,
)

confirm_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Подтвердить запись")],
        [KeyboardButton(text="Отменить запись")],
    ],
    resize_keyboard=True,
)

reminder_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Подтвердить визит")],
        [KeyboardButton(text="Отменить визит")],
        [KeyboardButton(text="Перезаписаться")],
    ],
    resize_keyboard=True,
)


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                service TEXT NOT NULL,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                name TEXT NOT NULL,
                phone TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reminder_sent INTEGER NOT NULL DEFAULT 0,
                client_confirmed INTEGER NOT NULL DEFAULT 0,
                UNIQUE(date, time)
            )
            """
        )

        existing_columns = [
            row[1]
            for row in cursor.execute("PRAGMA table_info(bookings)").fetchall()
        ]

        if "reminder_sent" not in existing_columns:
            cursor.execute(
                "ALTER TABLE bookings ADD COLUMN reminder_sent INTEGER NOT NULL DEFAULT 0"
            )

        if "client_confirmed" not in existing_columns:
            cursor.execute(
                "ALTER TABLE bookings ADD COLUMN client_confirmed INTEGER NOT NULL DEFAULT 0"
            )

        conn.commit()


def contains_banned_words(text: str) -> bool:
    lowered = text.lower()

    for banned_word in BANNED_WORDS:
        if banned_word in lowered:
            return True

    return False


def is_slot_booked(date_text: str, time_text: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM bookings WHERE date = ? AND time = ?",
            (date_text, time_text),
        )
        return cursor.fetchone() is not None


def save_booking(
    user_id: int,
    username: str | None,
    service: str,
    date_text: str,
    time_text: str,
    name: str,
    phone: str,
) -> bool:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO bookings (
                    user_id, username, service, date, time, name, phone, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    username,
                    service,
                    date_text,
                    time_text,
                    name,
                    phone,
                    datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                ),
            )
            conn.commit()
            return True
    except sqlite3.IntegrityError:
        return False


def delete_booking_by_id(booking_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM bookings WHERE id = ?", (booking_id,))
        conn.commit()


def get_free_times(selected_date_text: str) -> list[str]:
    return [
        time_text
        for time_text in WORKING_TIMES
        if not is_slot_booked(selected_date_text, time_text)
    ]


def get_nearest_booking_for_user(user_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, service, date, time, name, phone
            FROM bookings
            WHERE user_id = ?
            ORDER BY date, time
            """,
            (user_id,),
        )

        rows = cursor.fetchall()

    now = datetime.now()

    for row in rows:
        booking_id, service, date_text, time_text, name, phone = row
        booking_dt = datetime.strptime(
            f"{date_text} {time_text}",
            "%d.%m.%Y %H:%M",
        )

        if booking_dt > now:
            return {
                "id": booking_id,
                "service": service,
                "date": date_text,
                "time": time_text,
                "name": name,
                "phone": phone,
            }

    return None


def mark_booking_confirmed(booking_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE bookings SET client_confirmed = 1 WHERE id = ?",
            (booking_id,),
        )
        conn.commit()


def get_bookings_for_reminder():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, user_id, service, date, time, name
            FROM bookings
            WHERE reminder_sent = 0
            """
        )
        rows = cursor.fetchall()

    now = datetime.now()
    result = []

    for row in rows:
        booking_id, user_id, service, date_text, time_text, name = row

        booking_dt = datetime.strptime(
            f"{date_text} {time_text}",
            "%d.%m.%Y %H:%M",
        )

        time_until_booking = booking_dt - now

        if timedelta(hours=0) < time_until_booking <= timedelta(hours=24):
            result.append(
                {
                    "id": booking_id,
                    "user_id": user_id,
                    "service": service,
                    "date": date_text,
                    "time": time_text,
                    "name": name,
                }
            )

    return result


def mark_reminder_sent(booking_id: int):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE bookings SET reminder_sent = 1 WHERE id = ?",
            (booking_id,),
        )
        conn.commit()


def build_times_keyboard(free_times: list[str]) -> ReplyKeyboardMarkup:
    keyboard = []

    for i in range(0, len(free_times), 2):
        keyboard.append(
            [KeyboardButton(text=time_text) for time_text in free_times[i:i + 2]]
        )

    keyboard.append([KeyboardButton(text="Отмена")])

    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def parse_booking_date(date_text: str) -> date | None:
    try:
        return datetime.strptime(date_text, "%d.%m.%Y").date()
    except ValueError:
        return None


def is_date_available(selected_date: date) -> tuple[bool, str]:
    today = date.today()
    max_date = today + timedelta(days=14)

    if selected_date < today:
        return False, "Нельзя записаться на прошедшую дату."

    if selected_date > max_date:
        return False, "Можно записаться только в пределах ближайших 14 дней."

    return True, ""


async def send_photo_with_text(message: Message, image_path: Path, text: str):
    if image_path.exists():
        photo = FSInputFile(image_path)
        await message.answer_photo(photo=photo, caption=text)
    else:
        await message.answer(text + f"\n\nКартинка не найдена: {image_path.name}")


async def notify_manager(bot: Bot, text: str):
    try:
        await bot.send_message(
            chat_id=MANAGER_CHAT_ID,
            text=text,
        )
    except Exception:
        logging.exception("Не удалось отправить уведомление менеджеру")


async def reminder_loop(bot: Bot):
    while True:
        bookings = get_bookings_for_reminder()

        for booking in bookings:
            try:
                await bot.send_message(
                    chat_id=booking["user_id"],
                    text=(
                        "Напоминание о записи.\n\n"
                        f"Услуга: {booking['service']}\n"
                        f"Дата: {booking['date']}\n"
                        f"Время: {booking['time']}\n\n"
                        "Подтвердите визит, отмените запись или выберите перезапись."
                    ),
                    reply_markup=reminder_keyboard,
                )

                mark_reminder_sent(booking["id"])

            except Exception:
                logging.exception("Не удалось отправить напоминание клиенту")

        await asyncio.sleep(60)


@dp.message(Command("start"))
async def start_handler(message: Message, state: FSMContext):
    await state.clear()

    await send_photo_with_text(
        message,
        BANNER_IMAGE,
        "Добро пожаловать в Nail Studio.\n\n"
        "Я помогу узнать цены, посмотреть мастеров и записаться на услугу.",
    )

    await message.answer("Выберите действие:", reply_markup=main_keyboard)


@dp.message(F.text == "О салоне")
async def about_handler(message: Message):
    await send_photo_with_text(
        message,
        ABOUT_IMAGE,
        "Nail Studio — салон маникюра и педикюра.\n\n"
        "Работаем ежедневно с 10:00 до 21:00.\n"
        "Используем стерильные инструменты, одноразовые расходники "
        "и качественные материалы.",
    )


@dp.message(F.text == "Услуги и цены")
async def prices_handler(message: Message):
    await send_photo_with_text(
        message,
        PRICES_IMAGE,
        "Услуги и цены:\n\n"
        "Маникюр без покрытия — 1200 ₽\n"
        "Маникюр с гель-лаком — 2200 ₽\n"
        "Педикюр без покрытия — 1800 ₽\n"
        "Педикюр с гель-лаком — 2800 ₽\n"
        "Снятие покрытия — 500 ₽\n"
        "Дизайн — от 100 ₽ за ноготь",
    )


@dp.message(F.text == "Мастера")
async def masters_handler(message: Message):
    await send_photo_with_text(
        message,
        MASTERS_IMAGE,
        "Наши мастера:\n\n"
        "Анна — маникюр, дизайн, френч\n"
        "Мария — педикюр, укрепление\n"
        "Елена — маникюр и педикюр",
    )


@dp.message(F.text == "Контакты и адрес")
async def contacts_handler(message: Message):
    await send_photo_with_text(
        message,
        CONTACTS_IMAGE,
        "Контакты Nail Studio:\n\n"
        "Адрес: Москва, ул. Примерная, 10\n"
        "Телефон: +7 999 123-45-67\n"
        "Telegram: @nailstudio_manager\n"
        "Время работы: ежедневно 10:00–21:00",
    )


@dp.message(F.text == "Записаться")
async def start_booking_handler(message: Message, state: FSMContext):
    await state.set_state(BookingState.choosing_service)
    await message.answer("Выберите услугу:", reply_markup=services_keyboard)


@dp.message(F.text == "Отмена")
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=main_keyboard)


@dp.message(F.text == "Подтвердить визит")
async def confirm_visit_handler(message: Message):
    booking = get_nearest_booking_for_user(message.from_user.id)

    if not booking:
        await message.answer(
            "Активная запись не найдена.",
            reply_markup=main_keyboard,
        )
        return

    mark_booking_confirmed(booking["id"])

    await message.answer(
        "Визит подтверждён.\n\n"
        f"Услуга: {booking['service']}\n"
        f"Дата: {booking['date']}\n"
        f"Время: {booking['time']}",
        reply_markup=main_keyboard,
    )

    await notify_manager(
        message.bot,
        "Клиент подтвердил визит\n\n"
        f"Услуга: {booking['service']}\n"
        f"Дата: {booking['date']}\n"
        f"Время: {booking['time']}\n"
        f"Имя: {booking['name']}\n"
        f"Телефон: {booking['phone']}",
    )


@dp.message(F.text == "Отменить визит")
async def cancel_visit_handler(message: Message):
    booking = get_nearest_booking_for_user(message.from_user.id)

    if not booking:
        await message.answer(
            "Активная запись не найдена.",
            reply_markup=main_keyboard,
        )
        return

    delete_booking_by_id(booking["id"])

    await message.answer(
        "Запись отменена.",
        reply_markup=main_keyboard,
    )

    await notify_manager(
        message.bot,
        "Клиент отменил запись\n\n"
        f"Услуга: {booking['service']}\n"
        f"Дата: {booking['date']}\n"
        f"Время: {booking['time']}\n"
        f"Имя: {booking['name']}\n"
        f"Телефон: {booking['phone']}",
    )


@dp.message(F.text == "Перезаписаться")
async def reschedule_handler(message: Message, state: FSMContext):
    booking = get_nearest_booking_for_user(message.from_user.id)

    if booking:
        delete_booking_by_id(booking["id"])

        await notify_manager(
            message.bot,
            "Клиент начал перезапись. Старая запись удалена.\n\n"
            f"Услуга: {booking['service']}\n"
            f"Дата: {booking['date']}\n"
            f"Время: {booking['time']}\n"
            f"Имя: {booking['name']}\n"
            f"Телефон: {booking['phone']}",
        )

    await state.set_state(BookingState.choosing_service)

    await message.answer(
        "Выберите новую услугу:",
        reply_markup=services_keyboard,
    )


@dp.message(BookingState.choosing_service)
async def choose_service_handler(message: Message, state: FSMContext):
    allowed_services = [
        "Маникюр без покрытия",
        "Маникюр с гель-лаком",
        "Педикюр без покрытия",
        "Педикюр с гель-лаком",
    ]

    if message.text not in allowed_services:
        await message.answer(
            "Выберите услугу кнопкой из списка.",
            reply_markup=services_keyboard,
        )
        return

    await state.update_data(service=message.text)
    await state.set_state(BookingState.entering_date)

    await message.answer(
        "Введите дату записи в формате ДД.ММ.ГГГГ.\n\n"
        "Например: 22.05.2026\n\n"
        "Записаться можно в пределах ближайших 14 дней."
    )


@dp.message(BookingState.entering_date)
async def enter_date_handler(message: Message, state: FSMContext):
    selected_date = parse_booking_date(message.text.strip())

    if selected_date is None:
        await message.answer("Неверный формат даты.\n\nВведите дату так: 22.05.2026")
        return

    is_available, error_text = is_date_available(selected_date)

    if not is_available:
        await message.answer(error_text + "\n\nВведите другую дату в формате ДД.ММ.ГГГГ.")
        return

    selected_date_text = selected_date.strftime("%d.%m.%Y")
    free_times = get_free_times(selected_date_text)

    if not free_times:
        await message.answer(
            "На эту дату свободных окон нет.\n\n"
            "Введите другую дату в формате ДД.ММ.ГГГГ."
        )
        return

    await state.update_data(date=selected_date_text)
    await state.set_state(BookingState.choosing_time)

    await message.answer(
        "Выберите свободное время:",
        reply_markup=build_times_keyboard(free_times),
    )


@dp.message(BookingState.choosing_time)
async def choose_time_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    date_text = data["date"]
    free_times = get_free_times(date_text)

    if message.text not in free_times:
        await message.answer(
            "Выберите свободное время кнопкой из списка.",
            reply_markup=build_times_keyboard(free_times),
        )
        return

    await state.update_data(time=message.text)
    await state.set_state(BookingState.entering_name)

    await message.answer("Введите ваше имя:")


@dp.message(BookingState.entering_name)
async def enter_name_handler(message: Message, state: FSMContext):
    name = message.text.strip()

    if len(name) < 2:
        await message.answer("Имя слишком короткое. Введите имя ещё раз:")
        return

    if len(name) > 40:
        await message.answer("Имя слишком длинное. Введите имя короче.")
        return

    if contains_banned_words(name):
        await message.answer("Некорректное имя. Введите имя ещё раз.")
        return

    await state.update_data(name=name)
    await state.set_state(BookingState.entering_phone)

    await message.answer("Введите контактный номер телефона:")


@dp.message(BookingState.entering_phone)
async def enter_phone_handler(message: Message, state: FSMContext):
    phone = message.text.strip()

    if len(phone) < 7:
        await message.answer("Номер слишком короткий. Введите телефон ещё раз:")
        return

    if len(phone) > 20:
        await message.answer("Номер слишком длинный. Введите телефон ещё раз:")
        return

    if contains_banned_words(phone):
        await message.answer("Некорректный номер телефона.")
        return

    allowed_symbols = "+0123456789()- "

    for char in phone:
        if char not in allowed_symbols:
            await message.answer("Телефон содержит недопустимые символы.")
            return

    await state.update_data(phone=phone)
    data = await state.get_data()

    await state.set_state(BookingState.confirming)

    await message.answer(
        "Проверьте запись:\n\n"
        f"Услуга: {data['service']}\n"
        f"Дата: {data['date']}\n"
        f"Время: {data['time']}\n"
        f"Имя: {data['name']}\n"
        f"Телефон: {data['phone']}\n\n"
        "Подтвердить запись?",
        reply_markup=confirm_keyboard,
    )


@dp.message(BookingState.confirming)
async def confirm_booking_handler(message: Message, state: FSMContext):
    if message.text == "Отменить запись":
        await state.clear()
        await message.answer("Запись отменена.", reply_markup=main_keyboard)
        return

    if message.text != "Подтвердить запись":
        await message.answer(
            "Нажмите кнопку подтверждения или отмены.",
            reply_markup=confirm_keyboard,
        )
        return

    data = await state.get_data()

    saved = save_booking(
        user_id=message.from_user.id,
        username=message.from_user.username,
        service=data["service"],
        date_text=data["date"],
        time_text=data["time"],
        name=data["name"],
        phone=data["phone"],
    )

    if not saved:
        await state.set_state(BookingState.entering_date)
        await message.answer(
            "Это время уже заняли. Введите другую дату в формате ДД.ММ.ГГГГ."
        )
        return

    username = message.from_user.username
    telegram_contact = f"@{username}" if username else "Нет username"

    await notify_manager(
        message.bot,
        "Новая запись\n\n"
        f"Услуга: {data['service']}\n"
        f"Дата: {data['date']}\n"
        f"Время: {data['time']}\n"
        f"Имя: {data['name']}\n"
        f"Телефон: {data['phone']}\n"
        f"Telegram: {telegram_contact}",
    )

    await message.answer(
        "Вы успешно записаны.\n\n"
        f"Услуга: {data['service']}\n"
        f"Дата: {data['date']}\n"
        f"Время: {data['time']}\n\n"
        "Ждём вас в Nail Studio.",
        reply_markup=main_keyboard,
    )

    await state.clear()


@dp.message()
async def fallback_handler(message: Message):
    await message.answer("Выберите действие в меню ниже.", reply_markup=main_keyboard)


async def main():
    logging.basicConfig(level=logging.INFO)

    if not TOKEN:
        raise RuntimeError("Не найден BOT_TOKEN")

    init_db()

    bot = Bot(token=TOKEN)

    asyncio.create_task(reminder_loop(bot))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())