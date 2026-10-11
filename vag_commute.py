"""Calculate a school commute using VAG's official PULS realtime API."""
from __future__ import annotations

import csv
import io
import logging
import math
import threading
import unicodedata
import zipfile
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


GTFS_URL = "https://www.vgn.de/opendata/GTFS.zip"
_GTFS_LOCK = threading.Lock()
_GTFS_CACHE: dict | None = None


def _load_gtfs() -> dict:
    """Load and cache the official VGN static GTFS timetable in memory."""
    global _GTFS_CACHE
    if _GTFS_CACHE is not None:
        return _GTFS_CACHE
    with _GTFS_LOCK:
        if _GTFS_CACHE is not None:
            return _GTFS_CACHE
        with httpx.Client(timeout=45.0, follow_redirects=True) as client:
            response = client.get(GTFS_URL)
            response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = set(archive.namelist())

            def rows(filename: str) -> list[dict[str, str]]:
                if filename not in names:
                    return []
                with archive.open(filename) as raw:
                    return list(csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig")))

            stops = rows("stops.txt")
            routes = rows("routes.txt")
            all_trips = rows("trips.txt")
            calendar = rows("calendar.txt")
            calendar_dates = rows("calendar_dates.txt")
            route_by_id = {row["route_id"]: row for row in routes}
            u2_route_ids = {
                row["route_id"] for row in routes
                if _norm(row.get("route_short_name", "")).replace(" ", "") == "u2"
                and (not row.get("route_type") or row.get("route_type") == "1")
            }
            trip_by_id = {
                row["trip_id"]: row for row in all_trips
                if row.get("route_id") in u2_route_ids
            }
            # stop_times.txt is the largest GTFS file. Keep only U2 rows so the
            # Cloud Run service stays within its 512 MiB memory limit.
            times_by_trip: dict[str, list[dict[str, str]]] = {}
            if "stop_times.txt" in names:
                with archive.open("stop_times.txt") as raw:
                    for row in csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig")):
                        trip_id = row.get("trip_id", "")
                        if trip_id in trip_by_id:
                            times_by_trip.setdefault(trip_id, []).append(row)

        origin_ids = {
            row["stop_id"] for row in stops
            if _norm(ORIGIN_NAME) in _norm(row.get("stop_name", ""))
        }
        destination_ids = {
            row["stop_id"] for row in stops
            if _norm(DESTINATION_NAME) in _norm(row.get("stop_name", ""))
        }
        for calls in times_by_trip.values():
            calls.sort(key=lambda row: int(row.get("stop_sequence", "0") or 0))

        _GTFS_CACHE = {
            "origin_ids": origin_ids,
            "destination_ids": destination_ids,
            "route_by_id": route_by_id,
            "trip_by_id": trip_by_id,
            "times_by_trip": times_by_trip,
            "calendar": {row["service_id"]: row for row in calendar},
            "calendar_dates": calendar_dates,
        }
        logger.info(
            "Loaded official VGN GTFS: origin_stops=%d destination_stops=%d trips=%d",
            len(origin_ids), len(destination_ids), len(trips)
        )
        return _GTFS_CACHE


def _gtfs_service_runs(data: dict, service_id: str, service_date) -> bool:
    date_key = service_date.strftime("%Y%m%d")
    exceptions = [
        row for row in data["calendar_dates"]
        if row.get("service_id") == service_id and row.get("date") == date_key
    ]
    if exceptions:
        return exceptions[-1].get("exception_type") == "1"
    rule = data["calendar"].get(service_id)
    if not rule:
        return False
    if not (rule.get("start_date", "00000000") <= date_key <= rule.get("end_date", "99999999")):
        return False
    return rule.get(service_date.strftime("%A").lower(), "0") == "1"


def _gtfs_clock(value: str, service_date) -> datetime | None:
    try:
        hours, minutes, seconds = (int(part) for part in value.split(":"))
        base = datetime.combine(service_date, time(0, 0), tzinfo=TIMEZONE)
        return base + timedelta(hours=hours, minutes=minutes, seconds=seconds)
    except (ValueError, TypeError):
        return None


def _gtfs_scheduled_trip(now: datetime, deadline: datetime) -> tuple[datetime, datetime] | None:
    """Find latest scheduled U2 trip to Rathenauplatz for the target service date."""
    data = _load_gtfs()
    service_date = deadline.date()
    matches: list[tuple[datetime, datetime]] = []
    for trip_id, trip in data["trip_by_id"].items():
        route = data["route_by_id"].get(trip.get("route_id", ""), {})
        if _norm(route.get("route_short_name", "")).replace(" ", "") != "u2":
            continue
        if route.get("route_type") and route.get("route_type") != "1":
            continue
        if not _gtfs_service_runs(data, trip.get("service_id", ""), service_date):
            continue
        calls = data["times_by_trip"].get(trip_id, [])
        origin_call = next((row for row in calls if row.get("stop_id") in data["origin_ids"]), None)
        if not origin_call:
            continue
        origin_sequence = int(origin_call.get("stop_sequence", "0") or 0)
        destination_call = next(
            (row for row in calls
             if row.get("stop_id") in data["destination_ids"]
             and int(row.get("stop_sequence", "0") or 0) > origin_sequence),
            None,
        )
        if not destination_call:
            continue
        headsign = _norm(trip.get("trip_headsign", ""))
        # Herrnhütte to Rathenauplatz requires U2 in the direction of Röthenbach.
        if headsign and "rothenbach" not in headsign:
            continue
        departure = _gtfs_clock(origin_call.get("departure_time", ""), service_date)
        arrival = _gtfs_clock(destination_call.get("arrival_time", ""), service_date)
        if not departure or not arrival:
            continue
        if departure < now + timedelta(minutes=WALK_TO_STATION_MINUTES + DEPARTURE_BUFFER_MINUTES):
            continue
        if arrival + timedelta(minutes=WALK_TO_SCHOOL_MINUTES) > deadline:
            continue
        matches.append((departure, arrival))
    return max(matches, key=lambda pair: pair[0]) if matches else None


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

    # Keep coarse counters so a no-match response can be diagnosed from Cloud Run logs.
    stop_count = 0
    departure_count = 0
    u2_count = 0
    in_window_count = 0
    trip_checked_count = 0

    try:
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            origin_stops = _stops(client, ORIGIN_NAME)
            stop_count = len(origin_stops)
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
                departures = payload.get("Abfahrten", []) or []
                departure_count += len(departures)
                for departure in departures:
                    line = _norm(departure.get("Linienname", departure.get("Linie", ""))).replace(" ", "")
                    product = _norm(departure.get("Produkt", "ubahn"))
                    if line not in {REQUIRED_LINE, "2"}:
                        continue
                    if product and not any(x in product for x in ("ubahn", "u-bahn", "subway")):
                        continue
                    u2_count += 1
                    has_prediction = departure.get("Prognose") is True
                    departure_dt = _parse_datetime(
                        (departure.get("AbfahrtszeitIst") if has_prediction else departure.get("AbfahrtszeitSoll"))
                        or departure.get("AbfahrtszeitSoll")
                        or departure.get("AbfahrtszeitIst")
                    )
                    if not departure_dt or not (
                        earliest_departure_to_catch <= departure_dt < latest_arrival_at_station
                    ):
                        continue
                    in_window_count += 1
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
                trip_checked_count += 1
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
                    real_time_available = departure.get("Prognose") is True
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

    # VAG PULS is primarily a live departure monitor and may not expose next-day
    # departures. Fall back to VGN's official static GTFS timetable.
    try:
        scheduled = _gtfs_scheduled_trip(now, deadline)
        if scheduled:
            departure_dt, arrival_dt = scheduled
            leave_time = departure_dt - timedelta(
                minutes=WALK_TO_STATION_MINUTES + DEPARTURE_BUFFER_MINUTES
            )
            minutes_until_leave = max(0, math.ceil((leave_time - now).total_seconds() / 60))
            time_until_departure = max(0, math.ceil((departure_dt - now).total_seconds() / 60))
            school_arrival = arrival_dt + timedelta(minutes=WALK_TO_SCHOOL_MINUTES)
            deadline_margin = max(0, math.floor((deadline - school_arrival).total_seconds() / 60))
            logger.info("Using VGN GTFS scheduled trip: departure=%s arrival=%s", departure_dt.isoformat(), arrival_dt.isoformat())
            return (
                "🚇 Расчёт поездки до школы\\n"
                f"Цель: {target_label}, быть в школе к {deadline:%H:%M}\\n\\n"
                f"🚪 Выйти из дома: *{leave_time:%H:%M}*"
                + (f" (через ~{minutes_until_leave} мин.)" if minutes_until_leave else " (пора выходить)")
                + f"\\n🚉 Herrnhütte: U2 → Röthenbach, отправление *{departure_dt:%H:%M}*"
                + f"\\n⏳ До отправления от текущего момента: ~{time_until_departure} мин."
                + f"\\n📍 Rathenauplatz: прибытие *{arrival_dt:%H:%M}*"
                + f"\\n🚶 До школы: {WALK_TO_SCHOOL_MINUTES} мин."
                + f"\\n🎓 У школы: *{school_arrival:%H:%M}*"
                + f"\\n✅ Запас до {deadline:%H:%M}: {deadline_margin} мин."
                + "\\n\\n_Источник: плановое расписание VGN (GTFS). Реальные задержки могут изменить время._"
            )
    except (httpx.HTTPError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        logger.exception("VGN GTFS fallback failed (%s)", type(exc).__name__)

    logger.warning(
        "VAG route had no match: stops=%d departures=%d u2=%d in_window=%d trips_checked=%d target=%s",
        stop_count, departure_count, u2_count, in_window_count, trip_checked_count, deadline.isoformat()
    )
    return (
        f"⚠️ Не нашёл подходящую поездку U2, чтобы прибыть к {deadline:%H:%M} "
        f"({target_label}).\n\n"
        "Если ты проверяешь маршрут вечером, VAG может не отдавать прогноз отправления на следующее утро. "
        "Попробуй утром повторно. Если сообщение повторится, пришли мне количество "
        "остановок/рейсов из логов Cloud Run — проверим ответ API.\n\n"
        "Проверь актуальные отправления в VAGstart: https://start.vag.de/"
    )
