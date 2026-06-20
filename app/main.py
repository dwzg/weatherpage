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
    pressure_trend = None
    extremes_today = None

    if current:
        dew_point = compute_dew_point(current["temperature"], current["humidity"])
        pressure_trend = await database.get_pressure_trend()

    if stats_today and stats_today.get("count", 0) > 0:
        extremes_today = await database.get_extremes_with_times("today")

    template = _jinja_env.get_template("index.html")
    html = template.render(
        request=request,
        current=current,
        stats_all=stats_all,
        stats_today=stats_today,
        dew_point=dew_point,
        pressure_trend=pressure_trend,
        extremes_today=extremes_today,
        now=datetime.now(tz=TIMEZONE).isoformat(),
    )
    return HTMLResponse(html)


@app.get("/api/weather/daily")
async def get_daily(months: int = Query(3, ge=1, le=24)):
    """Return daily summaries for the heatmap."""
    return await database.get_daily_summaries(months)
