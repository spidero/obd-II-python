import pytest
from fastapi.testclient import TestClient

from obdruid.core import ConnectionSettings, dtc_i18n
from obdruid.i18n import tr
from obdruid.web.server import create_app


def test_every_description_is_translated():
    done, total, missing = dtc_i18n.coverage("pl")
    assert total > 1700 and missing == []


@pytest.mark.parametrize("english,polish", [
    ("System Too Lean", "Mieszanka za uboga"),
    ("Cylinder 4 Misfire Detected", "Cylinder 4 – wykryto wypadanie zapłonów"),
    ("Camshaft Position Sensor 'A' Circuit", "Czujnik położenia wałka rozrządu A – usterka obwodu"),
    ("Intake Air Temperature Sensor 1 Circuit High", "Czujnik temperatury powietrza dolotowego 1 – obwód: sygnał wysoki"),
    ("Lost Communication With TCM", "Utrata komunikacji: Sterownik skrzyni biegów (TCM)"),
    ("Fuel Rail/System Pressure - Too Low", "Ciśnienie paliwa w listwie/układzie – wartość za niska"),
    ("High Speed CAN Communication Bus (shorted to Bus)", "Szybka magistrala CAN – zwarcie do magistrali"),
])
def test_examples(english, polish):
    assert dtc_i18n.translate(english, "pl") == polish


def test_split():
    assert dtc_i18n.split("O2 Sensor Circuit Low Voltage (Bank 1 Sensor 2)") == (
        "", "O2 Sensor", "Circuit Low Voltage", "Bank 1 Sensor 2", [])
    assert dtc_i18n.split("Lost Communication With 'Door Control Module A'") == (
        "Lost Communication With", "Door Control Module {1}", "", "", ["A"])
    assert dtc_i18n.translate("O2 Sensor Circuit Low Voltage (Bank 1 Sensor 2)", "pl") == (
        "Sonda lambda – obwód: niskie napięcie (bank 1 czujnik 2)")


def test_unknown_text_and_english_are_unchanged():
    assert dtc_i18n.translate("Flux Capacitor Circuit Low", "pl") == "Flux Capacitor Circuit Low"
    assert dtc_i18n.translate("System Too Lean", "en") == "System Too Lean"
    assert dtc_i18n.translate("", "pl") == ""


def test_server_side_tr_and_api():
    assert tr("System Too Rich", "pl") == "Mieszanka za bogata"
    assert tr("System Too Rich", "en") == "System Too Rich"
    with TestClient(create_app(ConnectionSettings(simulate=True), connect=False)) as c:
        data = c.get("/api/i18n/dtc?lang=pl").json()["descriptions"]
        assert data["Catalyst System Efficiency Below Threshold"] == "Układ katalizatora – sprawność poniżej progu"
        assert len(data) > 1700
        assert c.get("/api/i18n/dtc?lang=en").json()["descriptions"] == {}
