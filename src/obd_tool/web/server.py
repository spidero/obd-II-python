"""FastAPI web server: REST API + WebSocket with live data + static UI."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import __version__
from ..core import DEFAULT_DASHBOARD, ConnectionSettings, ObdError, ObdSession, find_ports

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


class AppState:
    """Holds the single OBD session shared by all clients."""

    def __init__(self, settings: ConnectionSettings):
        self.settings = settings
        self.session = ObdSession(settings)
        self.state = "disconnected"  # disconnected | connecting | connected | error
        self.error: Optional[str] = None
        self._lock = threading.Lock()

    def connect(self, settings: Optional[ConnectionSettings] = None) -> None:
        with self._lock:
            if settings is not None:
                self.settings = settings
                self.session.close()
                self.session = ObdSession(settings)
            self.state, self.error = "connecting", None
        try:
            self.session.connect()
        except Exception as e:  # report any failure to the UI
            log.warning("Connect failed: %s", e)
            self.state, self.error = "error", str(e)
            raise
        self.state = "connected"

    def disconnect(self) -> None:
        self.session.close()
        self.state, self.error = "disconnected", None

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
            "version": __version__,
        }


class ConnectRequest(BaseModel):
    port: Optional[str] = None
    baudrate: Optional[int] = None
    protocol: Optional[str] = None
    simulate: bool = False


class ClearRequest(BaseModel):
    confirm: bool = False


def create_app(settings: Optional[ConnectionSettings] = None, connect: bool = True) -> FastAPI:
    state = AppState(settings or ConnectionSettings())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if connect:
            # connect in the background so the UI is available immediately
            threading.Thread(target=_safe_connect, args=(state,), daemon=True).start()
        yield
        state.disconnect()

    app = FastAPI(title="OBD Tool", version=__version__, lifespan=lifespan)
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
                "status": s.status().to_dict(),
            }
        return guarded(work)

    @app.get("/api/params")
    def get_params():
        def work(s: ObdSession):
            params = [p.to_dict() for p in s.supported_params()]
            keys = {p["key"] for p in params}
            return {"params": params, "default": [k for k in DEFAULT_DASHBOARD if k in keys]}
        return guarded(work)

    @app.get("/api/read")
    def get_read(keys: str):
        wanted = [k.strip().upper() for k in keys.split(",") if k.strip()]
        return guarded(lambda s: {"readings": [r.to_dict() for r in s.read(wanted)]})

    @app.get("/api/dtc")
    def get_dtc():
        def work(s: ObdSession):
            data = s.read_dtcs().to_dict()
            data["status"] = s.status().to_dict()
            return data
        return guarded(work)

    @app.post("/api/dtc/clear")
    def post_clear(req: ClearRequest):
        if not req.confirm:
            raise HTTPException(400, detail="Clearing codes requires confirmation (confirm: true)")

        def work(s: ObdSession):
            s.clear_dtcs()
            return s.read_dtcs().to_dict()
        return guarded(work)

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
                        session = state.check()
                        readings = await asyncio.to_thread(session.read, list(config["keys"]))
                        await ws.send_json({"readings": [r.to_dict() for r in readings]})
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

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def _safe_connect(state: AppState) -> None:
    try:
        state.connect()
    except Exception:
        pass  # error is stored in state and shown in the UI


def run(settings: ConnectionSettings, host: str = "127.0.0.1", port: int = 8090, connect: bool = True) -> None:
    import uvicorn

    app = create_app(settings, connect=connect)
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    print(f"OBD Tool: http://{shown}:{port}/  (Ctrl+C to quit)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
