#!/usr/bin/env python3
"""顺丰沙箱下单脚本"""
import hashlib, base64, time, urllib.request, urllib.parse, json, uuid, sys, os

PARTNER_ID = os.getenv("SF_PARTNER_ID", "")
CHECK_WORD = os.getenv("SF_CHECK_WORD", "")
API_URL = os.getenv("SF_API_URL", "https://sfapi-sbox.sf-express.com/std/service")

def call_sf(service_code, msg_data_dict):
    ts = str(int(time.time()))
    request_id = uuid.uuid4().hex[:20]
    msg_data = json.dumps(msg_data_dict, ensure_ascii=False)
    digest = base64.b64encode(hashlib.md5((msg_data + ts + CHECK_WORD).encode()).digest()).decode()
    params = urllib.parse.urlencode({
        "partnerID": PARTNER_ID, "requestID": request_id,
        "serviceCode": service_code, "timestamp": ts,
        "msgDigest": digest, "msgData": msg_data
    }).encode()
    req = urllib.request.Request(API_URL, data=params,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"})
    resp = urllib.request.urlopen(req, timeout=15)
    body = json.loads(resp.read().decode())
    if body.get("apiResultCode") != "A1000":
        return {"success": False, "error": body.get("apiErrorMsg"), "code": body.get("apiResultCode")}
    result = json.loads(body["apiResultData"])
    if not result.get("success"):
        return {"success": False, "error": result.get("errorMsg") or result.get("errorMessage"), "code": result.get("errorCode")}
    return {"success": True, "data": result.get("msgData", result)}

def create_order(sender_name, sender_mobile, sender_province, sender_city, sender_county, sender_address,
                 receiver_name, receiver_mobile, receiver_province, receiver_city, receiver_county, receiver_address,
                 cargo_name, cargo_weight=1.0, express_type=1):
    order_id = "wx-" + str(int(time.time()))
    return call_sf("EXP_RECE_CREATE_ORDER", {
        "language": "zh-CN",
        "orderId": order_id,
        "expressTypeId": express_type,
        "payMethod": 1,
        "parcelQty": 1,
        "totalWeight": cargo_weight,
        "isReturnRoutelabel": 1,
        "isGenWaybillNo": 1,
        "contactInfoList": [
            {"contactType": 1, "contact": sender_name, "mobile": sender_mobile, "country": "CN",
             "province": sender_province, "city": sender_city, "county": sender_county, "address": sender_address},
            {"contactType": 2, "contact": receiver_name, "mobile": receiver_mobile, "country": "CN",
             "province": receiver_province, "city": receiver_city, "county": receiver_county, "address": receiver_address}
        ],
        "cargoDetails": [{"name": cargo_name, "count": 1, "weight": cargo_weight}]
    })

if __name__ == "__main__":
    # 示例：从命令行读取 JSON 参数
    if len(sys.argv) > 1:
        params = json.loads(sys.argv[1])
        result = create_order(**params)
    else:
        # 默认测试（示例数据，请替换为真实寄收件信息）
        result = create_order(
            "张三", "13800000000", "广东省", "深圳市", "南山区", "科技园某路1号",
            "李四", "13900000000", "北京市", "北京市", "海淀区", "中关村某大厦2号",
            "笔记本电脑", 2.0
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
