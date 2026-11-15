"""FastAPI web server: REST API + WebSocket with live data + static UI."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__, about
from ..core import DEFAULT_DASHBOARD, ConnectionSettings, ObdError, ObdSession, find_ports
from ..core.health import HealthReport, run_check
from ..core.pids import DEFAULT_TILES
from ..core.report_html import render_html
from ..core import supporter, usage
from ..core.storage import FUELS, Storage, StorageError
from ..core.vin import decode as vin_decode
from ..core.trip import LiveReader, TripComputer

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class SupporterBody(BaseModel):
    key: str


class AppState:
    """Holds the single OBD session shared by all clients."""

    def __init__(self, settings: ConnectionSettings, storage: Optional[Storage] = None):
        self.settings = settings
        self.storage = storage or Storage()
        self.trip = TripComputer(fuel=self.storage.active().get("fuel", "petrol"))
        self.session = ObdSession(settings)
        self.reader = LiveReader(self.session, self.trip)
        self.state = "disconnected"  # disconnected | connecting | connected | reconnecting | error
        self.error: Optional[str] = None
        self.auto_reconnect = False     # True after a connect request, False after "Disconnect"
        self.attempt = 0                # reconnect attempts since the link was lost
        self._next_try = 0.0
        self._lock = threading.Lock()
        self.last_report: Optional[HealthReport] = None
        self.health_running = False

    def connect(self, settings: Optional[ConnectionSettings] = None) -> None:
        self.auto_reconnect = True
        with self._lock:
            if settings is not None:
                self.settings = settings
                self.session.close()
                self.session = ObdSession(settings)
            self.reader = LiveReader(self.session, self.trip)
            self.state, self.error = "connecting", None
        try:
            self.session.connect()
        except Exception as e:  # report any failure to the UI
            log.warning("Connect failed: %s", e)
            self.state, self.error = "error", str(e)
            raise
        self.reader = LiveReader(self.session, self.trip)
        self._remember_vehicle()
        self.state = "connected"

    def _remember_vehicle(self) -> None:
        """Pick the profile of the connected car and note the settings that worked."""
        try:
            info = self.session.adapter_info()
            vehicle = self.storage.select_for_vin(self.session.vin(), simulated=self.session.simulated,
                                                  port=info.port)
            if not self.session.simulated:
                self.storage.update(vehicle["id"], connection={
                    "port": info.port, "baudrate": self.settings.baudrate, "protocol": info.protocol_id,
                    "protocol_name": info.protocol})
            self.trip.fuel = vehicle.get("fuel", "petrol")
        except Exception as e:  # profiles are a convenience, never a reason to fail the connection
            log.warning("Could not update the vehicle profile: %s", e)

    @property
    def vehicle(self) -> dict:
        return self.storage.active()

    def disconnect(self) -> None:
        self.auto_reconnect = False
        self.session.close()
        self.state, self.error = "disconnected", None

    RETRY_DELAYS = (5, 5, 5, 15, 15, 30)  # seconds before attempt 1, 2, ...; then every 30 s

    def watchdog_step(self, now: Optional[float] = None) -> None:
        """Notice a lost link and reconnect with back-off (called every few seconds by a thread)."""
        now = time.monotonic() if now is None else now
        if self.state == "connected" and not self.session.connected:
            self.state, self.error = "error", self.session.last_error or "Connection to the adapter was lost"
        if not self.auto_reconnect or self.health_running or self.state not in ("error", "reconnecting"):
            if self.state == "connected":
                self.attempt = 0
            return
        if self.attempt == 0 and self.state == "error":  # first notice: schedule attempt 1
            self.state = "reconnecting"
            self._next_try = now + self.RETRY_DELAYS[0]
            self.attempt = 1
            return
        if now < self._next_try:
            return
        reason = self.error
        try:
            self.connect()
            self.attempt = 0
            log.info("Reconnected to the adapter")
        except Exception:
            delay = self.RETRY_DELAYS[min(self.attempt, len(self.RETRY_DELAYS) - 1)]
            self.attempt += 1
            self.state, self.error = "reconnecting", self.error or reason
            self._next_try = time.monotonic() + delay

    def check(self) -> ObdSession:
        if self.state != "connected" or not self.session.connected:
            if self.state == "connected":
                self.state, self.error = "error", self.session.last_error or "Connection lost"
            raise HTTPException(409, detail=self.error or "Not connected to the car")
        return self.session

    def to_dict(self) -> dict:
        s = self.settings
        return {
            "state": self.state,
            "error": self.error,
            "settings": {"port": s.port, "baudrate": s.baudrate, "protocol": s.protocol, "simulate": s.simulate},
            "attempt": self.attempt,
            "version": __version__,
            "vehicle": self.vehicle,
        }


class ConnectRequest(BaseModel):
    port: Optional[str] = None
    baudrate: Optional[int] = None
    protocol: Optional[str] = None
    simulate: bool = False


class ClearRequest(BaseModel):
    confirm: bool = False


class TileConfig(BaseModel):
    type: str = "value"   # value | gauge | chart
    min: Optional[float] = None
    max: Optional[float] = None
    warn_low: Optional[float] = None
    warn_high: Optional[float] = None
    alarm_low: Optional[float] = None
    alarm_high: Optional[float] = None


class DashboardLayout(BaseModel):
    keys: List[str] = []
    tiles: Dict[str, TileConfig] = {}
    sound: bool = False


class VehicleRequest(BaseModel):
    name: Optional[str] = None
    vin: Optional[str] = None
    fuel: Optional[str] = None
    notes: Optional[str] = None


class TripResetRequest(BaseModel):
    save: bool = True


def _health_summary(report: dict) -> str:
    counts = report.get("counts", {})
    parts = [f"{counts[k]} {label}" for k, label in (("critical", "critical"), ("warn", "warnings")) if counts.get(k)]
    return f"{report['score']}/100 {report['grade']}" + (" · " + ", ".join(parts) if parts else "")


def backup_before_clear(session: ObdSession, storage: Storage) -> Optional[dict]:
    """Save the codes and the freeze frame to the history; clearing erases them from the car."""
    dtcs = session.read_dtcs()
    freeze = session.read_freeze_frame()
    if not dtcs.total and not dtcs.permanent and not freeze.present:
        return None
    codes = [d.code for d in dtcs.stored + dtcs.pending]
    summary = "Before clearing: " + (", ".join(codes) if codes else "no stored codes")
    return storage.add_record(storage.active()["id"], "dtc-backup", summary,
                              {"dtcs": dtcs.to_dict(), "freeze_frame": freeze.to_dict()})


def create_app(settings: Optional[ConnectionSettings] = None, connect: bool = True,
               storage: Optional[Storage] = None) -> FastAPI:
    state = AppState(settings or ConnectionSettings(), storage)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if connect:
            # connect in the background so the UI is available immediately
            threading.Thread(target=_safe_connect, args=(state,), daemon=True).start()
        usage.record_start(state.storage.root)
        stop = threading.Event()
        threading.Thread(target=_watchdog, args=(state, stop), name="obd-watchdog", daemon=True).start()
        yield
        stop.set()
        state.disconnect()

    app = FastAPI(title="OBDruid", version=__version__, lifespan=lifespan)
    app.state.obd = state

    def guarded(fn, *args):
        session = state.check()
        try:
            return fn(session, *args)
        except ObdError as e:
            if not session.connected:
                state.state, state.error = "error", str(e)
            raise HTTPException(502, detail=str(e))

    @app.get("/api/state")
    def get_state():
        return state.to_dict()

    @app.get("/api/ports")
    def get_ports():
        return {"ports": find_ports()}

    @app.get("/api/i18n/dtc")
    def get_dtc_translations(lang: str = "en"):
        """Trouble code descriptions in another language: {english: translated} for the browser's dictionary."""
        return {"descriptions": _dtc_translations(lang)}

    @app.post("/api/bluetooth/scan")
    async def bluetooth_scan(seconds: int = 8):
        from ..core import transports

        devices = await asyncio.to_thread(transports.scan_bluetooth, min(20, max(3, seconds)))
        return {"devices": [d.to_dict() for d in devices]}

    @app.post("/api/connect")
    def post_connect(req: ConnectRequest):
        new = replace(state.settings, port=req.port or None, baudrate=req.baudrate,
                      protocol=req.protocol or None, simulate=req.simulate)
        try:
            state.connect(new)
        except Exception as e:
            raise HTTPException(502, detail=str(e))
        return state.to_dict()

    @app.post("/api/disconnect")
    def post_disconnect():
        state.disconnect()
        return state.to_dict()

    @app.get("/api/info")
    def get_info():
        def work(s: ObdSession):
            return {
                "adapter": s.adapter_info().to_dict(),
                "vin": s.vin(),
                "vin_info": (lambda info: info.to_dict() if info else None)(vin_decode(s.vin())),
                "status": s.status().to_dict(),
            }
        return guarded(work)

    @app.get("/api/params")
    def get_params():
        def work(s: ObdSession):
            params = [p.to_dict() for p in state.reader.params()]
            keys = {p["key"] for p in params}
            return {"params": params, "default": [k for k in DEFAULT_DASHBOARD if k in keys],
                    "tiles": {k: v for k, v in DEFAULT_TILES.items() if k in keys}}
        return guarded(work)

    @app.get("/api/read")
    def get_read(keys: str):
        wanted = [k.strip().upper() for k in keys.split(",") if k.strip()]
        return guarded(lambda s: {"readings": [r.to_dict() for r in state.reader.read(wanted)]})

    @app.get("/api/dtc")
    def get_dtc():
        def work(s: ObdSession):
            data = s.read_dtcs().to_dict()
            data["status"] = s.status().to_dict()
            data["freeze_frame"] = s.read_freeze_frame().to_dict()
            return data
        return guarded(work)

    @app.get("/api/freeze-frame")
    def get_freeze_frame():
        return guarded(lambda s: s.read_freeze_frame().to_dict())

    @app.get("/api/monitors")
    def get_monitors():
        return guarded(lambda s: s.read_monitors().to_dict())

    @app.post("/api/dtc/clear")
    def post_clear(req: ClearRequest):
        if not req.confirm:
            raise HTTPException(400, detail="Clearing codes requires confirmation (confirm: true)")

        def work(s: ObdSession):
            backup = backup_before_clear(s, state.storage)
            s.clear_dtcs()
            data = s.read_dtcs().to_dict()
            data["backup"] = backup
            return data
        return guarded(work)

    # ------------------------------------------------------------------ dashboard, trip
    @app.get("/api/dashboard")
    def get_dashboard():
        return {"layout": state.storage.dashboard(state.vehicle["id"])}

    @app.put("/api/dashboard")
    def put_dashboard(layout: DashboardLayout):
        if len(layout.keys) > 60 or any(t.type not in ("value", "gauge", "chart") for t in layout.tiles.values()):
            raise HTTPException(400, detail="Invalid dashboard layout")
        data = layout.model_dump()
        data["keys"] = [k.upper() for k in data["keys"]]
        return {"layout": state.storage.save_dashboard(state.vehicle["id"], data)}

    @app.get("/api/trip")
    def get_trip():
        return state.trip.summary()

    @app.post("/api/trip/reset")
    def post_trip_reset(req: TripResetRequest):
        summary = state.trip.summary()
        record = None
        if req.save and (summary["distance_km"] or 0) >= 0.1:
            avg = summary["average_l_100km"]
            text = f"{summary['distance_km']:.1f} km" + (f" · {avg:.1f} l/100 km" if avg is not None else "")
            record = state.storage.add_record(state.vehicle["id"], "trip", text, summary)
        state.trip.reset()
        return {"saved": record, "trip": state.trip.summary()}

    # ------------------------------------------------------------------ vehicles, history
    def stored(fn, *args):
        try:
            return fn(*args)
        except StorageError as e:
            raise HTTPException(404, detail=str(e))

    def check_fuel(req: VehicleRequest) -> None:
        if req.fuel is not None and req.fuel not in FUELS:
            raise HTTPException(400, detail=f"Fuel must be one of: {', '.join(FUELS)}")

    @app.get("/api/vehicles")
    def get_vehicles():
        active = state.vehicle
        return {"vehicles": state.storage.vehicles(), "active": active["id"], "fuels": list(FUELS)}

    @app.post("/api/vehicles")
    def post_vehicle(req: VehicleRequest):
        check_fuel(req)
        vehicle = state.storage.create(req.name or "New car", vin=req.vin, fuel=req.fuel or "petrol",
                                       notes=req.notes or "")
        state.trip.fuel = vehicle["fuel"]
        return vehicle

    @app.put("/api/vehicles/{vehicle_id}")
    def put_vehicle(vehicle_id: str, req: VehicleRequest):
        check_fuel(req)
        vehicle = stored(lambda: state.storage.update(vehicle_id, **req.model_dump()))
        if vehicle["id"] == state.vehicle["id"]:
            state.trip.fuel = vehicle["fuel"]
        return vehicle

    @app.post("/api/vehicles/{vehicle_id}/activate")
    def post_vehicle_activate(vehicle_id: str):
        vehicle = stored(state.storage.activate, vehicle_id)
        state.trip.fuel = vehicle["fuel"]
        return vehicle

    @app.delete("/api/vehicles/{vehicle_id}")
    def delete_vehicle(vehicle_id: str):
        stored(state.storage.delete, vehicle_id)
        state.trip.fuel = state.vehicle["fuel"]
        return get_vehicles()

    @app.get("/api/history")
    def get_history(kind: Optional[str] = None):
        return {"vehicle": state.vehicle, "records": state.storage.records(state.vehicle["id"], kind)}

    @app.get("/api/history/{record_id}")
    def get_history_record(record_id: str):
        return stored(state.storage.record, state.vehicle["id"], record_id)

    @app.delete("/api/history/{record_id}")
    def delete_history_record(record_id: str):
        stored(state.storage.delete_record, state.vehicle["id"], record_id)
        return {"deleted": record_id}

    @app.get("/api/history/{record_id}/report.html", response_class=HTMLResponse)
    def get_history_html(record_id: str, lang: str = "en"):
        record = stored(state.storage.record, state.vehicle["id"], record_id)
        if record.get("kind") not in ("health", "battery"):
            raise HTTPException(404, detail="Not a health or battery report")
        return HTMLResponse(render_html(HealthReport.from_dict(record["data"]), lang))

    def supporter_state() -> dict:
        key = supporter.load(state.storage.root)
        return {"active": key is not None, **(key.to_dict() if key else {})}

    @app.get("/api/supporter")
    def get_supporter():
        return supporter_state()

    @app.post("/api/supporter")
    def post_supporter(body: SupporterBody):
        try:
            supporter.save(state.storage.root, body.key)
        except supporter.InvalidKey as e:
            raise HTTPException(400, str(e))
        return supporter_state()

    @app.delete("/api/supporter")
    def delete_supporter():
        supporter.remove(state.storage.root)
        return supporter_state()

    @app.get("/api/about")
    def get_about():
        return {"version": __version__, "author": about.AUTHOR, "license": about.LICENSE, "contact": about.CONTACT_EMAIL,
                "key_request": about.KEY_REQUEST_URL, "repo": about.REPO_URL, "issues": about.ISSUES_URL,
                "support": about.SUPPORT_LINKS, "adapters": about.ADAPTERS, "vehicles": about.VEHICLES_NOTE,
                "data_dir": str(state.storage.root), "supporter": supporter_state(),
                "usage": usage.summary(state.storage.root)}

    @app.get("/api/health/last")
    def get_health_last():
        if state.last_report is None:
            raise HTTPException(404, detail="No health check has been run yet")
        return state.last_report.to_dict()

    @app.get("/api/health/report.html", response_class=HTMLResponse)
    def get_health_html(download: bool = False, lang: str = "en"):
        if state.last_report is None:
            raise HTTPException(404, detail="No health check has been run yet")
        headers = {}
        if download:
            stamp = state.last_report.created.replace(":", "-")
            headers["Content-Disposition"] = f'attachment; filename="health-report-{stamp}.html"'
        return HTMLResponse(render_html(state.last_report, lang), headers=headers)

    @app.websocket("/ws/health")
    async def ws_health(ws: WebSocket):
        """Client sends {"duration": 30}; server streams {"progress", "message"} and finally {"report"}."""
        await ws.accept()
        try:
            msg = json.loads(await ws.receive_text())
            duration = min(120.0, max(5.0, float(msg.get("duration", 30))))
            if state.health_running:
                await ws.send_json({"error": "A health check is already running"})
                return
            try:
                session = state.check()
            except HTTPException as e:
                await ws.send_json({"error": e.detail})
                return

            loop = asyncio.get_running_loop()
            queue: asyncio.Queue = asyncio.Queue()

            def progress(fraction: float, message: str) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, {"progress": round(fraction, 3), "message": message})

            async def forward():
                while True:
                    await ws.send_json(await queue.get())

            state.health_running = True
            forwarder = asyncio.create_task(forward())
            try:
                report = await asyncio.to_thread(run_check, session, duration, progress)
            except ObdError as e:
                await ws.send_json({"error": str(e)})
                return
            finally:
                state.health_running = False
                await asyncio.sleep(0)  # let queued progress messages go out first
                forwarder.cancel()
            state.last_report = report
            data = report.to_dict()
            try:
                data["record"] = state.storage.add_record(state.vehicle["id"], "health", _health_summary(data), data)
            except OSError as e:  # read-only data directory: the report is still shown
                log.warning("Could not save the health report: %s", e)
            await ws.send_json({"report": data})
            await ws.close()
        except (WebSocketDisconnect, RuntimeError):
            pass

    @app.websocket("/ws/battery")
    async def ws_battery(ws: WebSocket):
        """Guided battery test. Server -> {"ask": step, "prompt"}, client -> {"go": true} or {"skip": true};
        server streams {"progress", "message", "volts"} and finally {"report"}."""
        import queue as thread_queue

        from ..core.battery import run_battery_test

        await ws.accept()
        try:
            await ws.receive_text()  # {"start": true}
            if state.health_running:
                await ws.send_json({"error": "A test is already running"})
                return
            try:
                session = state.check()
            except HTTPException as e:
                await ws.send_json({"error": e.detail})
                return
            loop = asyncio.get_running_loop()
            outgoing: asyncio.Queue = asyncio.Queue()
            answers: thread_queue.Queue = thread_queue.Queue()

            def ask(step: str, prompt: str) -> bool:
                loop.call_soon_threadsafe(outgoing.put_nowait, {"ask": step, "prompt": prompt})
                reply = answers.get()           # blocks the test thread until the user answers
                if reply is None:
                    raise ObdError("Battery test cancelled")
                return bool(reply.get("go"))

            def progress(fraction: float, message: str, volts=None) -> None:
                loop.call_soon_threadsafe(outgoing.put_nowait, {"progress": round(fraction, 3),
                                                                "message": message, "volts": volts})

            async def forward():
                while True:
                    await ws.send_json(await outgoing.get())

            async def receive():
                try:
                    while True:
                        answers.put(json.loads(await ws.receive_text()))
                except (WebSocketDisconnect, RuntimeError):
                    answers.put(None)

            state.health_running = True   # keeps the reconnect watchdog away while the voltage dips
            forwarder, receiver = asyncio.create_task(forward()), asyncio.create_task(receive())
            try:
                report = await asyncio.to_thread(run_battery_test, session, ask, progress)
            except ObdError as e:
                await ws.send_json({"error": str(e)})
                return
            finally:
                state.health_running = False
                await asyncio.sleep(0)
                forwarder.cancel()
                receiver.cancel()
            data = report.to_dict()
            try:
                data["record"] = state.storage.add_record(state.vehicle["id"], "battery",
                                                          f"{data['score']}/100 {data['grade']}", data)
            except OSError as e:
                log.warning("Could not save the battery report: %s", e)
            await ws.send_json({"report": data})
            await ws.close()
        except (WebSocketDisconnect, RuntimeError):
            pass

    @app.websocket("/ws/live")
    async def ws_live(ws: WebSocket):
        """Client sends {"keys": [...], "interval": 0.5}; server streams {"readings": [...]}."""
        await ws.accept()
        config = {"keys": [], "interval": 0.5}

        async def receiver():
            while True:
                msg = json.loads(await ws.receive_text())
                if isinstance(msg.get("keys"), list):
                    config["keys"] = [str(k).upper() for k in msg["keys"]][:40]
                if "interval" in msg:
                    config["interval"] = min(10.0, max(0.1, float(msg["interval"])))

        recv_task = asyncio.create_task(receiver())
        try:
            while not recv_task.done():
                started = asyncio.get_running_loop().time()
                if config["keys"]:
                    try:
                        state.check()
                        readings = await asyncio.to_thread(state.reader.read, list(config["keys"]))
                        await ws.send_json({"readings": [r.to_dict() for r in readings],
                                            "trip": state.trip.summary()})
                    except HTTPException as e:
                        await ws.send_json({"error": e.detail, "state": state.state})
                        await asyncio.sleep(1.0)
                    except ObdError as e:
                        if not state.session.connected:
                            state.state, state.error = "error", str(e)
                        await ws.send_json({"error": str(e), "state": state.state})
                        await asyncio.sleep(1.0)
                elapsed = asyncio.get_running_loop().time() - started
                await asyncio.sleep(max(0.05, config["interval"] - elapsed))
            recv_task.result()  # re-raise disconnects
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            recv_task.cancel()

    @app.exception_handler(ObdError)
    async def obd_error_handler(request, exc: ObdError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    app.mount("/", NoCacheStaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


mimetypes.add_type("application/manifest+json", ".webmanifest")


class NoCacheStaticFiles(StaticFiles):
    """Static files the browser must revalidate (ETag), so an update never mixes new HTML with old CSS/JS."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


@lru_cache(maxsize=4)
def _dtc_translations(lang: str) -> dict:
    from obd.codes import DTC

    from ..core import dtc_i18n
    from ..core.dtc import clean_description

    out = {}
    for text in {clean_description(v) for v in DTC.values()}:
        local = dtc_i18n.translate(text, lang)
        if local != text:
            out[text] = local
    return out


def _watchdog(state: AppState, stop: threading.Event, every: float = 2.0) -> None:
    while not stop.wait(every):
        try:
            state.watchdog_step()
        except Exception as e:  # never let the watchdog die
            log.warning("Watchdog: %s", e)


def _safe_connect(state: AppState) -> None:
    try:
        state.connect()
    except Exception:
        pass  # error is stored in state and shown in the UI


def run(settings: ConnectionSettings, host: str = "127.0.0.1", port: int = 8090, connect: bool = True,
        ssl_cert: Optional[str] = None, ssl_key: Optional[str] = None) -> None:
    import uvicorn

    app = create_app(settings, connect=connect)
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    scheme = "https" if ssl_cert else "http"
    print(f"OBDruid: {scheme}://{shown}:{port}/  (Ctrl+C to quit)")
    uvicorn.run(app, host=host, port=port, log_level="warning", ssl_certfile=ssl_cert, ssl_keyfile=ssl_key)
