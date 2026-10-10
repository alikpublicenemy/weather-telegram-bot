"""Send daily weather and public WebUntis timetable, then exit (GitHub Actions)."""
import asyncio
import base64
import logging
import os
import time as system_time
from datetime import date, datetime
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
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


def _parse_public_link(public_url: str) -> tuple[str, str, int, int]:
    """Return WebUntis base URL, school slug, public element type and ID.

    New WebUntis share links keep timetable details in the URL fragment,
    for example: /WebUntis?school=school-name#/basic/timetablePublic/my-student?date=...&entityId=123
    """
    parsed = urlsplit(public_url.strip())
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise ValueError("WEBUNTIS_PUBLIC_URL must be the full public link copied from WebUntis.")

    school_values = parse_qs(parsed.query).get("school", [])
    school = school_values[0].strip() if school_values else ""
    fragment = parsed.fragment
    fragment_query = fragment.split("?", 1)[1] if "?" in fragment else ""
    fragment_params = parse_qs(fragment_query)
    entity_id_values = fragment_params.get("entityId", [])
    if not school or not entity_id_values:
        raise ValueError("The WebUntis link has no school or entityId. Copy it with 'Öffentliche Link kopieren'.")

    try:
        entity_id = int(entity_id_values[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("Could not read entityId from the WebUntis public link.") from exc

    # The supplied route contains 'my-student', which is a student timetable.
    route = fragment.split("?", 1)[0].rstrip("/")
    if route.endswith("/my-student"):
        element_type = 5
    elif route.endswith("/my-class") or route.endswith("/class"):
        element_type = 1
    elif route.endswith("/my-teacher") or route.endswith("/teacher"):
        element_type = 2
    elif route.endswith("/room"):
        element_type = 4
    else:
        # In the link supplied for this project the route is my-student.
        # Defaulting to student is appropriate for a student timetable share link.
        element_type = 5

    base_url = f"{parsed.scheme}://{parsed.netloc}/WebUntis"
    return base_url, school, element_type, entity_id


def _extract_timetable_data(payload: dict):
    """Return the timetable data object if the response resembles timetable JSON."""
    try:
        data = payload["data"]["result"]["data"]
    except (KeyError, TypeError):
        return None
    if isinstance(data, dict) and isinstance(data.get("elementPeriods"), dict):
        return data
    return None


async def _anonymous_login(client: httpx.AsyncClient, base_url: str, school: str) -> None:
    """Create a WebUntis anonymous session used for public timetables.

    The school timetable itself must be marked public by the school. No student
    password is used or stored by this script.
    """
    shared_secret_url = f"{base_url}/jsonrpc_intern.do"
    response = await client.post(
        shared_secret_url,
        params={"m": "getAppSharedSecret", "school": school, "v": "i3.5"},
        json={
            "id": "weather-bot-anon-init",
            "method": "getAppSharedSecret",
            "params": [{"userName": "#anonymous#", "password": ""}],
            "jsonrpc": "2.0",
        },
    )
    response.raise_for_status()
    try:
        init_payload = response.json()
    except ValueError as exc:
        raise RuntimeError("WebUntis did not return JSON during anonymous login initialization.") from exc
    if init_payload.get("error"):
        raise RuntimeError("WebUntis rejected anonymous public timetable access.")

    login_response = await client.post(
        f"{base_url}/jsonrpc_intern.do",
        params={"m": "getUserData2017", "school": school, "v": "i2.2"},
        json={
            "id": "weather-bot-anon-login",
            "method": "getUserData2017",
            "params": [{
                "auth": {
                    "clientTime": int(system_time.time() * 1000),
                    "user": "#anonymous#",
                    "otp": 100170,
                }
            }],
            "jsonrpc": "2.0",
        },
    )
    login_response.raise_for_status()
    try:
        login_payload = login_response.json()
    except ValueError as exc:
        raise RuntimeError("WebUntis did not return JSON during anonymous login.") from exc
    if login_payload.get("error") or not client.cookies.get("JSESSIONID"):
        raise RuntimeError("WebUntis did not create an anonymous session for public timetable access.")

    # Some WebUntis deployments require this companion cookie in addition to JSESSIONID.
    encoded_school = "_" + base64.b64encode(school.encode("utf-8")).decode("ascii")
    hostname = urlsplit(base_url).hostname
    client.cookies.set("schoolname", encoded_school, domain=hostname, path="/WebUntis")


async def _rpc_list(client: httpx.AsyncClient, base_url: str, school: str, method: str) -> list[dict]:
    """Best-effort lookup for human-readable subject, teacher and room names."""
    try:
        response = await client.post(
            f"{base_url}/jsonrpc.do",
            params={"school": school},
            json={"id": f"weather-bot-{method}", "method": method, "params": {}, "jsonrpc": "2.0"},
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result")
        return result if isinstance(result, list) else []
    except Exception:
        logger.info("Optional WebUntis lookup %s was unavailable; using timetable text fallbacks.", method)
        return []


def _make_lookup(items: list[dict]) -> dict[int, str]:
    lookup = {}
    for item in items:
        try:
            item_id = int(item.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        label = item.get("longName") or item.get("name") or item.get("displayname")
        if label:
            lookup[item_id] = " ".join(str(label).split())
    return lookup


def _time_label(value) -> str:
    try:
        raw = f"{int(value):04d}"
        return f"{raw[:2]}:{raw[2:]}"
    except (TypeError, ValueError):
        return "--:--"


def _date_as_yyyymmdd(value) -> str:
    if value is None:
        return ""
    return str(value).replace("-", "").replace("/", "")[:8]


def _period_labels(period: dict, subjects: dict[int, str], teachers: dict[int, str], rooms: dict[int, str]):
    names = {"subjects": [], "teachers": [], "rooms": []}
    for element in period.get("elements", []) or []:
        try:
            element_type = int(element.get("type"))
            element_id = int(element.get("id"))
        except (AttributeError, TypeError, ValueError):
            continue
        if element_type == 3 and element_id in subjects:
            names["subjects"].append(subjects[element_id])
        elif element_type == 2 and element_id in teachers:
            names["teachers"].append(teachers[element_id])
        elif element_type == 4 and element_id in rooms:
            names["rooms"].append(rooms[element_id])
    return names


async def build_timetable_message() -> str:
    """Fetch today's periods through the public WebUntis timetable endpoint."""
    public_url = os.getenv("WEBUNTIS_PUBLIC_URL", "").strip()
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

    # The user has no lessons on Saturdays or Sundays. Skip WebUntis entirely
    # on weekends, while the overall job still sends the weather message.
    if target_day.weekday() >= 5:
        return f"{header}\n🌴 На эту дату занятий по расписанию нет (выходной)."

    if not public_url:
        return (
            f"{header}\n"
            "⚠️ WebUntis пока не подключён. Добавь публичную ссылку в GitHub Secret "
            "`WEBUNTIS_PUBLIC_URL`."
        )

    try:
        base_url, school, element_type, entity_id = _parse_public_link(public_url)
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            # Try the public endpoint directly first. Some schools expose it without a session.
            params = {
                "elementType": element_type,
                "elementId": entity_id,
                "date": target_day.isoformat(),
                "formatId": 1,
            }
            endpoint = f"{base_url}/api/public/timetable/weekly/data"
            response = await client.get(endpoint, params=params)
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            data = _extract_timetable_data(payload) if response.is_success else None

            if data is None:
                # Official WebUntis public-timetable links can be opened without a student login;
                # its data endpoint may nevertheless need WebUntis's anonymous session.
                await _anonymous_login(client, base_url, school)
                response = await client.get(endpoint, params=params)
                response.raise_for_status()
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise RuntimeError("WebUntis returned a non-JSON response for the public timetable.") from exc
                data = _extract_timetable_data(payload)
                if data is None:
                    raise RuntimeError("WebUntis did not return the expected public timetable data.")

            elements_by_period = data.get("elementPeriods", {})
            all_periods = []
            seen = set()
            for periods in elements_by_period.values():
                if not isinstance(periods, list):
                    continue
                for period in periods:
                    if not isinstance(period, dict):
                        continue
                    if _date_as_yyyymmdd(period.get("date")) != target_day.strftime("%Y%m%d"):
                        continue
                    identity = (
                        period.get("id"), period.get("lessonId"), period.get("date"),
                        period.get("startTime"), period.get("endTime"),
                    )
                    if identity in seen:
                        continue
                    seen.add(identity)
                    all_periods.append(period)

            # These names are optional; timetable access remains usable if lookups are restricted.
            subjects_items, teachers_items, rooms_items = await asyncio.gather(
                _rpc_list(client, base_url, school, "getSubjects"),
                _rpc_list(client, base_url, school, "getTeachers"),
                _rpc_list(client, base_url, school, "getRooms"),
            )
            subjects = _make_lookup(subjects_items)
            teachers = _make_lookup(teachers_items)
            rooms = _make_lookup(rooms_items)

    except Exception as exc:
        logger.warning("Could not fetch WebUntis public timetable (%s).", type(exc).__name__)
        return (
            f"{header}\n"
            "⚠️ Не удалось получить расписание из публичной страницы WebUntis. Проверь секрет "
            "`WEBUNTIS_PUBLIC_URL` и доступность расписания без входа в аккаунт. "
            "Погода будет отправлена независимо от этой ошибки."
        )

    all_periods.sort(key=lambda p: (int(p.get("startTime") or 0), int(p.get("endTime") or 0)))
    if not all_periods:
        return f"{header}\n🎉 На сегодня занятий в публичном расписании нет."

    lines = [header, ""]
    for period in all_periods:
        start = _time_label(period.get("startTime"))
        end = _time_label(period.get("endTime"))
        labels = _period_labels(period, subjects, teachers, rooms)
        subject = ", ".join(dict.fromkeys(labels["subjects"]))
        if not subject:
            subject = str(period.get("lessonText") or period.get("periodText") or "Урок").strip()
        subject = " ".join(subject.replace("\n", " ").split())
        line = f"• *{escape_markdown(subject[:120], version=1)}* — {start}–{end}"

        if labels["rooms"]:
            rooms_text = ", ".join(dict.fromkeys(labels["rooms"]))
            line += f" · 🚪 {escape_markdown(rooms_text, version=1)}"
        if labels["teachers"]:
            teachers_text = ", ".join(dict.fromkeys(labels["teachers"]))
            line += f" · {escape_markdown(teachers_text, version=1)}"
        lines.append(line)

        note = str(period.get("substText") or period.get("periodText") or "").strip()
        if note and note.casefold() != subject.casefold() and len(note) <= 200:
            lines.append(f"  _{escape_markdown(' '.join(note.split()), version=1)}_")

    lines.extend(["", "_Источник: публичное расписание WebUntis_"])
    return "\n".join(lines)


async def main() -> None:
    weather_message = await build_weather_message()
    timetable_message = await build_timetable_message()
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
        await telegram_bot.send_message(
            chat_id=TELEGRAM_USER_ID,
            text=message,
            parse_mode="Markdown",
        )
    logger.info("Daily weather and timetable message sent successfully.")


if __name__ == "__main__":
    asyncio.run(main())
