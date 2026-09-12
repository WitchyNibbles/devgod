#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
uv run --locked ruff check src tests scripts
uv run --locked mypy
uv run --locked pytest -m 'not live' "$@"
