"""Send the daily weather and WebUntis timetable, then exit (GitHub Actions)."""
import asyncio
import logging
import os
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import httpx
from icalendar import Calendar
from telegram import Bot
from telegram.helpers import escape_markdown

from bot import BOT_TOKEN, CITY, TELEGRAM_USER_ID, TIMEZONE, build_weather_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

RU_WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
)


def _as_local_datetime(value, tz: ZoneInfo) -> datetime:
    """Convert an iCalendar date/date-time to the school's local timezone."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # WebUntis iCal commonly provides local (floating) times.
            return value.replace(tzinfo=tz)
        return value.astimezone(tz)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=tz)
    raise ValueError("Unsupported DTSTART/DTEND in WebUntis iCal")


def _clean_ical_text(value, fallback: str = "") -> str:
    if value is None:
        return fallback
    # Flatten multiline descriptions and keep Telegram messages readable.
    return " ".join(str(value).replace("\\n", " ").replace("\r", " ").split()).strip()


async def build_timetable_message() -> str:
    """Download the private WebUntis iCal feed and format today's lessons."""
    ical_url = os.getenv("WEBUNTIS_ICAL_URL", "").strip()
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)
    today = now.date()
    weekday = RU_WEEKDAYS[today.weekday()]
    date_label = f"{weekday}, {today:%d.%m.%Y}"
    header = f"📚 *Расписание на сегодня — {date_label}*"

    if not ical_url:
        return (
            f"{header}\n"
            "⚠️ WebUntis пока не подключён. Добавь ссылку в секрет GitHub "
            "`WEBUNTIS_ICAL_URL`."
        )

    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            response = await client.get(ical_url, headers={"Accept": "text/calendar"})
            response.raise_for_status()
        calendar = Calendar.from_ical(response.content)
    except Exception:
        # Don't log the URL: an iCal subscription link may grant access to the timetable.
        logger.exception("Could not download or parse the WebUntis iCal feed.")
        return (
            f"{header}\n"
            "⚠️ Не удалось загрузить расписание из WebUntis. Проверь секрет "
            "`WEBUNTIS_ICAL_URL` и доступность iCal-подписки в WebUntis."
        )

    lessons = []
    for event in calendar.walk("VEVENT"):
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        start_property = event.get("DTSTART")
        if start_property is None:
            continue
        try:
            start = _as_local_datetime(start_property.dt, tz)
        except (AttributeError, TypeError, ValueError):
            continue
        if start.date() != today:
            continue

        end_property = event.get("DTEND")
        try:
            end = _as_local_datetime(end_property.dt, tz) if end_property else start
        except (AttributeError, TypeError, ValueError):
            end = start

        title = _clean_ical_text(event.get("SUMMARY"), "Урок")
        location = _clean_ical_text(event.get("LOCATION"))
        description = _clean_ical_text(event.get("DESCRIPTION"))

        if start_property.dt.__class__ is date:
            time_range = "весь день"
        else:
            time_range = f"{start:%H:%M}–{end:%H:%M}"

        line = f"• *{escape_markdown(title, version=1)}* — {time_range}"
        if location:
            line += f" · 📍 {escape_markdown(location, version=1)}"
        lessons.append((start, line, description, title))

    lessons.sort(key=lambda item: item[0])
    if not lessons:
        return f"{header}\n🎉 На сегодня в календаре WebUntis нет занятий."

    lines = [header, ""]
    for _, line, description, title in lessons:
        lines.append(line)
        # WebUntis formats differ by school. Include a short description when it
        # adds useful detail, such as a teacher or substitution note.
        if description and description.casefold() != title.casefold() and len(description) <= 180:
            lines.append(f"  _{escape_markdown(description, version=1)}_")
    lines.extend(["", f"_Источник: WebUntis · {escape_markdown(CITY, version=1)}_"])
    return "\n".join(lines)


async def main() -> None:
    weather_message = await build_weather_message()
    timetable_message = await build_timetable_message()
    message = f"{weather_message}\n\n{timetable_message}"

    # Telegram messages have a 4096-character limit. Timetables are normally
    # much shorter; truncate unusually long schedules instead of losing the send.
    if len(message) > 4000:
        message = message[:3970] + "…\n(Расписание сокращено)"

    async with Bot(token=BOT_TOKEN) as telegram_bot:
        await telegram_bot.send_message(
            chat_id=TELEGRAM_USER_ID,
            text=message,
            parse_mode="Markdown",
        )
    logger.info("Daily weather and timetable message sent successfully.")


if __name__ == "__main__":
    asyncio.run(main())
