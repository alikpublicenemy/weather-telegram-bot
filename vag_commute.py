"""Calculate a school commute using VAG's official PULS realtime API."""
from __future__ import annotations

import logging
import math
import unicodedata
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://start.vag.de/dm/api"
TIMEZONE = ZoneInfo("Europe/Berlin")
ORIGIN_NAME = "Herrnhütte"
DESTINATION_NAME = "Rathenauplatz"
WALK_TO_STATION_MINUTES = 3
DEPARTURE_BUFFER_MINUTES = 2
WALK_TO_SCHOOL_MINUTES = 5
SCHOOL_ARRIVAL_DEADLINE = time(8, 0)
REQUIRED_LINE = "u2"
REQUIRED_DIRECTION = "röthenbach"


class VAGApiError(RuntimeError):
    """The VAG API could not supply enough data for a safe calculation."""


def _norm(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return "".join(ch for ch in text if not unicodedata.combining(ch)).replace("ß", "ss").strip()


def _get_json(client: httpx.Client, path: str, params: dict | None = None) -> dict:
    response = client.get(f"{API_BASE}/{path.lstrip('/')}", params=params)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise VAGApiError("VAG API returned an unexpected response format")
    return payload


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TIMEZONE)
        return parsed.astimezone(TIMEZONE)
    except (TypeError, ValueError):
        return None


def _stops(client: httpx.Client, name: str) -> list[tuple[str, str]]:
    """Return (network, stop ID) candidates from the official stop lookup."""
    found: list[tuple[str, str]] = []
    normalized_name = _norm(name)
    for network in ("vag", "vgn"):
        try:
            payload = _get_json(client, f"haltestellen.json/{network}", {"name": name})
        except (httpx.HTTPError, ValueError, VAGApiError) as exc:
            logger.warning("VAG stop lookup failed for %s/%s (%s)", network, name, type(exc).__name__)
            continue
        for stop in payload.get("Haltestellen", []) or []:
            stop_name = _norm(stop.get("Haltestellenname", ""))
            if normalized_name not in stop_name:
                continue
            stop_id = stop.get("VAGKennung") if network == "vag" else stop.get("VGNKennung")
            if stop_id is not None and str(stop_id).strip():
                pair = (network, str(stop_id).strip())
                if pair not in found:
                    found.append(pair)
    if not found:
        raise VAGApiError(f"Не удалось найти остановку «{name}» в справочнике VAG/VGN.")
    found.sort(key=lambda pair: 0 if pair[0] == "vag" else 1)
    return found


def _arrival_in_trip(
    client: httpx.Client,
    trip: dict,
    target_stop_names: tuple[str, ...],
) -> datetime | None:
    trip_number = trip.get("Fahrtnummer") or trip.get("FahrtNummer") or trip.get("FahrtNr")
    service_date = trip.get("Betriebstag")
    if not service_date:
        service_date = datetime.now(TIMEZONE).date().isoformat()
    if not trip_number:
        return None

    payload = _get_json(client, f"fahrten.json/ubahn/{service_date}/{trip_number}")
    target_tokens = tuple(_norm(n) for n in target_stop_names)
    stops = payload.get("Fahrtverlauf", []) or []
    seen_origin = False
    for stop in stops:
        stop_name = _norm(stop.get("Haltestellenname", ""))
        if _norm(ORIGIN_NAME) in stop_name:
            seen_origin = True
        if not seen_origin:
            continue
        if any(token in stop_name for token in target_tokens):
            for field in (
                "AnkunftszeitIst",
                "AbfahrtszeitIst",
                "AnkunftszeitSoll",
                "AbfahrtszeitSoll",
            ):
                parsed = _parse_datetime(stop.get(field))
                if parsed:
                    return parsed
            return None
    return None


def _target_deadline(now: datetime) -> datetime:
    """Use today's 08:00 before school time, otherwise the next weekday at 08:00."""
    target_date = now.date()
    today_deadline = datetime.combine(target_date, SCHOOL_ARRIVAL_DEADLINE, tzinfo=TIMEZONE)
    if now < today_deadline and now.weekday() < 5:
        return today_deadline
    target_date += timedelta(days=1)
    while target_date.weekday() >= 5:
        target_date += timedelta(days=1)
    return datetime.combine(target_date, SCHOOL_ARRIVAL_DEADLINE, tzinfo=TIMEZONE)


def build_commute_message(now: datetime | None = None) -> str:
    """Find the latest U2 departure that can get the user to school by 08:00."""
    now = (now or datetime.now(TIMEZONE)).astimezone(TIMEZONE)
    deadline = _target_deadline(now)
    if (deadline.date() - now.date()).days > 1:
        target_label = deadline.strftime("%d.%m.%Y")
    else:
        target_label = "сегодня" if deadline.date() == now.date() else "следующий учебный день"

    earliest_departure_to_catch = now + timedelta(
        minutes=WALK_TO_STATION_MINUTES + DEPARTURE_BUFFER_MINUTES
    )
    latest_arrival_at_station = deadline - timedelta(minutes=WALK_TO_SCHOOL_MINUTES)
    # Focus the query on the final hour before the arrival deadline. PULS supports
    # TimeDelay, so an evening test press can query tomorrow morning rather than
    # returning dozens of unrelated departures from tonight.
    query_start = max(
        earliest_departure_to_catch,
        latest_arrival_at_station - timedelta(minutes=60),
    )
    delay_minutes = max(0, math.floor((query_start - now).total_seconds() / 60))
    query_span = max(
        1,
        math.ceil((latest_arrival_at_station - query_start).total_seconds() / 60),
    )

    if query_start >= latest_arrival_at_station:
        return "⚠️ Нет времени на безопасную поездку до указанного времени прибытия."

    try:
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            origin_stops = _stops(client, ORIGIN_NAME)
            candidates: list[tuple[datetime, dict]] = []
            for network, stop_id in origin_stops:
                try:
                    payload = _get_json(
                        client,
                        f"abfahrten.json/{network}/{stop_id}",
                        {
                            "produkt": "ubahn",
                            "timedelay": delay_minutes,
                            "timespan": query_span,
                            "limitcount": 60,
                        },
                    )
                except (httpx.HTTPError, ValueError, VAGApiError) as exc:
                    logger.warning("VAG departures failed at %s (%s)", stop_id, type(exc).__name__)
                    continue
                for departure in payload.get("Abfahrten", []) or []:
                    line = _norm(departure.get("Linienname", departure.get("Linie", ""))).replace(" ", "")
                    product = _norm(departure.get("Produkt", "ubahn"))
                    direction = _norm(
                        f"{departure.get('Richtung', '')} {departure.get('Richtungstext', '')}"
                    )
                    if line not in {REQUIRED_LINE, "2"}:
                        continue
                    if product and not any(x in product for x in ("ubahn", "u-bahn", "subway")):
                        continue
                    if _norm(REQUIRED_DIRECTION) not in direction and "rothenbach" not in direction:
                        continue
                    departure_dt = _parse_datetime(
                        departure.get("AbfahrtszeitIst") or departure.get("AbfahrtszeitSoll")
                    )
                    if not departure_dt or not (
                        earliest_departure_to_catch <= departure_dt < latest_arrival_at_station
                    ):
                        continue
                    if not (
                        departure.get("Fahrtnummer")
                        or departure.get("FahrtNummer")
                        or departure.get("FahrtNr")
                    ):
                        continue
                    candidates.append((departure_dt, departure))

            candidates.sort(key=lambda item: item[0], reverse=True)
            examined: set[tuple[str, str]] = set()
            for departure_dt, departure in candidates:
                trip_number = str(
                    departure.get("Fahrtnummer")
                    or departure.get("FahrtNummer")
                    or departure.get("FahrtNr")
                )
                service_day = str(
                    departure.get("Betriebstag") or departure_dt.date().isoformat()
                )
                trip_key = (service_day, trip_number)
                if trip_key in examined:
                    continue
                examined.add(trip_key)
                try:
                    arrival_dt = _arrival_in_trip(
                        client,
                        {**departure, "Betriebstag": service_day},
                        (DESTINATION_NAME,),
                    )
                except (httpx.HTTPError, ValueError, VAGApiError) as exc:
                    logger.warning("VAG trip lookup failed for %s (%s)", trip_number, type(exc).__name__)
                    continue
                if not arrival_dt:
                    continue
                school_arrival = arrival_dt + timedelta(minutes=WALK_TO_SCHOOL_MINUTES)
                if school_arrival <= deadline:
                    leave_time = departure_dt - timedelta(
                        minutes=WALK_TO_STATION_MINUTES + DEPARTURE_BUFFER_MINUTES
                    )
                    minutes_until_leave = max(
                        0, math.ceil((leave_time - now).total_seconds() / 60)
                    )
                    time_until_departure = max(
                        0, math.ceil((departure_dt - now).total_seconds() / 60)
                    )
                    deadline_margin = max(
                        0, math.floor((deadline - school_arrival).total_seconds() / 60)
                    )
                    real_time_available = bool(departure.get("AbfahrtszeitIst"))
                    departure_source = "прогноз VAG" if real_time_available else "расписание VAG"
                    when = deadline.strftime("%H:%M")
                    return (
                        "🚇 Расчёт поездки до школы\n"
                        f"Цель: {target_label}, быть в школе к {when}\n\n"
                        f"🚪 Выйти из дома: *{leave_time:%H:%M}*"
                        + (f" (через ~{minutes_until_leave} мин.)" if minutes_until_leave else " (пора выходить)")
                        + f"\n🚉 Herrnhütte: U2 → Röthenbach, отправление *{departure_dt:%H:%M}*"
                        + f"\n⏳ До отправления от текущего момента: ~{time_until_departure} мин."
                        + f"\n📍 Rathenauplatz: прибытие *{arrival_dt:%H:%M}*"
                        + f"\n🚶 До школы: {WALK_TO_SCHOOL_MINUTES} мин."
                        + f"\n🎓 У школы: *{school_arrival:%H:%M}*"
                        + f"\n✅ Запас до {when}: {deadline_margin} мин."
                        + f"\n\n_Отправление по данным: {departure_source}. Время поездки взято из данных конкретного рейса VAG._"
                    )
    except (httpx.HTTPError, ValueError, VAGApiError) as exc:
        logger.exception("Could not calculate VAG route (%s)", type(exc).__name__)
        return (
            "⚠️ Сейчас не удалось получить маршрут из системы VAG.\n\n"
            "Попробуй нажать кнопку ещё раз через минуту или проверь отправления в официальном VAGstart: https://start.vag.de/"
        )

    return (
        f"⚠️ Не нашёл подходящую поездку U2, чтобы прибыть к {deadline:%H:%M} "
        f"({target_label}).\n\n"
        "Проверь актуальные отправления Herrnhütte в VAGstart: https://start.vag.de/"
    )
