"""Catalogue of mode 01 live parameters with labels and display units.

Keys are python-OBD command names. Parameters missing here are still shown
(when the car supports them) using python-OBD's English description.
"""

from __future__ import annotations

from .models import Param

# key: (label, unit, group, decimals)
CATALOG: dict[str, tuple[str, str, str, int]] = {
    "RPM": ("Engine speed", "rpm", "Engine", 0),
    "SPEED": ("Vehicle speed", "km/h", "Engine", 0),
    "ENGINE_LOAD": ("Engine load", "%", "Engine", 1),
    "ABSOLUTE_LOAD": ("Absolute load", "%", "Engine", 1),
    "COOLANT_TEMP": ("Coolant temperature", "°C", "Temperatures", 0),
    "INTAKE_TEMP": ("Intake air temperature", "°C", "Temperatures", 0),
    "AMBIANT_AIR_TEMP": ("Ambient air temperature", "°C", "Temperatures", 0),
    "OIL_TEMP": ("Oil temperature", "°C", "Temperatures", 0),
    "CATALYST_TEMP_B1S1": ("Catalyst temperature B1S1", "°C", "Temperatures", 0),
    "CATALYST_TEMP_B1S2": ("Catalyst temperature B1S2", "°C", "Temperatures", 0),
    "TIMING_ADVANCE": ("Timing advance", "°", "Engine", 1),
    "INTAKE_PRESSURE": ("Intake manifold pressure (MAP)", "kPa", "Air", 0),
    "BAROMETRIC_PRESSURE": ("Barometric pressure", "kPa", "Air", 0),
    "MAF": ("Mass air flow (MAF)", "g/s", "Air", 2),
    "THROTTLE_POS": ("Throttle position", "%", "Throttle", 1),
    "RELATIVE_THROTTLE_POS": ("Relative throttle position", "%", "Throttle", 1),
    "THROTTLE_POS_B": ("Throttle position B", "%", "Throttle", 1),
    "THROTTLE_ACTUATOR": ("Commanded throttle actuator", "%", "Throttle", 1),
    "ACCELERATOR_POS_D": ("Accelerator pedal D", "%", "Throttle", 1),
    "ACCELERATOR_POS_E": ("Accelerator pedal E", "%", "Throttle", 1),
    "SHORT_FUEL_TRIM_1": ("Short term fuel trim B1", "%", "Fuel", 1),
    "LONG_FUEL_TRIM_1": ("Long term fuel trim B1", "%", "Fuel", 1),
    "SHORT_FUEL_TRIM_2": ("Short term fuel trim B2", "%", "Fuel", 1),
    "LONG_FUEL_TRIM_2": ("Long term fuel trim B2", "%", "Fuel", 1),
    "FUEL_STATUS": ("Fuel system status", "", "Fuel", 0),
    "FUEL_LEVEL": ("Fuel level", "%", "Fuel", 0),
    "FUEL_PRESSURE": ("Fuel pressure", "kPa", "Fuel", 0),
    "FUEL_RAIL_PRESSURE_DIRECT": ("Fuel rail pressure", "kPa", "Fuel", 0),
    "COMMANDED_EQUIV_RATIO": ("Commanded equivalence ratio", "λ", "Fuel", 3),
    "FUEL_RATE": ("Fuel rate", "l/h", "Fuel", 2),
    "O2_B1S1": ("O2 sensor B1S1", "V", "O2 sensors", 3),
    "O2_B1S2": ("O2 sensor B1S2", "V", "O2 sensors", 3),
    "O2_S1_WR_VOLTAGE": ("Wideband O2 sensor S1", "V", "O2 sensors", 3),
    "O2_S1_WR_CURRENT": ("Wideband O2 sensor S1 current", "mA", "O2 sensors", 3),
    "SHORT_O2_TRIM_B1": ("Short term secondary O2 trim B1", "%", "O2 sensors", 1),
    "LONG_O2_TRIM_B1": ("Long term secondary O2 trim B1", "%", "O2 sensors", 1),
    "CONTROL_MODULE_VOLTAGE": ("Control module voltage", "V", "Electrical", 2),
    "RUN_TIME": ("Run time since engine start", "s", "Counters", 0),
    "DISTANCE_W_MIL": ("Distance with MIL on", "km", "Counters", 0),
    "DISTANCE_SINCE_DTC_CLEAR": ("Distance since codes cleared", "km", "Counters", 0),
    "WARMUPS_SINCE_DTC_CLEAR": ("Warm-ups since codes cleared", "", "Counters", 0),
    "TIME_SINCE_DTC_CLEARED": ("Time since codes cleared", "min", "Counters", 0),
    "COMMANDED_EGR": ("Commanded EGR", "%", "Emissions", 1),
    "EGR_ERROR": ("EGR error", "%", "Emissions", 1),
    "EVAPORATIVE_PURGE": ("Commanded evaporative purge", "%", "Emissions", 1),
    "OBD_COMPLIANCE": ("OBD standard", "", "Information", 0),
    "O2_SENSORS": ("O2 sensors present", "", "Information", 0),
}

# Parameters shown on the dashboard by default (if supported).
DEFAULT_DASHBOARD = [
    "RPM",
    "SPEED",
    "COOLANT_TEMP",
    "ENGINE_LOAD",
    "THROTTLE_POS",
    "INTAKE_PRESSURE",
    "INTAKE_TEMP",
    "TIMING_ADVANCE",
    "SHORT_FUEL_TRIM_1",
    "LONG_FUEL_TRIM_1",
    "CONTROL_MODULE_VOLTAGE",
    "O2_B1S1",
]

# Commands that are not live values (bitmasks, status, freeze frame...).
NOT_LIVE = {
    "PIDS_A", "PIDS_B", "PIDS_C", "STATUS", "FREEZE_DTC", "STATUS_DRIVE_CYCLE", "MAX_VALUES",
}

# Display names for pint units (python-OBD) when a key is not in CATALOG.
UNIT_NAMES = {
    "degC": "°C",
    "degree_Celsius": "°C",
    "revolutions_per_minute": "rpm",
    "kph": "km/h",
    "kilometer_per_hour": "km/h",
    "percent": "%",
    "kilopascal": "kPa",
    "pascal": "Pa",
    "gps": "g/s",
    "grams_per_second": "g/s",
    "volt": "V",
    "milliampere": "mA",
    "degree": "°",
    "second": "s",
    "minute": "min",
    "kilometer": "km",
    "ratio": "",
    "count": "",
    "liters_per_hour": "l/h",
    "dimensionless": "",
}

# Names of OBD readiness monitors (python-OBD Status attributes).
READINESS_LABELS = {
    "MISFIRE_MONITORING": "Misfire",
    "FUEL_SYSTEM_MONITORING": "Fuel system",
    "COMPONENT_MONITORING": "Comprehensive components",
    "CATALYST_MONITORING": "Catalyst",
    "HEATED_CATALYST_MONITORING": "Heated catalyst",
    "EVAPORATIVE_SYSTEM_MONITORING": "Evaporative system",
    "SECONDARY_AIR_SYSTEM_MONITORING": "Secondary air system",
    "OXYGEN_SENSOR_MONITORING": "O2 sensor",
    "OXYGEN_SENSOR_HEATER_MONITORING": "O2 sensor heater",
    "EGR_VVT_SYSTEM_MONITORING": "EGR / VVT",
    "NMHC_CATALYST_MONITORING": "NMHC catalyst",
    "NOX_SCR_AFTERTREATMENT_MONITORING": "NOx / SCR aftertreatment",
    "BOOST_PRESSURE_MONITORING": "Boost pressure",
    "EXHAUST_GAS_SENSOR_MONITORING": "Exhaust gas sensor",
    "PM_FILTER_MONITORING": "Particulate filter (DPF)",
}


def unit_name(units: str) -> str:
    return UNIT_NAMES.get(units, units)


def param_for(cmd) -> Param:
    """Build a Param for a python-OBD command."""
    pid = cmd.command.decode() if isinstance(cmd.command, bytes) else str(cmd.command)
    if cmd.name in CATALOG:
        label, unit, group, decimals = CATALOG[cmd.name]
    else:
        label, unit, group, decimals = cmd.desc, "", "Other", 2
    return Param(key=cmd.name, label=label, unit=unit, pid=pid.upper(), group=group, decimals=decimals)
