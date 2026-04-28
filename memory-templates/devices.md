# 智能家居设备清单

> 列出所有接入 Home Assistant 的设备，agent 通过 entity_id 控制它们。

## 示例格式

### 客厅

| 设备 | entity_id | 备注 |
|------|-----------|------|
| 空调 | `climate.living_room_ac` | 支持制冷/制热/除湿 |
| 主灯 | `light.living_room_main` | 支持亮度和色温 |
| 温湿度传感器 | `sensor.living_room_temperature` | |

### 卧室

| 设备 | entity_id | 备注 |
|------|-----------|------|
| 空调 | `climate.bedroom_ac` | |
| 灯 | `light.bedroom` | |

> 请根据你的实际设备替换以上内容。可通过 HA API 查询所有实体：
> `curl -s "$HA_URL/states" -H "Authorization: Bearer $HA_TOKEN" | python3 -m json.tool`
