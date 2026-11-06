#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$PROJECT_DIR"
trap 'printf "\nPressione Enter para fechar. "; read -r resposta || true' EXIT
./radio-lote
