"""R18 持续验证采集器（只读）：API 接口层 + DB 分析，输出 JSON 到 _r18.json。
严格遵循记忆中的方法论：
  - accounts 取两次（stale-while-revalidate 设计行为，首调必陈旧）
  - 日志中文为 GBK，须 decode('gbk','replace')
  - trades 表无 status/ticket/direction 列；未平判据 close_time IS NULL
  - 只统计真实4账号，排除 acc_exit_0001
"""
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime

BASE = "http://127.0.0.1:8080"
EMAIL = "1558895@qq.com"
PASSWORD = "Tzhl@708090"
TIMEOUT = 30
DB = "F:/WanxiangAI/backend/data/wx_prod.dat"
ACCS = (
    "2877213e-e79f-4ac4-93cd-4db64730bc04",
    "b3db40fd-7a1a-4772-b5c5-8fb83dbed9dd",
    "8ecb1ff9-aa09-4057-9f0e-a87434a29bf3",
    "3540bf33-ee40-4169-8099-7c9616406d99",
)
NAME = {
    "2877213e": "lmc1(跟)",
    "b3db40fd": "lmc2(跟)",
    "8ecb1ff9": "lmc3(跟)",
    "3540bf33": "lmc4(主)",
}

# 硬损阈值（权益敞口%，R16 用 0.157% 曾触及；此处用 MT5 端 harsh 阈值 2% 权益做 P0 判定）
HARD_LOSS_PCT = 2.0

OUT = {}


def nm(a):
    return NAME.get(a[:8], a[:8])


# ---------- 1. API 拉取 ----------
import urllib.error
import urllib.request


def req(method, path, token=None, body=None, timeout=TIMEOUT):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


# health 免鉴权
st, raw = req("GET", "/api/health")
OUT["health"] = {"status": st, "body": raw}

tok = None
if st == 200:
    st2, raw2 = req("POST", "/api/auth/login", body={"email": EMAIL, "password": PASSWORD})
    if st2 == 200:
        try:
            tok = json.loads(raw2).get("access_token")
        except Exception:
            tok = None
OUT["login_ok"] = bool(tok)

if tok:
    st, raw1 = req("GET", "/api/dashboard/accounts", token=tok)
    OUT["accounts_1"] = {"status": st, "body": raw1}
    st, raw2 = req("GET", "/api/dashboard/accounts", token=tok)
    OUT["accounts_2"] = {"status": st, "body": raw2}
    st, raw = req("GET", "/api/local-model/status", token=tok)
    OUT["local_model"] = {"status": st, "body": raw}
else:
    OUT["accounts_1"] = OUT["accounts_2"] = OUT["local_model"] = {"status": 0, "body": ""}

with open("F:/WanxiangAI/backend/_r18_api.json", "w", encoding="utf-8") as f:
    json.dump(OUT, f, ensure_ascii=False)

# ---------- 2. DB 分析 ----------
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15)
con.row_factory = sqlite3.Row
cur = con.cursor()
ph = ",".join("?" * len(ACCS))

db_out = {}

# 未平单
rows = cur.execute(f"""SELECT mt5_account_id,mt5_ticket,action,volume,open_price,sl,tp,profit,open_time
    FROM trades WHERE mt5_account_id IN ({ph}) AND close_time IS NULL ORDER BY open_time DESC""", ACCS).fetchall()
open_positions = []
for x in rows:
    open_positions.append({
        "acc": nm(x["mt5_account_id"]), "ticket": x["mt5_ticket"], "action": x["action"],
        "vol": x["volume"], "open": x["open_price"], "sl": x["sl"], "tp": x["tp"],
        "profit": x["profit"], "open_time": str(x["open_time"])[:19],
    })
db_out["open_positions"] = open_positions

# 今日已平 + SL伪造检测
t = cur.execute(f"""SELECT mt5_account_id,mt5_ticket,action,volume,open_price,close_price,profit,
    exit_reason,open_time,close_time,sl,tp,mfe,mae,result,meta_agent_confidence,q_score,chronos_vote
    FROM trades WHERE mt5_account_id IN ({ph}) AND close_time IS NOT NULL
    AND date(close_time) >= '2026-08-11' ORDER BY close_time""", ACCS).fetchall()
forged, clean = [], []
for x in t:
    cp, sl, op, vol = x["close_price"], x["sl"], x["open_price"], x["volume"]
    isf = False
    if cp is not None and sl is not None and abs(cp - sl) < 1e-6:
        isf = True
    if cp is not None and op is not None and abs(cp - op) < 1e-6:
        isf = True
    rec = {
        "acc": nm(x["mt5_account_id"]), "ticket": x["mt5_ticket"], "action": x["action"],
        "vol": vol, "open": op, "close": cp, "sl": sl, "tp": x["tp"],
        "profit": x["profit"], "reason": x["exit_reason"], "close_time": str(x["close_time"])[:19],
    }
    (forged if isf else clean).append(rec)
db_out["today_total"] = len(t)
db_out["forged"] = forged
db_out["clean"] = clean
db_out["forged_sum"] = sum((x["profit"] or 0) for x in forged)
db_out["clean_sum"] = sum((x["profit"] or 0) for x in clean)

# 出场归因（分伪造/正常）
agg = defaultdict(lambda: [0, 0.0, 0])
for x in t:
    cp, sl, op = x["close_price"], x["sl"], x["open_price"]
    isf = (cp is not None and sl is not None and abs(cp - sl) < 1e-6) or \
          (cp is not None and op is not None and abs(cp - op) < 1e-6)
    key = (x["exit_reason"] or "None") + ("[伪造]" if isf else "")
    a = agg[key]
    a[0] += 1
    a[1] += x["profit"] or 0
    if (x["profit"] or 0) > 0:
        a[2] += 1
db_out["exit_attrib"] = {k: {"n": v[0], "net": round(v[1], 2), "win": v[2],
                             "winrate": round(v[2] / v[0] * 100, 1) if v[0] else 0,
                             "avg": round(v[1] / v[0], 2) if v[0] else 0}
                         for k, v in sorted(agg.items(), key=lambda i: i[1][1])}

# 按账号
acc_stat = {}
for a in ACCS:
    sub = [x for x in clean if x["acc"] == nm(a)]
    if not sub:
        acc_stat[nm(a)] = {"n": 0}
        continue
    w = [x["profit"] or 0 for x in sub if (x["profit"] or 0) > 0]
    l = [x["profit"] or 0 for x in sub if (x["profit"] or 0) <= 0]
    gp, gl = sum(w), abs(sum(l))
    acc_stat[nm(a)] = {"n": len(sub), "win": len(w), "net": round(gp - gl, 2),
                       "pf": round(gp / gl, 3) if gl else 0,
                       "winrate": round(len(w) / len(sub) * 100, 1)}
db_out["acc_stat"] = acc_stat

# 特征填充
tot = len(t)
feat = {}
for col in ("mfe", "mae", "chronos_vote", "q_score", "meta_agent_confidence"):
    c = sum(1 for x in t if x[col] is not None and x[col] != 0)
    feat[col] = f"{c}/{tot} = {round(c/tot*100,1) if tot else 0}%"
db_out["feat"] = feat

# 最近平仓 12 笔
r = cur.execute(f"""SELECT mt5_account_id,mt5_ticket,action,volume,open_price,close_price,profit,
    exit_reason,close_time,sl,tp FROM trades WHERE mt5_account_id IN ({ph})
    AND close_time IS NOT NULL ORDER BY close_time DESC LIMIT 12""", ACCS).fetchall()
recent = []
for x in r:
    cp, sl = x["close_price"], x["sl"]
    f = True if (cp is not None and sl is not None and abs(cp - sl) < 1e-6) else False
    recent.append({"close_time": str(x["close_time"])[:19], "acc": nm(x["mt5_account_id"]),
                   "ticket": x["mt5_ticket"], "action": x["action"], "vol": x["volume"],
                   "close": cp, "sl": sl, "tp": x["tp"], "profit": x["profit"],
                   "reason": x["exit_reason"], "forged": f})
db_out["recent"] = recent

con.close()

with open("F:/WanxiangAI/backend/_r18_db.json", "w", encoding="utf-8") as f:
    json.dump(db_out, f, ensure_ascii=False)

print("OK api+db collected")
print(f"health_status={st} login_ok={bool(tok)}")
print(f"open_positions={len(open_positions)} today_total={len(t)} forged={len(forged)} clean={len(clean)}")
