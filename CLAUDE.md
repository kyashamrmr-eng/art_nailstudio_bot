# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the Bot

```bash
pip install -r requirements.txt   # install dependencies
python bot.py                      # run the bot
```

Requires a `.env` file with `BOT_TOKEN=<telegram_bot_token>`.

On Windows, `run.bat` runs `py -3.10 bot.py`.

Optional: `credentials.json` (Google service account) in the project root enables Google Sheets sync. If the file is absent, sync is silently skipped.

## Deployment

Deployed to [Amvera](https://amvera.ru/) platform. Config in [amvera.yaml](amvera.yaml): Python 3.11, entry `python bot.py`, `/data` mounted for persistence. In production, set `DB_PATH=/data/bookings.db` so the database survives redeployments.

## Architecture

Single-file application: all logic is in [bot.py](bot.py) (~2478 lines). No modules, no tests.

**Tech stack:** Python + aiogram 3.x (async Telegram framework) + SQLite3 + gspread (Google Sheets).

### Bot flow

1. `/start` → main menu (ReplyKeyboard)
2. Info pages (About, Services/Prices, Masters, Contacts) show content from `info_pages` DB table with optional photo; fallback to `images/` files
3. Booking follows a 7-state FSM: `choosing_service → entering_date → choosing_master → choosing_time → entering_name → entering_phone → confirming`
4. After a visit, `review_loop` sends a rating request 3+ hours later, entering `ReviewState`
5. Admin panel accessible via `/admin` (only for `MANAGER_CHAT_ID`)

### FSM state groups

- **`BookingState`** — 7 states for the client booking wizard
- **`CancelBookingState`** — 2 states for cancellation (`confirming_single`, `choosing_booking`)
- **`ReviewState`** — 2 states injected by the background loop (`rating_pending`, `comment_pending`)
- **`AdminState`** — ~35 states covering masters, salon schedule, services catalog, and info pages

### Database schema

Nine tables in SQLite:
- **`bookings`** — core table; unique constraint on `(master_id, date, time)`; flags: `reminder_sent`, `client_confirmed`, `review_sent`, `duration`
- **`masters`** / **`master_services`** — master roster and which services each offers
- **`master_schedules`** — repeating schedule patterns (`all`, `2/2`, `5/2`, or custom `W/O` work/off cycles with a `start_date`)
- **`master_day_overrides`** — per-master date overrides (vacation days, one-off day-offs)
- **`salon_day_overrides`** — salon-wide closures or custom hours for specific dates
- **`services`** — catalog of services with price and duration (1 or 2 hours); seeded on first run
- **`reviews`** — client ratings and comments linked to a master
- **`info_pages`** — editable text + Telegram `file_id` for each info section

`init_db()` runs inline migrations (ALTER TABLE) to add columns missing from older schemas.

### Key subsystems

**Slot availability** — `WORKING_TIMES` = 10 slots: 10:00–19:00 hourly. `get_free_times_for_master()` filters: outside salon hours, past times today, already-booked slots, and for 2-hour services the following slot must also be free. `is_slot_blocked()` also checks whether a preceding 2-hour booking extends into the queried slot.

**Master scheduling** — `is_master_working(mid, date_text)` first checks `master_day_overrides`, then computes the W/O cycle from `master_schedules`. `get_masters_for_service_on_date()` chains salon-open check + active masters + correct service + working that day.

**Reminder loop** — runs every 60 s; sends reminder when booking is within 48 h (triggers at ≤2 h before) or outside 48 h window (triggers at ≤24 h before); marks `reminder_sent=1`.

**Review loop** — runs every 60 s; sends rating request ≥3 h after visit time; skips users already in an FSM state; injects `ReviewState` directly via `storage.set_state`.

**Google Sheets** — `sheets_add_booking` / `sheets_set_status` are fire-and-forget `asyncio.create_task` wrappers around blocking gspread calls run via `asyncio.to_thread`. Sheet ID is hardcoded as `GOOGLE_SHEET_ID`.

**Admin panel** — `/admin` (manager only) gives access to: master CRUD (add/edit/deactivate), salon schedule overrides, service catalog CRUD, and info-page editing (text + photo). Info-page button handlers (`О салоне`, etc.) check if the current FSM state is `AdminState.info_select` and hijack the message for editing.

**Loyalty badge** — `loyalty_badge(count)` appends ⭐ (≥2 visits) or ❤️ (≥5 visits) to client name in admin notifications and schedule view.

**Input validation** — phone: `8XXXXXXXXXX` or `+7XXXXXXXXXX` (strips spaces/dashes); name: 2–40 chars with banned-word filter; date: `DD.MM.YYYY` or `DD.MM.YY`.

### Constants

- `MANAGER_CHAT_ID = 6430611356` — hardcoded manager Telegram ID; used for access control and notifications
- `GOOGLE_SHEET_ID` — hardcoded sheet key
- `DEFAULT_OPEN = "10:00"`, `DEFAULT_CLOSE = "20:00"` — salon default hours
