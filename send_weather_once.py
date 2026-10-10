"""Send one weather report, then exit (for GitHub Actions)."""
import asyncio
import logging

from telegram import Bot

from bot import BOT_TOKEN, TELEGRAM_USER_ID, build_weather_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")


async def main() -> None:
    message = await build_weather_message()
    async with Bot(token=BOT_TOKEN) as telegram_bot:
        await telegram_bot.send_message(
            chat_id=TELEGRAM_USER_ID,
            text=message,
            parse_mode="Markdown",
        )
    logging.info("Daily weather message sent successfully.")


if __name__ == "__main__":
    asyncio.run(main())
