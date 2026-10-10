import logging
import os
from datetime import time
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TELEGRAM_USER_ID_TEXT = os.getenv("TELEGRAM_USER_ID", "0").strip()
CITY = os.getenv("CITY", "Нюрнберг").strip()
LATITUDE = float(os.getenv("LATITUDE", "49.4521"))
LONGITUDE = float(os.getenv("LONGITUDE", "11.0767"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Berlin").strip()

try:
    TELEGRAM_USER_ID = int(TELEGRAM_USER_ID_TEXT)
except ValueError as exc:
    raise SystemExit("TELEGRAM_USER_ID должен быть числом. Проверь файл .env.") from exc

if not BOT_TOKEN:
    raise SystemExit("Не найден BOT_TOKEN. Добавь токен бота в файл .env.")
if TELEGRAM_USER_ID <= 0:
    raise SystemExit("Укажи свой числовой TELEGRAM_USER_ID в файле .env.")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
# Hide noisy HTTP request logs and avoid logging secrets.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

WMO_DESCRIPTIONS = {
    0: "ясно",
    1: "преимущественно ясно",
    2: "переменная облачность",
    3: "пасмурно",
    45: "туман",
    48: "туман с изморозью",
    51: "слабая морось",
    53: "морось",
    55: "сильная морось",
    56: "слабая ледяная морось",
    57: "сильная ледяная морось",
    61: "небольшой дождь",
    63: "дождь",
    65: "сильный дождь",
    66: "слабый ледяной дождь",
    67: "сильный ледяной дождь",
    71: "небольшой снег",
    73: "снег",
    75: "сильный снегопад",
    77: "снежная крупа",
    80: "небольшой дождь с перерывами",
    81: "дождь с перерывами",
    82: "сильные ливни",
    85: "небольшой снегопад с перерывами",
    86: "сильный снегопад с перерывами",
    95: "гроза",
    96: "гроза с небольшим градом",
    99: "гроза с сильным градом",
}

WET_CODES = {
    51, 53, 55, 56, 57, 61, 63, 65, 66, 67,
    71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99,
}
RU_WEEKDAYS = (
    "понедельник", "вторник", "среда", "четверг",
    "пятница", "суббота", "воскресенье",
)


def describe_weather(code) -> str:
    try:
        return WMO_DESCRIPTIONS.get(int(code), "переменная погода")
    except (TypeError, ValueError):
        return "переменная погода"


def clothing_advice(
    min_temp: float,
    max_temp: float,
    precipitation_probability: int | None,
    current_code: int,
    daily_code: int,
    max_wind: float,
) -> str:
    """Give a simple clothing suggestion based on today's forecast."""
    if max_temp <= 4:
        advice = "тёплая зимняя куртка, свитер и длинные брюки"
        if min_temp <= 0:
            advice += "; утром пригодятся шапка и перчатки"
    elif max_temp <= 9:
        advice = "тёплая куртка, худи или свитер и длинные брюки"
    elif max_temp <= 14:
        advice = "демисезонная куртка поверх худи/свитера и длинные брюки"
    elif max_temp <= 19:
        advice = "худи или лёгкая куртка и длинные брюки"
    elif max_temp <= 24:
        advice = "футболка или лёгкий верх; возьми тонкую кофту на утро и вечер"
    else:
        advice = "лёгкая, дышащая одежда"

    likely_wet = (
        (precipitation_probability is not None and precipitation_probability >= 40)
        or current_code in WET_CODES
        or daily_code in WET_CODES
    )
    if likely_wet:
        advice += ". Возьми зонт или непромокаемую куртку"
    if max_wind >= 35:
        advice += ". Будет ветрено — лучше выбрать ветрозащитный верх"
    return advice + "."


async def build_weather_message() -> str:
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": LATITUDE,
        "longitude": LONGITUDE,
        "current": ",".join([
            "temperature_2m",
            "apparent_temperature",
            "relative_humidity_2m",
            "precipitation",
            "weather_code",
            "wind_speed_10m",
        ]),
        "daily": ",".join([
            "temperature_2m_max",
            "temperature_2m_min",
            "precipitation_probability_max",
            "weather_code",
            "wind_speed_10m_max",
        ]),
        "timezone": TIMEZONE,
        "forecast_days": 1,
        "temperature_unit": "celsius",
        "wind_speed_unit": "kmh",
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

    current = data["current"]
    daily = data["daily"]

    current_temp = round(float(current["temperature_2m"]))
    feels_like = round(float(current["apparent_temperature"]))
    current_wind = round(float(current["wind_speed_10m"]))
    current_code = int(current["weather_code"])

    min_temp = round(float(daily["temperature_2m_min"][0]))
    max_temp = round(float(daily["temperature_2m_max"][0]))
    daily_code = int(daily["weather_code"][0])
    max_wind = float(daily["wind_speed_10m_max"][0] or 0)

    raw_probability = daily.get("precipitation_probability_max", [None])[0]
    precipitation_probability = (
        int(raw_probability) if raw_probability is not None else None
    )

    date_text = str(daily["time"][0])
    year, month, day = (int(part) for part in date_text.split("-")[:3])
    from datetime import date
    weekday = RU_WEEKDAYS[date(year, month, day).weekday()]
    current_time = str(current.get("time", ""))[-5:]
    current_desc = describe_weather(current_code)
    daily_desc = describe_weather(daily_code)

    if precipitation_probability is None:
        rain_line = "🌧 Вероятность осадков: нет данных"
    else:
        rain_line = f"🌧 Вероятность осадков сегодня: до {precipitation_probability}%"

    outfit = clothing_advice(
        min_temp=min_temp,
        max_temp=max_temp,
        precipitation_probability=precipitation_probability,
        current_code=current_code,
        daily_code=daily_code,
        max_wind=max_wind,
    )

    return (
        f"🌦 *Погода на сегодня — {CITY}*\n"
        f"📅 {weekday}, {day:02d}.{month:02d}.{year}\n\n"
        f"*Сейчас{f' ({current_time})' if current_time else ''}:* "
        f"{current_temp:+d} °C, {current_desc}\n"
        f"🌡 Ощущается как: {feels_like:+d} °C\n"
        f"💨 Ветер сейчас: {current_wind} км/ч\n\n"
        f"*Сегодня днём:* {daily_desc}\n"
        f"🌡 Температура за день: от {min_temp:+d} до {max_temp:+d} °C\n"
        f"☀️ Максимум за день: около {max_temp:+d} °C\n"
        f"{rain_line}\n\n"
        f"👕 *Что надеть:* {outfit}\n\n"
        f"_Источник погоды: Open-Meteo_"
    )


async def is_authorized(update: Update) -> bool:
    user = update.effective_user
    if user and user.id == TELEGRAM_USER_ID:
        return True
    # Don't expose private bot functionality to other Telegram users.
    if update.effective_message:
        await update.effective_message.reply_text("Этот бот доступен только владельцу.")
    return False


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_authorized(update):
        return
    await update.effective_message.reply_text(
        "Привет! Я твой личный погодный помощник.\n\n"
        "Каждый день в 07:00 по времени Нюрнберга пришлю прогноз и совет, что надеть.\n\n"
        "Команда для проверки: /weather\n"
        "Список команд: /help"
    )


async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_authorized(update):
        return
    await update.effective_message.reply_text("Секунду, проверяю погоду…")
    try:
        message = await build_weather_message()
        await update.effective_message.reply_text(message, parse_mode="Markdown")
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
        logger.exception("Could not fetch or format weather")
        await update.effective_message.reply_text(
            "Не получилось получить погоду. Попробуй ещё раз чуть позже."
        )


async def daily_weather_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        message = await build_weather_message()
        await context.bot.send_message(
            chat_id=TELEGRAM_USER_ID,
            text=message,
            parse_mode="Markdown",
        )
        logger.info("Daily weather message sent")
    except Exception:
        logger.exception("Failed to send daily weather message")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await is_authorized(update):
        return
    await update.effective_message.reply_text(
        "Команды:\n"
        "/start — приветствие\n"
        "/weather — погода прямо сейчас и прогноз на сегодня\n"
        "/help — эта подсказка\n\n"
        "Ежедневная сводка запланирована на 07:00 по времени Нюрнберга."
    )


def main() -> None:
    try:
        local_timezone = ZoneInfo(TIMEZONE)
    except Exception as exc:
        raise SystemExit(f"Не удалось определить часовой пояс {TIMEZONE!r}.") from exc

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("weather", weather_command))
    application.add_handler(CommandHandler("help", help_command))

    if application.job_queue is None:
        raise SystemExit(
            "JobQueue не установлен. Выполни: python -m pip install 'python-telegram-bot[job-queue]'"
        )

    application.job_queue.run_daily(
        daily_weather_job,
        time=time(hour=7, minute=0, tzinfo=local_timezone),
        name="daily-weather-0700",
    )

    logger.info("Bot is starting; daily weather is scheduled for 07:00 %s", TIMEZONE)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
