"""Send daily weather and the signed-in user's WebUntis timetable, then exit."""
import asyncio
import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown

from bot import BOT_TOKEN, CITY, TELEGRAM_USER_ID, TIMEZONE, build_weather_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

RU_WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
)

# Values come from the WebUntis link the user supplied:
# https://wirtschaftsschule.webuntis.com/WebUntis?school=wirtschaftsschule
WEBUNTIS_SERVER = "wirtschaftsschule.webuntis.com"
WEBUNTIS_SCHOOL = "wirtschaftsschule"


def _name_of(item, attributes: tuple[str, ...]) -> str:
    """Get the first non-empty display-name attribute from a WebUntis object."""
    for attr in attributes:
        try:
            value = getattr(item, attr, None)
        except Exception:
            value = None
        if value:
            return " ".join(str(value).split())
    return ""


def _time_label(value) -> str:
    if value is None:
        return "--:--"
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%H:%M")
        except Exception:
            pass
    text = str(value)
    if len(text) >= 16 and text[10] in {"T", " "}:
        return text[11:16]
    return text[:5] if len(text) >= 5 else "--:--"


def _period_items(period, attribute: str) -> list:
    try:
        value = getattr(period, attribute, None)
        return list(value) if value else []
    except Exception:
        return []


def build_timetable_message() -> str:
    """Log in to WebUntis and fetch the timetable of the authenticated student."""
    username = os.getenv("WEBUNTIS_USERNAME", "").strip()
    password = os.getenv("WEBUNTIS_PASSWORD", "")
    tz = ZoneInfo(TIMEZONE)
    today = datetime.now(tz).date()
    target_date_text = os.getenv("TARGET_DATE", "").strip()
    if target_date_text:
        try:
            target_day = date.fromisoformat(target_date_text)
        except ValueError as exc:
            raise ValueError("TARGET_DATE must be in YYYY-MM-DD format, for example 2026-10-12.") from exc
    else:
        target_day = today

    weekday = RU_WEEKDAYS[target_day.weekday()]
    date_label = f"{weekday}, {target_day:%d.%m.%Y}"
    if target_date_text:
        header = f"🧪 *Тест расписания — {date_label}*"
    elif target_day == today:
        header = f"📚 *Расписание на сегодня — {date_label}*"
    else:
        header = f"📚 *Расписание — {date_label}*"

    # Saturdays and Sundays are always treated as days without lessons.
    if target_day.weekday() >= 5:
        return f"{header}\n🌴 На эту дату занятий по расписанию нет (выходной)."

    if not username or not password:
        return (
            f"{header}\n"
            "⚠️ Не настроен вход в WebUntis. Добавь секреты `WEBUNTIS_USERNAME` и "
            "`WEBUNTIS_PASSWORD` в GitHub → Settings → Secrets and variables → Actions. "
            "Погода будет отправлена независимо от этой ошибки."
        )

    try:
        # Imported here so a timetable-library issue cannot prevent the weather
        # portion from being sent. Never log the supplied credentials.
        import webuntis

        with webuntis.Session(
            username=username,
            password=password,
            server=WEBUNTIS_SERVER,
            school=WEBUNTIS_SCHOOL,
            useragent="PersonalWeatherTelegramBot/1.0",
        ).login() as session:
            periods = list(session.my_timetable(start=target_day, end=target_day))

            # WebUntis period relationships (subjects, rooms, teachers) are lazy:
            # the properties may call the API the first time they are accessed.
            # Resolve them before leaving the session, otherwise later lookups
            # happen after logout and can silently fall back to generic "Unterricht".
            for period in periods:
                for attribute in ("subjects", "rooms", "teachers"):
                    try:
                        related = getattr(period, attribute, None)
                        if related:
                            list(related)
                    except Exception as lazy_error:
                        # Keep the weather delivery resilient; rendering below
                        # will use available text fields if a relation is absent.
                        logger.debug(
                            "Could not preload WebUntis %s (error type: %s).",
                            attribute,
                            type(lazy_error).__name__,
                        )

    except Exception as exc:
        error_name = type(exc).__name__
        logger.warning("WebUntis timetable fetch failed (error type: %s).", error_name)
        if error_name == "BadCredentialsError":
            detail = (
                "WebUntis не принял логин или пароль. Проверь секреты `WEBUNTIS_USERNAME` "
                "и `WEBUNTIS_PASSWORD` — используй те же данные, что и при входе на сайт."
            )
        elif error_name in {"ImportError", "ModuleNotFoundError"}:
            detail = "Не удалось загрузить библиотеку WebUntis. Проверь установку зависимостей в GitHub Actions."
        else:
            detail = (
                "Не удалось войти в WebUntis API или получить личное расписание "
                f"(ошибка {error_name}). Возможно, школа ограничивает API или изменён способ входа. "
                "Проверь журнал запуска в GitHub Actions."
            )
        return f"{header}\n⚠️ {detail}\nПогода будет отправлена независимо от этой ошибки."

    periods.sort(key=lambda period: _time_label(getattr(period, "start", None)))
    if not periods:
        return (
            f"{header}\n"
            "🎉 WebUntis не показывает уроков на эту дату. Возможно, это выходной, каникулы "
            "или расписание ещё не опубликовано."
        )

    lines = [header, ""]
    for period in periods:
        start = _time_label(getattr(period, "start", None))
        end = _time_label(getattr(period, "end", None))
        subjects = [
            _name_of(item, ("long_name", "name"))
            for item in _period_items(period, "subjects")
        ]
        subjects = [item for item in subjects if item]
        subject = ", ".join(dict.fromkeys(subjects))
        if not subject:
            subject = _name_of(period, ("lstext", "substText", "activityType")) or "Урок"
        subject = " ".join(subject.replace("\n", " ").split())
        code = getattr(period, "code", None)
        if code == "cancelled":
            subject = "❌ Отменён: " + subject
        elif code == "irregular":
            subject = "🔁 Замена: " + subject
        line = f"• *{escape_markdown(subject[:120], version=1)}* — {start}–{end}"

        rooms = [
            _name_of(item, ("name", "long_name"))
            for item in _period_items(period, "rooms")
        ]
        rooms = [item for item in rooms if item]
        if rooms:
            line += " · 🚪 " + escape_markdown(", ".join(dict.fromkeys(rooms)), version=1)

        teachers = [
            _name_of(item, ("full_name", "name", "long_name"))
            for item in _period_items(period, "teachers")
        ]
        teachers = [item for item in teachers if item]
        if teachers:
            line += " · 👨‍🏫 " + escape_markdown(", ".join(dict.fromkeys(teachers)), version=1)
        lines.append(line)

        note = _name_of(period, ("substText", "lstext"))
        if note and note.casefold() != subject.casefold() and len(note) <= 200:
            lines.append(f"  _{escape_markdown(note, version=1)}_")

    lines.extend(["", "_Источник: личное расписание WebUntis_"])
    return "\n".join(lines)


async def main() -> None:
    weather_message = await build_weather_message()
    timetable_message = await asyncio.to_thread(build_timetable_message)
    target_date_text = os.getenv("TARGET_DATE", "").strip()
    if target_date_text:
        test_note = (
            f"🧪 *Проверочный запуск:* расписание запрашивается на {target_date_text}.\n"
            "Погодная часть сообщения отражает текущий день.\n\n"
        )
    else:
        test_note = ""
    message = f"{test_note}{weather_message}\n\n{timetable_message}"
    if len(message) > 4000:
        message = message[:3970] + "…\n(Сообщение сокращено)"

    async with Bot(token=BOT_TOKEN) as telegram_bot:
        commute_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🚇 Когда выходить?", callback_data="commute_v1")]
        ])
        await telegram_bot.send_message(
            chat_id=TELEGRAM_USER_ID,
            text=message,
            parse_mode="Markdown",
            reply_markup=commute_keyboard,
        )
    logger.info("Daily weather and timetable message sent successfully.")


if __name__ == "__main__":
    asyncio.run(main())
