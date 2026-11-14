"""Files kept between runs: vehicle profiles, dashboard layouts and history records.

Everything is plain JSON under one data directory:

    vehicles.json                      profiles + the active one
    vehicles/<id>/dashboard.json       dashboard layout of that vehicle
    vehicles/<id>/history/<id>.json    health reports, pre-clear backups, trips

The directory is $OBD_TOOL_DATA, or $XDG_DATA_HOME/obd-tool, or ~/.local/share/obd-tool.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

FUELS = ("petrol", "diesel", "lpg")
KINDS = ("health", "dtc-backup", "trip")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def default_data_dir() -> Path:
    if os.environ.get("OBD_TOOL_DATA"):
        return Path(os.environ["OBD_TOOL_DATA"])
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "obd-tool"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class StorageError(ValueError):
    """Bad id or missing record/vehicle."""


class Storage:
    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else default_data_dir()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ low level
    def _read(self, path: Path, fallback: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return fallback

    def _write(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)  # atomic: a crash never leaves a half written file

    @staticmethod
    def _check_id(value: str) -> str:
        if not _ID_RE.match(value or ""):
            raise StorageError(f"Invalid id: {value!r}")
        return value

    # ------------------------------------------------------------------ vehicles
    def _load(self) -> dict:
        data = self._read(self.root / "vehicles.json", {})
        data.setdefault("vehicles", [])
        data.setdefault("active", None)
        return data

    def _save(self, data: dict) -> None:
        self._write(self.root / "vehicles.json", data)

    def vehicles(self) -> list[dict]:
        with self._lock:
            return self._load()["vehicles"]

    def active(self) -> dict:
        """The active vehicle; a default profile is created on first use."""
        with self._lock:
            data = self._load()
            for v in data["vehicles"]:
                if v["id"] == data["active"]:
                    return v
            if data["vehicles"]:
                data["active"] = data["vehicles"][0]["id"]
                self._save(data)
                return data["vehicles"][0]
            return self.create("My car")

    def get(self, vehicle_id: str) -> dict:
        for v in self.vehicles():
            if v["id"] == vehicle_id:
                return v
        raise StorageError("Unknown vehicle")

    def create(self, name: str, vin: Optional[str] = None, fuel: str = "petrol", notes: str = "",
               activate: bool = True) -> dict:
        with self._lock:
            data = self._load()
            vehicle = {"id": uuid.uuid4().hex[:8], "name": (name or "").strip() or "My car", "vin": vin or None,
                       "fuel": fuel if fuel in FUELS else "petrol", "notes": notes or "", "created": _now(),
                       "connection": None}
            data["vehicles"].append(vehicle)
            if activate or not data["active"]:
                data["active"] = vehicle["id"]
            self._save(data)
            return vehicle

    def update(self, vehicle_id: str, **changes) -> dict:
        with self._lock:
            data = self._load()
            for v in data["vehicles"]:
                if v["id"] != vehicle_id:
                    continue
                for key in ("name", "vin", "fuel", "notes", "connection"):
                    if key in changes and changes[key] is not None:
                        v[key] = changes[key]
                if v["fuel"] not in FUELS:
                    v["fuel"] = "petrol"
                v["name"] = str(v["name"]).strip() or "My car"
                self._save(data)
                return v
        raise StorageError("Unknown vehicle")

    def activate(self, vehicle_id: str) -> dict:
        with self._lock:
            data = self._load()
            for v in data["vehicles"]:
                if v["id"] == vehicle_id:
                    data["active"] = vehicle_id
                    self._save(data)
                    return v
        raise StorageError("Unknown vehicle")

    def delete(self, vehicle_id: str) -> None:
        """Remove a profile with its dashboard and history."""
        import shutil

        with self._lock:
            self._check_id(vehicle_id)
            data = self._load()
            if not any(v["id"] == vehicle_id for v in data["vehicles"]):
                raise StorageError("Unknown vehicle")
            data["vehicles"] = [v for v in data["vehicles"] if v["id"] != vehicle_id]
            if data["active"] == vehicle_id:
                data["active"] = data["vehicles"][0]["id"] if data["vehicles"] else None
            self._save(data)
            shutil.rmtree(self._dir(vehicle_id), ignore_errors=True)

    def select_for_vin(self, vin: Optional[str], simulated: bool = False) -> dict:
        """Pick (and activate) the profile for the connected car.

        The simulator always gets its own "Simulator" profile, so test data never lands
        in a real car's history. A real car with a VIN gets the profile with that VIN,
        else the active one if it has no VIN yet (it adopts the VIN), else a new profile.
        A real car without a VIN keeps the active profile.
        """
        with self._lock:
            everyone = self.vehicles()
            if simulated:
                for v in everyone:
                    if v.get("simulator"):
                        return self.activate(v["id"])
                vehicle = self.create("Simulator", vin=vin)
                return self._flag_simulator(vehicle["id"])
            real = [v for v in everyone if not v.get("simulator")]
            active = self.active()
            if active.get("simulator"):  # coming back from the simulator
                active = self.activate(real[0]["id"]) if real else self.create("My car")
            if not vin:
                return active
            for v in real:
                if v.get("vin") == vin:
                    return self.activate(v["id"])
            if not active.get("vin"):
                return self.update(active["id"], vin=vin)
            return self.create(f"Car {vin[-6:]}", vin=vin)

    def _flag_simulator(self, vehicle_id: str) -> dict:
        data = self._load()
        for v in data["vehicles"]:
            if v["id"] == vehicle_id:
                v["simulator"] = True
                self._save(data)
                return v
        raise StorageError("Unknown vehicle")

    def _dir(self, vehicle_id: str) -> Path:
        return self.root / "vehicles" / self._check_id(vehicle_id)

    # ------------------------------------------------------------------ dashboard
    def dashboard(self, vehicle_id: str) -> Optional[dict]:
        return self._read(self._dir(vehicle_id) / "dashboard.json", None)

    def save_dashboard(self, vehicle_id: str, layout: dict) -> dict:
        with self._lock:
            self._write(self._dir(vehicle_id) / "dashboard.json", layout)
            return layout

    # ------------------------------------------------------------------ history
    def add_record(self, vehicle_id: str, kind: str, summary: str, data: dict) -> dict:
        """Store a record; returns its metadata (id, kind, created, summary)."""
        if kind not in KINDS:
            raise StorageError(f"Unknown record kind: {kind}")
        with self._lock:
            created = _now()
            stamp = created.replace("-", "").replace(":", "").replace("T", "-")
            folder = self._dir(vehicle_id) / "history"
            record_id, n = f"{stamp}-{kind}", 1
            while (folder / f"{record_id}.json").exists():
                n += 1
                record_id = f"{stamp}-{kind}-{n}"
            meta = {"id": record_id, "kind": kind, "created": created, "summary": summary}
            self._write(folder / f"{record_id}.json", {**meta, "data": data})
            return meta

    def records(self, vehicle_id: str, kind: Optional[str] = None) -> list[dict]:
        """Metadata of the vehicle's records, newest first."""
        folder = self._dir(vehicle_id) / "history"
        out = []
        for path in folder.glob("*.json") if folder.is_dir() else []:
            record = self._read(path, None)
            if not isinstance(record, dict) or (kind and record.get("kind") != kind):
                continue
            out.append({k: record.get(k) for k in ("id", "kind", "created", "summary")})
        # ids of records made in the same second end with -2, -3...: order those by length too
        out.sort(key=lambda r: (r["created"] or "", len(r["id"] or ""), r["id"] or ""), reverse=True)
        return out

    def record(self, vehicle_id: str, record_id: str) -> dict:
        record = self._read(self._dir(vehicle_id) / "history" / f"{self._check_id(record_id)}.json", None)
        if not isinstance(record, dict):
            raise StorageError("Unknown record")
        return record

    def record_path(self, vehicle_id: str, record_id: str) -> Path:
        return self._dir(vehicle_id) / "history" / f"{self._check_id(record_id)}.json"

    def delete_record(self, vehicle_id: str, record_id: str) -> None:
        path = self.record_path(vehicle_id, record_id)
        if not path.exists():
            raise StorageError("Unknown record")
        path.unlink()
