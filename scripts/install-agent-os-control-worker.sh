#!/bin/bash
# Install the Agent OS control worker as a per-user launchd service.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
exec "$ROOT/scripts/install-agent-os-workers.sh" control
