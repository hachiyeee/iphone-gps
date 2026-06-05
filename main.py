from __future__ import annotations
import asyncio
import json
import math
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import gpxpy
import uvicorn
from fastapi import FastAPI, Form, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pymobiledevice3.tunneld.api import get_tunneld_devices
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

# ── State ──────────────────────────────────────────────────────────────────────

class AppState:
    lat: Optional[float] = None
    lon: Optional[float] = None
    speed: float = 5.0
    playing: bool = False
    progress: float = 0.0
    error: str = ""

state = AppState()
play_task: Optional[asyncio.Task] = None
clients: set[WebSocket] = set()

# ── Device connection (persistent, reused across all set() calls) ─────────────

_location_sim: Optional[LocationSimulation] = None
_dvt_ctx: Optional[DvtProvider] = None

async def _connect() -> None:
    global _location_sim, _dvt_ctx
    devices = await get_tunneld_devices()
    if not devices:
        raise RuntimeError("找不到設備 — 請確認 tunneld 正在執行且手機已連接")
    rsd = devices[0]
    _dvt_ctx = DvtProvider(rsd)
    dvt = await _dvt_ctx.__aenter__()
    loc_ctx = LocationSimulation(dvt)
    _location_sim = await loc_ctx.__aenter__()

async def _ensure_connected() -> LocationSimulation:
    global _location_sim
    if _location_sim is None:
        await _connect()
    return _location_sim  # type: ignore

async def _reset_connection() -> None:
    global _location_sim, _dvt_ctx
    _location_sim = None
    if _dvt_ctx is not None:
        try:
            await _dvt_ctx.__aexit__(None, None, None)
        except Exception:
            pass
        _dvt_ctx = None


# ── Location worker (debounced — always sends the latest coordinate) ───────────

# maxsize=1: schedule_set() drops the old pending coord and replaces with newest
_coord_queue: asyncio.Queue[tuple[float, float]] = asyncio.Queue(maxsize=1)


def schedule_set(lat: float, lon: float) -> None:
    # Discard stale pending coord so we always forward the most recent one
    while not _coord_queue.empty():
        try:
            _coord_queue.get_nowait()
        except asyncio.QueueEmpty:
            break
    try:
        _coord_queue.put_nowait((lat, lon))
    except asyncio.QueueFull:
        pass


async def location_worker() -> None:
    while True:
        lat, lon = await _coord_queue.get()
        try:
            sim = await _ensure_connected()
            await sim.set(lat, lon)
            state.error = ""
        except Exception as e:
            state.error = str(e)
            await _reset_connection()


# ── Broadcast loop (push state to all connected browsers) ─────────────────────

async def broadcast_loop() -> None:
    while True:
        await asyncio.sleep(0.1)
        if not clients:
            continue
        msg = json.dumps({
            "lat": state.lat,
            "lon": state.lon,
            "playing": state.playing,
            "progress": state.progress,
            "error": state.error,
            "speed": state.speed,
        })
        dead: set[WebSocket] = set()
        for ws in clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        clients.difference_update(dead)


# ── Route playback ─────────────────────────────────────────────────────────────

def _build_segments(coords: list[tuple[float, float]]):
    segs = []
    for i in range(len(coords) - 1):
        a, b = coords[i], coords[i + 1]
        dlat = (b[0] - a[0]) * 111_000
        dlon = (b[1] - a[1]) * 111_000 * math.cos(math.radians(a[0]))
        segs.append((a, b, math.hypot(dlat, dlon)))
    return segs


def _interpolate(segs, meters: float) -> tuple[float, float]:
    remaining = meters
    for a, b, dist in segs:
        if remaining <= dist:
            t = remaining / dist if dist > 0 else 0.0
            return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
        remaining -= dist
    return segs[-1][1]


async def run_route(
    coords: list[tuple[float, float]],
    repeat: int = 1,
    bounce: bool = False,
) -> None:
    state.playing = True
    state.progress = 0.0
    repeat = max(1, repeat)
    interval = 0.3

    # Pre-build both directions so we don't rebuild every iteration
    segs_fwd = _build_segments(coords)
    total_fwd = sum(s[2] for s in segs_fwd)
    segs_bwd = _build_segments(list(reversed(coords)))
    total_bwd = sum(s[2] for s in segs_bwd)

    try:
        for i in range(repeat):
            # Odd iterations go backward when bounce is on
            segs, total = (segs_bwd, total_bwd) if (bounce and i % 2 == 1) else (segs_fwd, total_fwd)
            traveled = 0.0
            while traveled < total:
                traveled = min(traveled + state.speed * interval, total)
                state.progress = (i + (traveled / total if total > 0 else 1)) / repeat
                coord = _interpolate(segs, traveled)
                state.lat, state.lon = coord
                schedule_set(coord[0], coord[1])
                await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
    finally:
        state.playing = False
        state.progress = 0.0


# ── Lifespan ───────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(location_worker())
    asyncio.create_task(broadcast_loop())
    yield


app = FastAPI(lifespan=lifespan)


# ── REST API ───────────────────────────────────────────────────────────────────

class CoordBody(BaseModel):
    lat: float
    lon: float


class SpeedBody(BaseModel):
    speed: float


@app.post("/api/set")
async def api_set(body: CoordBody):
    state.lat, state.lon = body.lat, body.lon
    schedule_set(body.lat, body.lon)
    return {"ok": True}


@app.post("/api/clear")
async def api_clear():
    try:
        sim = await _ensure_connected()
        await sim.clear()
        state.error = ""
    except Exception as e:
        state.error = str(e)
        await _reset_connection()
    return {"ok": True}


@app.post("/api/speed")
async def api_speed(body: SpeedBody):
    state.speed = max(0.1, body.speed)
    return {"ok": True}


@app.post("/api/play")
async def api_play(
    file: UploadFile = File(...),
    repeat: int = Form(1),
    bounce: bool = Form(False),
):
    global play_task
    content = await file.read()
    try:
        gpx = gpxpy.parse(content.decode())
    except Exception as e:
        raise HTTPException(400, f"GPX 解析失敗: {e}")

    coords: list[tuple[float, float]] = []
    for track in gpx.tracks:
        for seg in track.segments:
            for pt in seg.points:
                coords.append((pt.latitude, pt.longitude))
    for wp in gpx.waypoints:
        coords.append((wp.latitude, wp.longitude))
    for route in gpx.routes:
        for pt in route.points:
            coords.append((pt.latitude, pt.longitude))

    if len(coords) < 2:
        raise HTTPException(400, "GPX 至少需要 2 個點")

    if play_task and not play_task.done():
        play_task.cancel()
        await asyncio.sleep(0.05)

    play_task = asyncio.create_task(run_route(coords, repeat=repeat, bounce=bounce))
    return {"ok": True, "points": len(coords)}


@app.post("/api/stop")
async def api_stop():
    global play_task
    if play_task and not play_task.done():
        play_task.cancel()
    state.playing = False
    state.progress = 0.0
    return {"ok": True}


# ── WebSocket (joystick real-time moves + state broadcast) ────────────────────

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") == "move":
                state.lat, state.lon = data["lat"], data["lon"]
                schedule_set(data["lat"], data["lon"])
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


# ── Static files ───────────────────────────────────────────────────────────────

app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8080, reload=True)
