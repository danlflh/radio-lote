# Contributing to radio-lote

Changes should keep the programming path conservative and the operator's
workflow clear. Discuss support for a different radio model before adding it:
the current byte-preservation rules belong to the BF-888/H777 driver.

## Local development

Use Python 3.12. No container or physical radio is required for development.

```sh
git clone https://github.com/danlflh/radio-lote.git
cd radio-lote
python -m venv .venv
```

Activate it with `source .venv/bin/activate` on macOS/Linux, or
`.venv\Scripts\Activate.ps1` in Windows PowerShell. Then:

```sh
python -m pip install -r requirements.lock
python -m pip install -e ".[dev]"
python -m unittest discover -s tests -v
```

The lock and package metadata contain the same pinned runtime requirements,
including the official CHIRP source archive and its SHA-256. Keep both in sync.
Build tools are development dependencies, not runtime dependencies.

## Working on a change

- Preserve an individual backup before any upload. A successful upload alone
  must never mark a unit verified.
- After an ambiguous write or failed readback, retry verification only. A user
  retry must not silently repeat an upload.
- Keep port discovery separate from opening a port. Identification describes
  driver compatibility; it does not identify an individual radio.
- Keep UI operations routed through the same services used by the CLI. Test
  screens with Textual's headless tools rather than real serial hardware.
- Use synthetic fixtures. Never commit customer images, exports, logs, serial
  identifiers, local absolute paths, credentials or personal data.
- Add tests for changed behavior and failure cases. Avoid tests that require
  network access or a radio after dependencies are installed.

The suite covers image preservation, persistent backups, interrupted writes,
verification-only retries, batch data, and the real CHIRP driver against an
in-memory serial emulator. Keep the `E`/`F` exit-response regression: some
compatible radios leave an unread `F` after clone mode ends.

## Package checks

```sh
python -m build
```

Set `RADIO_LOTE_WHEEL=dist` in your shell, then run:

```sh
python -m unittest tests.test_packaging -v
```

This inspects the built wheel rather than assuming the source tree's ignore
rules control package contents. The GitHub Actions workflow performs this
check and installed-package smoke checks on macOS, Windows and Linux.

Do not publish an artifact with real customer data or unexplained absolute
paths. Follow [the release checklist](docs/RELEASING.md) for a release.

## Pull requests and reports

Describe the concrete trigger, resulting behavior and checks performed.
Separate emulator/offline results from physical hardware validation. Include
the operating system, application version and driver revision in bug reports;
redact customer names and inspect attachments before posting them.

Contributions are accepted under GPL-3.0-or-later, the project's license.
For security-sensitive reports, follow [SECURITY.md](SECURITY.md).
