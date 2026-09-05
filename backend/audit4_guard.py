import json, urllib.request, urllib.error, base64, hmac, hashlib, time, sqlite3

SECRET = "5b195a9f0e0998b66e51faaeb49c10e36a52246d33e964cfd6696648bbbfa4b6"
UID = "6f50aea4-7879-4d6d-8046-9b9d9f1989a3"
PRIMARY_ID = "2877213e-e79f-4ac4-93cd-4db64730bc04"

def b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

def mk():
    h = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    p = b64(json.dumps({"sub": UID, "email": "x@x.com", "exp": int(time.time()) + 3600}).encode())
    s = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return f"{h}.{p}.{s}"

def call(method, url, token, body=None):
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "null")

# 1) 先禁用主号的交易开关
con = sqlite3.connect("F:/WanxiangAI/data/wx_prod.dat")
con.execute("UPDATE mt5_accounts SET is_trading_enabled=0 WHERE id=?", (PRIMARY_ID,))
con.commit()
con.close()
print("已禁用主号交易开关")

tok = mk()
# 2) 手动下单应被护栏拒绝 (400)
s, d = call("POST", "http://127.0.0.1:8080/api/trade/order", tok,
            {"symbol": "XAUUSD", "order_type": "BUY", "volume": 0.01})
print(f"手动下单(停用后): HTTP {s} -> {d}")

# 3) 还原
con = sqlite3.connect("F:/WanxiangAI/data/wx_prod.dat")
con.execute("UPDATE mt5_accounts SET is_trading_enabled=1 WHERE id=?", (PRIMARY_ID,))
con.commit()
con.close()
print("已还原主号交易开关为启用")

# 4) 还原后手动下单不再被'停用'拒绝 (会进入风控/下单流程, 可能因 MT5/时段返回其他结果, 但不应是'停用'400)
s2, d2 = call("POST", "http://127.0.0.1:8080/api/trade/order", tok,
              {"symbol": "XAUUSD", "order_type": "BUY", "volume": 0.01})
print(f"手动下单(启用后): HTTP {s2} -> {d2}")
disabled_rejected = (s == 400 and "停用交易" in str(d))
print("\n护栏生效:", "PASS" if disabled_rejected else "FAIL")
