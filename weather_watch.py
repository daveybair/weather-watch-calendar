import requests
import datetime as dt
from collections import defaultdict
from pathlib import Path
import uuid


# =========================
# CONFIG
# =========================

CONFIG = {
    # Default: Itasca / Chicago area. Change these for any jobsite.
    "location_name": "Itasca, IL",
    "latitude": 41.9750,
    "longitude": -88.0073,

    # Forecast range
    "forecast_days": 10,

    # Calendar output
    "ics_output": "weather_alerts.ics",

    # Buy-water warning lead time
    "water_lead_days": 2,

    # Thresholds
    # Lowered so you get warned earlier.
    "extreme_heat_index_f": 40,
    "hot_humid_heat_index_f": 35,

    # Wind warning thresholds
    "wind_gust_threshold_mph": 45,
    "sustained_wind_threshold_mph": 30,

    # Calendar subject prefix so you can search/delete them later.
    "event_prefix": "[WEATHER WATCH]",
}


THUNDERSTORM_CODES = {95, 96, 99}

NWS_ALERT_KEYWORDS = [
    "Excessive Heat",
    "Heat Advisory",
    "Extreme Heat",
    "Severe Thunderstorm",
    "Thunderstorm",
    "Tornado",
    "Wind Advisory",
    "High Wind",
    "Special Weather Statement",
]


# =========================
# WEATHER LOGIC
# =========================

def heat_index_f(temp_f: float, rh: float) -> float:
    """
    NWS heat index approximation.
    Works best for hot/humid conditions.
    """
    if temp_f < 80 or rh < 40:
        return temp_f

    hi = (
        -42.379
        + 2.04901523 * temp_f
        + 10.14333127 * rh
        - 0.22475541 * temp_f * rh
        - 0.00683783 * temp_f * temp_f
        - 0.05481717 * rh * rh
        + 0.00122874 * temp_f * temp_f * rh
        + 0.00085282 * temp_f * rh * rh
        - 0.00000199 * temp_f * temp_f * rh * rh
    )

    return round(hi, 1)


def fetch_open_meteo_forecast():
    url = "https://api.open-meteo.com/v1/forecast"

    params = {
        "latitude": CONFIG["latitude"],
        "longitude": CONFIG["longitude"],
        "forecast_days": CONFIG["forecast_days"],
        "timezone": "auto",
        "temperature_unit": "fahrenheit",
        "wind_speed_unit": "mph",
        "precipitation_unit": "inch",
        "hourly": ",".join([
            "temperature_2m",
            "relative_humidity_2m",
            "apparent_temperature",
            "weather_code",
            "wind_speed_10m",
            "wind_gusts_10m",
            "precipitation_probability",
        ]),
    }

    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def fetch_nws_active_alerts():
    """
    Pull official active NWS alerts for the same point.
    """
    url = "https://api.weather.gov/alerts/active"

    params = {
        "point": f"{CONFIG['latitude']},{CONFIG['longitude']}"
    }

    headers = {
        "User-Agent": "WeatherWarningSystem/1.0"
    }

    response = requests.get(url, params=params, headers=headers, timeout=20)
    response.raise_for_status()
    return response.json()


def analyze_forecast(forecast_json):
    hourly = forecast_json["hourly"]
    by_day = defaultdict(list)

    for i, timestamp in enumerate(hourly["time"]):
        day = dt.date.fromisoformat(timestamp[:10])

        temp_f = hourly["temperature_2m"][i]
        rh = hourly["relative_humidity_2m"][i]
        apparent_f = hourly["apparent_temperature"][i]
        weather_code = hourly["weather_code"][i]
        wind_mph = hourly["wind_speed_10m"][i]
        gust_mph = hourly["wind_gusts_10m"][i]
        precip_prob = hourly["precipitation_probability"][i]

        by_day[day].append({
            "temp_f": temp_f,
            "rh": rh,
            "apparent_f": apparent_f,
            "weather_code": weather_code,
            "wind_mph": wind_mph,
            "gust_mph": gust_mph,
            "precip_prob": precip_prob,
            "heat_index_f": heat_index_f(temp_f, rh),
        })

    events = []
    today = dt.date.today()

    for day, rows in sorted(by_day.items()):
        if day < today:
            continue

        max_temp = max(r["temp_f"] for r in rows)
        max_rh = max(r["rh"] for r in rows)
        max_heat_index = max(r["heat_index_f"] for r in rows)
        max_apparent = max(r["apparent_f"] for r in rows)
        max_wind = max(r["wind_mph"] for r in rows)
        max_gust = max(r["gust_mph"] for r in rows)

        precip_values = [
            r["precip_prob"]
            for r in rows
            if r["precip_prob"] is not None
        ]

        max_precip_prob = max(precip_values) if precip_values else 0

        thunder_hours = sum(
            1 for r in rows
            if r["weather_code"] in THUNDERSTORM_CODES
        )

        descriptions = []
        titles = []

        # Heat / humidity
        if (
            max_heat_index >= CONFIG["extreme_heat_index_f"]
            or max_apparent >= CONFIG["extreme_heat_index_f"]
        ):
            titles.append("EXTREME HEAT / HUMIDITY")
            descriptions.append(
                f"Max heat index: {max_heat_index}°F. "
                f"Max apparent temp: {max_apparent}°F. "
                f"Max temp: {max_temp}°F. Max humidity: {max_rh}%."
            )

            # Add separate buy-water event before the hot day
            buy_day = day - dt.timedelta(days=CONFIG["water_lead_days"])

            if buy_day >= today:
                events.append({
                    "date": buy_day,
                    "time": "08:00",
                    "duration_minutes": 30,
                    "title": "BUY WATER — heat/humidity coming",
                    "description": (
                        f"Heat/humidity is forecast for {day}. "
                        f"Expected max heat index: {max_heat_index}°F. "
                        f"Buy water early, check coolers, ice, shade, and electrolyte supplies."
                    ),
                })

        elif max_heat_index >= CONFIG["hot_humid_heat_index_f"]:
            titles.append("HOT / HUMID")
            descriptions.append(
                f"Max heat index: {max_heat_index}°F. "
                f"Max apparent temp: {max_apparent}°F. "
                f"Max temp: {max_temp}°F. Max humidity: {max_rh}%."
            )

        # Lightning / thunderstorm
        if thunder_hours > 0:
            titles.append("LIGHTNING / THUNDERSTORM RISK")
            descriptions.append(
                f"Thunderstorm weather code appears in {thunder_hours} forecast hour(s). "
                f"Max precipitation probability: {max_precip_prob}%."
            )

        # Wind
        if (
            max_gust >= CONFIG["wind_gust_threshold_mph"]
            or max_wind >= CONFIG["sustained_wind_threshold_mph"]
        ):
            titles.append("WINDY DAY")
            descriptions.append(
                f"Max wind: {max_wind} mph. Max gust: {max_gust} mph."
            )

        if titles:
            events.append({
                "date": day,
                "all_day": True,
                "title": " + ".join(titles),
                "description": "\n".join(descriptions),
            })

    return events


def analyze_nws_alerts(alerts_json):
    events = []

    for feature in alerts_json.get("features", []):
        props = feature.get("properties", {})
        alert_name = props.get("event", "")
        headline = props.get("headline", "")
        description = props.get("description", "")
        effective = props.get("effective")

        if not alert_name:
            continue

        if not any(
            keyword.lower() in alert_name.lower()
            for keyword in NWS_ALERT_KEYWORDS
        ):
            continue

        if effective:
            effective_dt = dt.datetime.fromisoformat(
                effective.replace("Z", "+00:00")
            )
            event_date = effective_dt.date()
        else:
            event_date = dt.date.today()

        events.append({
            "date": event_date,
            "all_day": True,
            "title": f"OFFICIAL NWS ALERT — {alert_name}",
            "description": f"{headline}\n\n{description[:1000]}",
        })

    return events


# =========================
# CALENDAR / ICS OUTPUT
# =========================

def ics_escape(text: str) -> str:
    text = str(text)
    return (
        text.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def format_ics_date(day: dt.date) -> str:
    return day.strftime("%Y%m%d")


def format_ics_datetime(day: dt.date, time_str: str) -> str:
    hour, minute = map(int, time_str.split(":"))
    return dt.datetime(
        day.year,
        day.month,
        day.day,
        hour,
        minute
    ).strftime("%Y%m%dT%H%M%S")


def create_ics(events):
    now_utc = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Weather Warning System//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]

    seen = set()

    for event in events:
        key = (event["date"], event["title"])

        if key in seen:
            continue

        seen.add(key)

        full_title = f"{CONFIG['event_prefix']} {event['title']}"

        description = (
            f"Location: {CONFIG['location_name']}\n\n"
            f"{event['description']}"
        )

        lines.append("BEGIN:VEVENT")
        lines.append(
            f"UID:{uuid.uuid5(uuid.NAMESPACE_DNS, str(key))}@weather-watch"
        )
        lines.append(f"DTSTAMP:{now_utc}")
        lines.append(f"SUMMARY:{ics_escape(full_title)}")
        lines.append(f"DESCRIPTION:{ics_escape(description)}")
        lines.append("CATEGORIES:Weather Warning")

        if event.get("all_day", False):
            start = format_ics_date(event["date"])
            end = format_ics_date(event["date"] + dt.timedelta(days=1))

            lines.append(f"DTSTART;VALUE=DATE:{start}")
            lines.append(f"DTEND;VALUE=DATE:{end}")

        else:
            start_dt = format_ics_datetime(
                event["date"],
                event.get("time", "08:00")
            )

            end_time = (
                dt.datetime.strptime(event.get("time", "08:00"), "%H:%M")
                + dt.timedelta(minutes=event.get("duration_minutes", 30))
            ).strftime("%H:%M")

            end_dt = format_ics_datetime(event["date"], end_time)

            lines.append(f"DTSTART:{start_dt}")
            lines.append(f"DTEND:{end_dt}")

            lines.append("BEGIN:VALARM")
            lines.append("TRIGGER:-PT0M")
            lines.append("ACTION:DISPLAY")
            lines.append(f"DESCRIPTION:{ics_escape(full_title)}")
            lines.append("END:VALARM")

        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")

    output_path = Path(CONFIG["ics_output"])
    output_path.write_text("\n".join(lines), encoding="utf-8")

    return output_path


def main():
    print(f"Checking weather for {CONFIG['location_name']}...")

    all_events = []

    try:
        forecast = fetch_open_meteo_forecast()
        forecast_events = analyze_forecast(forecast)
        all_events.extend(forecast_events)
        print(f"Forecast events found: {len(forecast_events)}")

    except Exception as e:
        print(f"Open-Meteo forecast failed: {e}")

    try:
        nws_alerts = fetch_nws_active_alerts()
        nws_events = analyze_nws_alerts(nws_alerts)
        all_events.extend(nws_events)
        print(f"NWS alert events found: {len(nws_events)}")

    except Exception as e:
        print(f"NWS alerts failed: {e}")

    if not all_events:
        print("No dangerous weather events found.")
        return

    output_path = create_ics(all_events)

    print(f"Created calendar file: {output_path.resolve()}")

    print("\nEvents:")
    for event in sorted(all_events, key=lambda e: (e["date"], e["title"])):
        print(f"- {event['date']}: {event['title']}")


if __name__ == "__main__":
    main()