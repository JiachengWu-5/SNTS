#!/usr/bin/env bash
# Start only the SHANYDOC writing page. Start llama-server separately first.
set -euo pipefail
cd "$(dirname "$0")"
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
export NOVEL_WRITER_ARTICLE_ROOT="${NOVEL_WRITER_ARTICLE_ROOT:-$HOME/Article}"
exec python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
