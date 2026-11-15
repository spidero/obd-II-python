# Contributing to OBD Tool

Thanks for helping! Bug reports, reports of which cars and adapters work, translations and code are
all welcome. By taking part you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

## Reporting a problem

Open an [issue](https://github.com/spidero/obd-II-python/issues/new/choose) and pick a template.
The most useful details are:

- `obd-tool --version`, your operating system and how you installed it (pip/pipx, venv)
- the adapter (brand, model, USB/Bluetooth) and the output of `obd-tool ports`
- the car (make, model, year, petrol/diesel) and the protocol shown by `obd-tool info`
- the log of the failing command with `-v`, e.g. `obd-tool -v info`

**Do not post your VIN** or other personal data – remove them from logs and screenshots.

Security problems: please follow [SECURITY.md](SECURITY.md) instead of opening a public issue.

## Development setup

```bash
git clone https://github.com/spidero/obd-II-python.git
cd obd-II-python
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

No car is needed: the built-in ELM327 simulator (`--sim`) runs everything, and the whole test suite
uses it. Try a change by hand with, for example, `obd-tool --sim web` or
`obd-tool --sim --sim-fault lean health`.

## Guidelines

- **Safety first.** Never send mode 04 (clear trouble codes) or any other writing command to a real
  car from tests or automatic code paths. Clearing codes always needs an explicit confirmation.
- The program, its messages and the code comments are in **English**. The Polish UI translation lives
  in `src/obd_tool/web/static/pl.json`; `tests/test_i18n.py` checks that every UI text is translated.
- The web UI is plain HTML/CSS/JavaScript with **no CDN and no external libraries** – it has to work
  offline in a car.
- Keep the core (`src/obd_tool/core`) free of UI code; the CLI, the web UI and the desktop GUI all use it.
- Add or update tests for every change and keep `pytest` green.
- Keep pull requests small and focused, and describe how you tested them (simulator or a real car –
  which car, adapter and protocol).

## License

By contributing you agree that your contribution is released under the project's license,
[GPL-2.0-or-later](LICENSE).
