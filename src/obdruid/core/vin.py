"""Offline VIN decoding: manufacturer and country from the WMI (first three characters) and the model year.

Only what the VIN reliably tells: the WMI table covers common makes; the model year is in position 10
for most makers (and position 11 for Ford of Europe), repeats every 30 years and is optional outside
North America, so it is reported as an estimate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Optional

# Longest prefix wins (3 characters, then 2).
WMI = {
    # Germany
    "WVW": "Volkswagen", "WV1": "Volkswagen Commercial", "WV2": "Volkswagen Commercial", "WVG": "Volkswagen",
    "WAU": "Audi", "WA1": "Audi", "WUA": "Audi Sport", "TRU": "Audi", "WBA": "BMW", "WBS": "BMW M", "WBX": "BMW",
    "WBY": "BMW i", "WMW": "MINI", "WDB": "Mercedes-Benz", "WDD": "Mercedes-Benz", "WDC": "Mercedes-Benz",
    "WDF": "Mercedes-Benz", "W1K": "Mercedes-Benz", "W1N": "Mercedes-Benz", "W1V": "Mercedes-Benz",
    "WME": "smart", "W0L": "Opel", "W0V": "Opel", "WP0": "Porsche", "WP1": "Porsche", "WF0": "Ford",
    # Czechia, Spain, Slovakia, Hungary, Poland, Romania
    "TMB": "Škoda", "TMA": "Hyundai", "VSS": "SEAT", "VS6": "Ford", "VS7": "Citroën", "VSK": "Nissan",
    "U5Y": "Kia", "TSM": "Suzuki", "SUF": "FSO", "SUP": "FSO", "SUL": "FSC", "SUU": "Solaris", "UU1": "Dacia",
    # France, Italy, UK, Sweden, Netherlands, Belgium, Turkey
    "VF1": "Renault", "VF2": "Renault", "VF3": "Peugeot", "VF6": "Renault Trucks", "VF7": "Citroën",
    "VR1": "DS", "VR3": "Peugeot", "VR7": "Citroën", "VNK": "Toyota", "VN1": "Renault/Opel/Nissan",
    "ZFA": "Fiat", "ZFF": "Ferrari", "ZAR": "Alfa Romeo", "ZLA": "Lancia", "ZAM": "Maserati", "ZCF": "Iveco",
    "ZHW": "Lamborghini", "SAJ": "Jaguar", "SAL": "Land Rover", "SCC": "Lotus", "SCF": "Aston Martin",
    "SCB": "Bentley", "SCA": "Rolls-Royce", "SHH": "Honda", "SHS": "Honda", "SJN": "Nissan", "SB1": "Toyota",
    "SFD": "Alexander Dennis", "YV1": "Volvo", "YV4": "Volvo", "YS3": "Saab", "YS2": "Scania", "XLR": "DAF",
    "NM0": "Ford", "NMT": "Toyota", "NLH": "Hyundai", "VX1": "Opel",
    # Japan, Korea, China, India, Thailand
    "JTD": "Toyota", "JTE": "Toyota", "JTH": "Lexus", "JTN": "Toyota", "JT": "Toyota", "JHM": "Honda",
    "JHL": "Honda", "JH": "Honda", "JN1": "Nissan", "JN": "Nissan", "JM": "Mazda", "JMZ": "Mazda",
    "JF": "Subaru", "JS": "Suzuki", "JA": "Isuzu", "JMB": "Mitsubishi", "JMY": "Mitsubishi",
    "KMH": "Hyundai", "KM8": "Hyundai", "KNA": "Kia", "KND": "Kia", "KNE": "Kia", "KL": "Daewoo/Chevrolet",
    "KPT": "SsangYong", "LSJ": "MG", "LRW": "Tesla", "LVS": "Ford", "MA3": "Suzuki", "MAL": "Hyundai",
    "MM": "Mazda", "MMB": "Mitsubishi", "MR0": "Toyota",
    # North America
    "1FA": "Ford", "1FT": "Ford", "1FM": "Ford", "1F": "Ford", "1G1": "Chevrolet", "1GC": "Chevrolet",
    "1G": "General Motors", "1C": "Chrysler", "1J": "Jeep", "1H": "Honda", "1N": "Nissan", "1VW": "Volkswagen",
    "2F": "Ford", "2G": "General Motors", "2H": "Honda", "2T": "Toyota", "3F": "Ford", "3G": "General Motors",
    "3VW": "Volkswagen", "3N": "Nissan", "4T": "Toyota", "4S": "Subaru", "5YJ": "Tesla", "7SA": "Tesla",
    "5N": "Hyundai", "5X": "Hyundai/Kia", "5T": "Toyota", "4U": "BMW", "5U": "BMW", "55S": "Mercedes-Benz",
    "4J": "Mercedes-Benz",
}

# First character(s) -> country of manufacture (where the WMI was assigned).
COUNTRIES = [
    ("SA", "SM", "United Kingdom"), ("SN", "ST", "Germany"), ("SU", "SZ", "Poland"), ("TA", "TH", "Switzerland"),
    ("TJ", "TP", "Czechia"), ("TR", "TV", "Hungary"), ("TW", "T1", "Portugal"), ("UU", "UZ", "Romania"),
    ("U5", "U7", "Slovakia"), ("VA", "VE", "Austria"), ("VF", "VR", "France"), ("VS", "VW", "Spain"),
    ("VX", "V2", "France"), ("V3", "V5", "Croatia"), ("WA", "W0", "Germany"), ("W1", "W9", "Germany"),
    ("XL", "XR", "Netherlands"), ("XS", "XW", "Russia"), ("YA", "YE", "Belgium"), ("YF", "YK", "Finland"),
    ("YS", "YW", "Sweden"), ("ZA", "ZR", "Italy"), ("NL", "NR", "Turkey"), ("JA", "J0", "Japan"),
    ("KL", "KR", "South Korea"), ("LA", "L0", "China"), ("MA", "ME", "India"), ("ML", "MR", "Thailand"),
    ("1A", "10", "United States"), ("4A", "40", "United States"), ("5A", "50", "United States"),
    ("7A", "70", "United States"), ("2A", "20", "Canada"), ("3A", "37", "Mexico"), ("9A", "9E", "Brazil"),
]
_ORDER = "ABCDEFGHJKLMNPRSTUVWXYZ1234567890"
YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"   # 1980..2009, then again 2010..2039


@dataclass
class VinInfo:
    vin: str
    valid_length: bool
    manufacturer: Optional[str] = None
    country: Optional[str] = None
    year: Optional[int] = None          # model year estimate
    wmi: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def label(self) -> Optional[str]:
        """Short name for a vehicle profile, e.g. "Ford 2013"."""
        if not self.manufacturer:
            return None
        return f"{self.manufacturer} {self.year}" if self.year else self.manufacturer


def _country(wmi: str) -> Optional[str]:
    pair = wmi[:2]
    if len(pair) < 2 or pair[0] not in _ORDER or pair[1] not in _ORDER:
        return None
    for low, high, name in COUNTRIES:
        if pair[0] == low[0] and _ORDER.index(low[1]) <= _ORDER.index(pair[1]) <= _ORDER.index(high[1]):
            return name
    return None


def _year(code: str, today: Optional[date] = None) -> Optional[int]:
    if code not in YEAR_CODES:
        return None
    limit = (today or date.today()).year + 1
    year = 1980 + YEAR_CODES.index(code)
    while year + 30 <= limit:
        year += 30
    return year


def decode(vin: Optional[str], today: Optional[date] = None) -> Optional[VinInfo]:
    """Decode what can be told offline; None when there is no usable VIN."""
    text = "".join(ch for ch in (vin or "").upper() if ch.isalnum())
    if len(text) < 11:
        return None
    wmi = text[:3]
    info = VinInfo(vin=text, valid_length=len(text) == 17, wmi=wmi, country=_country(wmi),
                   manufacturer=WMI.get(wmi) or WMI.get(wmi[:2]))
    # Ford of Europe keeps the year in position 11 (and the month in 12); others use position 10.
    position = 10 if wmi in ("WF0", "VS6", "NM0", "SFA") else 9
    if len(text) > position and (info.manufacturer or info.country):  # no year guess for an unknown WMI
        info.year = _year(text[position], today)
    return info
