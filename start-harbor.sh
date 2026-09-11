#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -f .env ]; then
    set -a
    . ./.env
    set +a
fi
python="${HARBOR_PYTHON:-$PWD/.venv/bin/python}"
case "${1:-mcp}" in
    mcp) exec "$python" -c 'from server_legacy import mcp; import os; mcp.settings.port = int(os.environ.get("HARBOR_MCP_PORT", "8765")); mcp.run(transport="streamable-http")' ;;
    daemon) exec "$python" codex_job_daemon.py ;;
    tunnel) unset CONTROL_PLANE_TUNNEL_ID CONTROL_PLANE_API_KEY
        exec "${HARBOR_TUNNEL_EXE:-tunnel-client}" run --profile-dir "$PWD/.control/tunnel" --profile harness_harbor_mac ;;
    *) echo "Usage: $0 [mcp|daemon|tunnel]" >&2; exit 2 ;;
esac
