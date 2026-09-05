# -*- coding: utf-8 -*-
"""R19 持续验证采集：API + SQLite，只读观测，严禁改动任何数据/配置。
复用记忆：/api/health 免鉴权；其余需 Bearer token，用 SECRET_KEY 自签 HS256 JWT。
"""
import json, sqlite3, base64, hmac, hashlib, time, urllib.request, urllib.error, datetime

BASE = "http://127.0.0.1:8080"
SECRET = "5b195a9f0e0998b66e51faaeb49c10e36a52246d33e964cfd6696648bbbfa4b6"
USER_ID = "6f50aea4-7879-4d6d-8046-9b9d9f1989a3"
EMAIL = "1558895@qq.com"
DB_PATH = r"F:/WanxiangAI/backend/data/wx_prod.dat"
ACCOUNTS = ("2877213e-e79f-4ac4-93cd-4db64730bc04",
            "b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd",
            "8ecb1ff9-aa09-4057-9f0e-a87434a29bf3",
            "3540bf33-ee40-4169-8099-7c9616406d99")

def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

def make_jwt():
    header = b64u(json.dumps({"alg":"HS256","typ":"JWT"}).encode())
    now = int(time.time())
    payload = b64u(json.dumps({"sub":USER_ID,"email":EMAIL,"exp":now+3600,"iat":now}).encode())
    sig = hmac.new(SECRET.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{b64u(sig)}"

def get(url, token=None, timeout=12):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def post(url, data, timeout=12):
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                 headers={"Content-Type":"application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

# ---------- 1. health ----------
out = {"health": None, "model": None, "accounts": None, "db": None, "error": None}
try:
    out["health"] = get(f"{BASE}/api/health")
except Exception as e:
    out["error"] = f"health: {e}"
    print(json.dumps(out, ensure_ascii=False))
    raise SystemExit

# ---------- 2. auth + model/accounts ----------
try:
    tok = make_jwt()
    out["model"] = get(f"{BASE}/api/local-model/status", tok)
    # 取两次：缓存快照 vs 强刷（stale-while-revalidate 设计）
    acc1 = get(f"{BASE}/api/dashboard/accounts", tok)
    acc2 = get(f"{BASE}/api/dashboard/accounts", tok)
    out["accounts"] = {"first": acc1, "refreshed": acc2}
except Exception as e:
    out["error"] = f"api: {e}"

# ---------- 3. SQLite ----------
try:
    uri = f"file:{DB_PATH}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    cur = con.cursor()
    acc_in = ",".join(f"'{a}'" for a in ACCOUNTS)
    # 列探测
    cols = [r[1] for r in cur.execute("PRAGMA table_info(trades)").fetchall()]
    # 最近已平单（真实4账号，排除外部零值伪造但保留用于展示）
    q_closed = f"""
      SELECT mt5_account_id, action, open_price, close_price, sl, tp, profit, net_profit,
             volume, result, exit_reason, open_time, close_time
      FROM trades
      WHERE mt5_account_id IN ({acc_in})
        AND close_time IS NOT NULL
      ORDER BY close_time DESC LIMIT 60
    """
    closed = cur.execute(q_closed).fetchall()
    # 当前未平单
    q_open = f"""
      SELECT mt5_account_id, action, open_price, sl, tp, volume, profit, open_time, current_price
      FROM trades
      WHERE mt5_account_id IN ({acc_in})
        AND close_time IS NULL
      ORDER BY open_time DESC
    """
    opened = cur.execute(q_open).fetchall()
    # 今日有效单（剔外部零值伪单：profit==0 且 close==open 视为伪单，但DB里close可能被写成sl）
    q_today = f"""
      SELECT mt5_account_id, action, open_price, close_price, sl, profit, volume, result, exit_reason
      FROM trades
      WHERE mt5_account_id IN ({acc_in})
        AND close_time >= '2026-08-11 00:00:00'
    """
    today = cur.execute(q_today).fetchall()
    con.close()
    out["db"] = {"cols": cols, "closed": closed, "opened": opened, "today": today}
except Exception as e:
    out["db_error"] = f"db: {e}"

print(json.dumps(out, ensure_ascii=False, default=str))
