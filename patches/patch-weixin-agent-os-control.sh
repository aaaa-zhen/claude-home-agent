#!/bin/bash
# Deprecated: do not install direct home-control logic into the WeChat gateway.
# Agent OS gateway integration must submit user.message events and wait for
# response.created; business logic belongs in Scheduler/Workers/Tools.
set -euo pipefail

echo "[patch] direct Agent OS control gateway is deprecated; leaving bundle unchanged"
