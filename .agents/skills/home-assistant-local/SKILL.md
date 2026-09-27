---
name: home-assistant-local
description: Query or control Zhen's Home Assistant devices, including air conditioners, lights, presence, doors, media players, temperatures, and device status. Prefer the local LAN HA API/MCP.
---

# Home Assistant Local

Use this skill for smart home status or control requests: air conditioners, lights, door sensor, presence, room temperature, media players, curtains, and HA automations.

## Routing

1. Prefer Codex MCP tools if available:
   - `ha_get_state`
   - `ha_list_entities`
   - `ha_call_service`
2. Otherwise use `.env`:
   - `HA_URL` should point to local LAN HA, normally `http://192.168.1.100:8123/api`.
   - Use `--noproxy 192.168.1.100` with curl.
3. Do not use `https://your-ha-domain.example.com` or `ha_run.sh` for normal status/control while this Mac is at home. Use those only as explicit fallback if the local LAN API fails.

## Device Names

Use human-friendly names from `config.json` and `memory/devices.md`.

Common climate entities:

- `climate.gree` — 客厅空调
- `climate.gree_e6d9` — 主卧空调
- `climate.studioroom` — 书房空调

## Control Rules

- For AC on + set temperature, call `climate.turn_on` first, then `climate.set_temperature`.
- For AC off, prefer `climate.set_hvac_mode` with `{"hvac_mode":"off"}`.
- After controlling a device, verify the final state before replying.
- Ask for confirmation before high-impact or sensitive actions.

## Reply Style

Reply briefly in Chinese. Do not mention curl, API URLs, tokens, scripts, MCP, retries, or internal files unless the user asks how it works.
