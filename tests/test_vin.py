from datetime import date

import pytest

from obdruid.core.storage import Storage
from obdruid.core.vin import decode

TODAY = date(2026, 11, 15)


@pytest.mark.parametrize("vin,maker,country,year", [
    ("WF0XXXTTFXDM89830", "Ford", "Germany", 2013),      # Ford of Europe: year in position 11
    ("WVWZZZ1KZAW000001", "Volkswagen", "Germany", 2010),
    ("TMBJJ7NE8G0123456", "Škoda", "Czechia", 2016),
    ("VF1RFB00X56789012", "Renault", "France", 2005),
    ("1HGCM82633A004352", "Honda", "United States", 2003),
    ("JTDKB20U093512345", "Toyota", "Japan", 2009),
    ("SUPB01CEHYW000001", "FSO", "Poland", 2000),
])
def test_decode(vin, maker, country, year):
    info = decode(vin, TODAY)
    assert (info.manufacturer, info.country, info.year) == (maker, country, year)
    assert info.valid_length and info.label == f"{maker} {year}"


def test_short_unknown_and_missing():
    info = decode("WF0XXXTTFXDM8983", TODAY)           # 16 characters, as one real car reports it
    assert not info.valid_length and info.manufacturer == "Ford" and info.year == 2013
    unknown = decode("XXXZZZ1KZAW000001", TODAY)
    assert unknown.manufacturer is None and unknown.label is None
    assert decode(None) is None and decode("SIM") is None
    assert decode("wvw zzz-1kz aw000001", TODAY).manufacturer == "Volkswagen"


def test_year_never_in_the_future():
    assert decode("WVWZZZ1KZWW000001", TODAY).year == 1998   # "W" = 1998 or 2028; 2028 is still in the future
    assert decode("WVWZZZ1KZTW000001", TODAY).year == 2026   # the current model year is fine
    assert decode("WVWZZZ1KZSW000001", date(2025, 6, 1)).year == 2025


def test_new_profile_is_named_from_the_vin(tmp_path):
    st = Storage(tmp_path)
    st.add_record(st.active()["id"], "health", "x", {})    # the default profile is already used
    assert st.select_for_vin("WF0XXXTTFXDM89830")["name"].startswith("Ford 20")
    assert st.select_for_vin("XXXZZZ1KZAW000001")["name"] == "Car 000001"
