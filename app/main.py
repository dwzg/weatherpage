import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import database

API_KEY = os.environ.get("API_KEY")


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
async def get_history(period: str = Query("24h", pattern="^(24h|7d|30d|all)$")):
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
    template = _jinja_env.get_template("index.html")
    html = template.render(
        current=current,
        stats_all=stats_all,
        stats_today=stats_today,
        now=datetime.utcnow().isoformat(),
    )
    return HTMLResponse(html)
