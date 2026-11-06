#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
PYTHON_BIN="${RADIO_LOTE_PYTHON:-python3}"
cd "$PROJECT_DIR"
"$PYTHON_BIN" -m venv .venv
if [[ -f requirements.lock ]]; then
  .venv/bin/python -m pip install -r requirements.lock
  .venv/bin/python -m pip install --no-deps -e .
else
  .venv/bin/python -m pip install -e .
fi
echo "Pronto. Execute ./radio-lote para abrir o painel ou ./radio-lote doctor para conferir a instalação."
