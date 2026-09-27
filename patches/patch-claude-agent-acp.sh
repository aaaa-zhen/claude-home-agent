#!/bin/bash
# Reapply transport correlation after dependency installation or service startup.
set -euo pipefail
exec /opt/homebrew/bin/node "$(dirname "$0")/patch-acp-turn-router.mjs"
