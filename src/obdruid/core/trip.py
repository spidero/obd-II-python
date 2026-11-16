"""Fuel consumption and trip summary calculated from live data.

Fuel flow comes from the ECU's fuel rate (PID 5E) when the car reports it, otherwise
it is estimated from the mass air flow: a petrol engine in closed loop burns air and
fuel at the stoichiometric ratio, so fuel [g/s] = MAF / AFR. This is an estimate - it
is a little high while coasting (fuel cut) and at full load (enrichment).
Diesels run lean, so without PID 5E no estimate is made for them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .models import Param, Reading

# fuel: (stoichiometric air/fuel ratio, density g/l); None = MAF based estimate not valid
FUEL_DATA = {
    "petrol": (14.7, 745.0),
    "lpg": (15.5, 540.0),
    "diesel": None,
}

GROUP = "Trip"
# key: (label, unit, decimals)
TRIP_PARAMS = {
    "FUEL_FLOW": ("Fuel flow (calculated)", "l/h", 2),
    "FUEL_ECONOMY": ("Fuel consumption (instant)", "l/100 km", 1),
    "TRIP_ECONOMY": ("Fuel consumption (trip average)", "l/100 km", 1),
    "TRIP_DISTANCE": ("Trip distance", "km", 2),
    "TRIP_FUEL": ("Trip fuel used", "l", 2),
    "TRIP_TIME": ("Trip time", "min", 1),
    "TRIP_AVG_SPEED": ("Trip average speed", "km/h", 0),
}
MAX_GAP = 5.0     # s; longer pauses between samples are not integrated
MOVING = 2.0      # km/h; below this the car counts as standing


def fuel_source(supported: set[str], fuel: str) -> Optional[str]:
    """Live parameter the fuel flow is derived from, or None when it cannot be calculated."""
    if "FUEL_RATE" in supported:
        return "FUEL_RATE"
    if "MAF" in supported and FUEL_DATA.get(fuel):
        return "MAF"
    return None


def trip_params(supported: set[str], fuel: str) -> list[Param]:
    """Calculated parameters available for this car."""
    keys = []
    has_fuel = fuel_source(supported, fuel) is not None
    has_speed = "SPEED" in supported
    if has_fuel:
        keys.append("FUEL_FLOW")
    if has_fuel and has_speed:
        keys += ["FUEL_ECONOMY", "TRIP_ECONOMY"]
    if has_speed:
        keys += ["TRIP_DISTANCE"]
    if has_fuel:
        keys.append("TRIP_FUEL")
    keys.append("TRIP_TIME")
    if has_speed:
        keys.append("TRIP_AVG_SPEED")
    return [Param(key=k, label=TRIP_PARAMS[k][0], unit=TRIP_PARAMS[k][1], pid="calc", group=GROUP,
                  decimals=TRIP_PARAMS[k][2]) for k in keys]


def fuel_flow(fuel: str, fuel_rate: Optional[float] = None, maf: Optional[float] = None) -> Optional[float]:
    """Fuel flow in l/h from the fuel rate PID [l/h] or the mass air flow [g/s]."""
    if fuel_rate is not None:
        return float(fuel_rate)
    data = FUEL_DATA.get(fuel)
    if maf is None or not data:
        return None
    afr, density = data
    return maf / afr / density * 3600.0


@dataclass
class TripComputer:
    fuel: str = "petrol"
    distance: float = 0.0     # km
    fuel_used: float = 0.0    # l
    duration: float = 0.0     # s the engine/logging was active
    idle: float = 0.0         # s standing still
    started: Optional[float] = None   # wall clock time of the first sample
    _last: Optional[float] = None
    flow: Optional[float] = None      # last fuel flow, l/h
    speed: Optional[float] = None     # last speed, km/h

    def reset(self) -> None:
        fuel = self.fuel
        self.__init__(fuel=fuel)  # type: ignore[misc]

    def update(self, speed: Optional[float], fuel_rate: Optional[float] = None, maf: Optional[float] = None,
               now: Optional[float] = None) -> None:
        """Feed one sample. `now` is a monotonic time in seconds (defaults to the current time)."""
        # perf_counter: the monotonic clock of Windows ticks every 15.6 ms, two quick reads would get the same time
        now = time.perf_counter() if now is None else now
        flow = fuel_flow(self.fuel, fuel_rate, maf)
        if self.started is None:
            self.started = time.time()
        if self._last is not None:
            dt = now - self._last
            if 0 < dt <= MAX_GAP:
                self.duration += dt
                # trapezoid between the previous and the current sample
                if speed is not None and self.speed is not None:
                    self.distance += (speed + self.speed) / 2 * dt / 3600.0
                if flow is not None and self.flow is not None:
                    self.fuel_used += (flow + self.flow) / 2 * dt / 3600.0
                if speed is not None and speed < MOVING:
                    self.idle += dt
        self._last = now
        self.flow, self.speed = flow, speed

    # ------------------------------------------------------------------ results
    @property
    def instant(self) -> Optional[float]:
        """Instant consumption in l/100 km; None while standing (it would be infinite)."""
        if self.flow is None or self.speed is None or self.speed < MOVING:
            return None
        return self.flow / self.speed * 100.0

    @property
    def average(self) -> Optional[float]:
        if self.distance < 0.05 or self.fuel_used <= 0:
            return None
        return self.fuel_used / self.distance * 100.0

    @property
    def average_speed(self) -> Optional[float]:
        return self.distance / self.duration * 3600.0 if self.duration > 1 else None

    def value(self, key: str) -> Optional[float]:
        return {
            "FUEL_FLOW": self.flow,
            "FUEL_ECONOMY": self.instant,
            "TRIP_ECONOMY": self.average,
            "TRIP_DISTANCE": self.distance,
            "TRIP_FUEL": self.fuel_used,
            "TRIP_TIME": self.duration / 60.0,
            "TRIP_AVG_SPEED": self.average_speed,
        }.get(key)

    def reading(self, key: str) -> Reading:
        label, unit, decimals = TRIP_PARAMS[key]
        value = self.value(key)
        return Reading(key=key, label=label, value=None if value is None else round(value, decimals),
                       unit=unit, pid="calc", time=time.time())

    def summary(self) -> dict:
        def r(v, n):
            return None if v is None else round(v, n)
        return {
            "fuel": self.fuel,
            "distance_km": r(self.distance, 2),
            "fuel_l": r(self.fuel_used, 3),
            "average_l_100km": r(self.average, 1),
            "duration_s": r(self.duration, 0),
            "idle_s": r(self.idle, 0),
            "average_speed_kmh": r(self.average_speed, 1),
            "started": self.started,
        }


class LiveReader:
    """Reads live parameters and fills in the calculated trip ones.

    The parameters the trip computer needs (speed and fuel rate or MAF) are read with
    every batch, so the trip keeps counting whatever is shown on the dashboard.
    """

    def __init__(self, session, trip: TripComputer):
        self.session = session
        self.trip = trip
        self._supported: Optional[set[str]] = None

    def supported(self) -> set[str]:
        if self._supported is None:
            self._supported = {p.key for p in self.session.supported_params()}
        return self._supported

    def params(self) -> list[Param]:
        return self.session.supported_params() + trip_params(self.supported(), self.trip.fuel)

    def read(self, keys: list[str]) -> list[Reading]:
        supported = self.supported()
        source = fuel_source(supported, self.trip.fuel)
        needed = [k for k in ("SPEED", source) if k and k in supported]
        real = [k for k in keys if k not in TRIP_PARAMS]
        extra = [k for k in needed if k not in real]
        readings = {r.key: r for r in self.session.read(real + extra)}

        def number(key):
            r = readings.get(key)
            return r.value if r is not None and isinstance(r.value, (int, float)) else None

        self.trip.update(speed=number("SPEED"),
                         fuel_rate=number("FUEL_RATE") if source == "FUEL_RATE" else None,
                         maf=number("MAF") if source == "MAF" else None)
        return [self.trip.reading(k) if k in TRIP_PARAMS else readings[k] for k in keys]
