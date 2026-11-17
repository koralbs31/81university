#!/bin/sh
set -eu
export PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8
# Kubernetes owns HOSTNAME; use a separate setting for the listening interface.
exec .venv/bin/uvicorn app:app --host "${LEARNHOUSE_BIND_HOST:-0.0.0.0}" --port "${LEARNHOUSE_PORT:-9000}" --timeout-keep-alive 75
