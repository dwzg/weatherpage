import math
import os
from contextlib import asynccontextmanager
from datetime import datetime

from .database import TIMEZONE

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import database

API_KEY = os.environ.get("API_KEY")


def compute_dew_point(temp_c: float, humidity: float) -> float:
    """Magnus formula for dew point temperature."""
    a, b = 17.27, 237.7
    gamma = (a * temp_c) / (b + temp_c) + math.log(humidity / 100.0)
    return round((b * gamma) / (a - gamma), 1)


def compute_heat_index(temp_c: float, humidity: float) -> float | None:
    """Heat index (feels-like) using the NOAA formula. Returns None below 27°C."""
    if temp_c < 27:
        return None  # heat index only meaningful at warm temps
    t = temp_c * 9 / 5 + 32  # to Fahrenheit
    rh = humidity
    hi = (0.5 * (t + 61.0 + (t - 68.0) * 1.2 + rh * 0.094))
    # Full Rothfusz regression
    hi = -42.379 + 2.04901523 * t + 10.14333127 * rh \
         - 0.22475541 * t * rh - 6.83783e-3 * t * t \
         - 5.481717e-2 * rh * rh + 1.22874e-3 * t * t * rh \
         + 8.5282e-4 * t * rh * rh - 1.99e-6 * t * t * rh * rh
    return round((hi - 32) * 5 / 9, 1)


def compute_forecast(pressure_trend: dict | None, humidity: float) -> str:
    """Simple forecast based on pressure trend and humidity."""
    if not pressure_trend:
        return "Not enough data"
    d = pressure_trend["direction"]
    rh = humidity
    if d == "falling" and rh > 60:
        return "Rain likely"
    elif d == "falling" and rh > 40:
        return "Rain possible"
    elif d == "falling":
        return "Weather worsening"
    elif d == "rising" and rh > 70:
        return "Humid but clearing"
    elif d == "rising":
        return "Clearing up"
    else:
        return "Stable conditions"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.init_db()
    yield


app = FastAPI(lifespan=lifespan, title="Weather Page")
_jinja_env = Environment(
    loader=FileSystemLoader("app/templates"),
    autoescape=select_autoescape(["html"]),
)


# ── API endpoints ──────────────────────────────────────────────


@app.post("/api/weather")
async def post_weather(data: dict, request: Request):
    """Receive weather data. Requires X-API-Key header if API_KEY is configured."""
    if API_KEY and request.headers.get("X-API-Key") != API_KEY:
        raise HTTPException(status_code=401, detail="invalid API key")
    await database.insert_reading(
        temperature=float(data["temperature"]),
        humidity=float(data["humidity"]),
        pressure=float(data["pressure"]),
        timestamp=data["timestamp"],
    )
    return {"status": "ok"}


@app.get("/api/weather/current")
async def get_current():
    """Return the most recent reading."""
    reading = await database.get_current()
    return reading or {"error": "no data"}


@app.get("/api/weather/status")
async def get_status():
    """Return all dashboard data for live polling: current, computed values, sparkline data."""
    current = await database.get_current()
    if not current:
        return {"error": "no data"}

    t = current["temperature"]
    h = current["humidity"]
    dew_point = compute_dew_point(t, h)
    heat_index = compute_heat_index(t, h)
    pressure_trend = await database.get_pressure_trend()
    forecast = compute_forecast(pressure_trend, h)
    frost_warning = t < 2.0
    yesterday = await database.get_reading_ago(24)
    spark_data = await database.get_history("3h")

    return {
        "current": current,
        "dew_point": dew_point,
        "heat_index": heat_index,
        "pressure_trend": pressure_trend,
        "forecast": forecast,
        "frost_warning": frost_warning,
        "yesterday": yesterday,
        "spark_data": spark_data,
    }


@app.get("/api/weather/history")
async def get_history(period: str = Query("24h", pattern="^(24h|7d|30d|all|today)$")):
    """Return all readings for the given period."""
    return await database.get_history(period)


@app.get("/api/weather/stats")
async def get_stats(period: str = Query("24h", pattern="^(24h|7d|30d|all|today)$")):
    """Return min/max/avg stats for the given period."""
    return await database.get_stats(period)


# ── UI ─────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def serve_ui(request: Request):
    """Serve the weather dashboard."""
    current = await database.get_current()
    stats_all = await database.get_stats("all")
    stats_today = await database.get_stats("today")

    dew_point = None
    heat_index = None
    pressure_trend = None
    extremes_today = None
    forecast = None
    frost_warning = False
    yesterday = None

    if current:
        t = current["temperature"]
        h = current["humidity"]
        dew_point = compute_dew_point(t, h)
        heat_index = compute_heat_index(t, h)
        pressure_trend = await database.get_pressure_trend()
        forecast = compute_forecast(pressure_trend, h)
        if t < 2.0:
            frost_warning = True
        yesterday = await database.get_reading_ago(24)

    extremes_all = None
    daily_extremes = None
    climate = None
    if stats_today and stats_today.get("count", 0) > 0:
        extremes_today = await database.get_extremes_with_times("today")
    if stats_all and stats_all.get("count", 0) > 0:
        extremes_all = await database.get_extremes_with_times("all")
        daily_extremes = await database.get_daily_extremes()
        climate = await database.get_climate_stats()

    template = _jinja_env.get_template("index.html")
    html = template.render(
        request=request,
        current=current,
        stats_all=stats_all,
        stats_today=stats_today,
        dew_point=dew_point,
        heat_index=heat_index,
        pressure_trend=pressure_trend,
        extremes_today=extremes_today,
        extremes_all=extremes_all,
        daily_extremes=daily_extremes,
        climate=climate,
        current_year=datetime.now(tz=TIMEZONE).year,
        forecast=forecast,
        frost_warning=frost_warning,
        yesterday=yesterday,
        now=datetime.now(tz=TIMEZONE).isoformat(),
    )
    return HTMLResponse(html)


@app.get("/api/weather/daily")
async def get_daily(months: int = Query(3, ge=1, le=24)):
    """Return daily summaries for the heatmap."""
    return await database.get_daily_summaries(months)
