#!/usr/bin/env bash
# Launch the Claude Console server.
# Read-only control panel for ~/.claude — binds 127.0.0.1 only.
set -euo pipefail

# cd to this script's directory so `python -m claude_console.server` resolves
# the package regardless of where run.sh was invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

exec python3 -m claude_console.server "$@"
