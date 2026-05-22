# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Bot

```bash
pip install -r requirements.txt   # install dependencies
python bot.py                      # run the bot
```

Requires a `.env` file with `BOT_TOKEN=<telegram_bot_token>`.

On Windows, `run.bat` runs `py -3.10 bot.py`.

## Deployment

Deployed to [Amvera](https://amvera.ru/) platform. Config in [amvera.yaml](amvera.yaml): Python 3.11, entry `python bot.py`, `/data` mounted for persistence (database lives there in prod).

## Architecture

Single-file application: all logic is in [bot.py](bot.py) (~822 lines). No modules, no tests.

**Tech stack:** Python + aiogram 3.x (async Telegram framework) + SQLite3.

### Bot flow

1. `/start` → main menu with inline keyboard buttons
2. Info pages (About, Services/Prices, Masters, Contacts) send photos from `images/` with captions
3. Booking follows a 6-state FSM: `choosing_service → entering_date → choosing_time → entering_name → entering_phone → confirming`

### Key subsystems

**FSM (BookingStates)** — aiogram `StatesGroup` controls the booking wizard. Each state has a handler that validates input and advances or re-prompts.

**Database** — SQLite `bookings` table. Unique constraint on `(date, time)` enforces one booking per slot. `reminder_sent` and `client_confirmed` are integer flags.

**Reminder loop** — async background task (runs every 60 s) queries bookings within the next 0–24 hours where `reminder_sent = 0`, sends reminder to client, marks `reminder_sent = 1`.

**Time slot logic** — 6 fixed slots: 10:00, 12:00, 14:00, 16:00, 18:00, 20:00. Booked slots are excluded from the inline keyboard. Past times on today's date are also filtered.

**Date pagination** — 14-day booking window displayed 7 days per page with `<` / `>` navigation via callback data.

**Manager notifications** — hardcoded `MANAGER_ID = 6430611356`; receives messages on new booking, cancellation, and client confirmation.

**Input validation** — phone accepts `8XXXXXXXXXX` or `+7XXXXXXXXXX`; name 2–40 chars with banned-word filter.
