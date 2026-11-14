#!/usr/bin/env bash
# Start only the SHANYDOC writing page. Start llama-server separately first.
set -euo pipefail
cd "$(dirname "$0")"
exec python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
