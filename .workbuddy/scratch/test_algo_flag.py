"""
验证：用 /config: 启动配置（AllowLiveTrading=1）拉起终端后，
      MT5 Python API 看到的 trade_allowed 是否变为 True。
"""
import sys, os, time, sqlite3, json

_BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend")
sys.path.insert(0, _BACKEND)
os.chdir(_BACKEND)  # 让 pydantic-settings 能找到 backend/.env

from app.services.mt5_launcher import ensure_terminal, is_terminal_running  # noqa
from app.utils.crypto import decrypt  # noqa
import MetaTrader5 as mt5  # noqa

db = os.path.expanduser("~/.wanxiangai/wanxiangai.db")
con = sqlite3.connect(db)
cur = con.cursor()
cur.execute("PRAGMA table_info(mt5_accounts)")
cols = [r[1] for r in cur.fetchall()]
cur.execute("SELECT * FROM mt5_accounts")
rows = [dict(zip(cols, r)) for r in cur.fetchall()]
con.close()

# 只测第一个账号（F:\mt5），避免多终端互相干扰
acc = rows[0]
path = acc["terminal_path"]
login = int(acc["account_id"])
pwd = decrypt(acc["password"]) if acc["password"] else ""
server = acc["server"]

print(f"[目标] {acc['name']} login={login} server={server}")
print(f"[路径] {path}")
print(f"[运行中] {is_terminal_running(path)}")

r = ensure_terminal(path, str(login), pwd, server, tag=str(login))
print(f"[启动器] {r}")

ok = mt5.initialize(login=login, password=pwd, server=server, path=path)
print(f"[initialize] {ok}  last_error={mt5.last_error()}")

if ok:
    ti = mt5.terminal_info()
    ai = mt5.account_info()
    d = ti._asdict() if ti else {}
    print("[terminal_info]")
    for k in ("name", "connected", "trade_allowed", "tradeapi_disabled",
              "dlls_allowed", "path", "data_path"):
        print(f"    {k:20s} = {d.get(k)}")
    print(f"[account_info] login={ai.login if ai else None} balance={ai.balance if ai else None}")

    if d.get("trade_allowed"):
        print("\n>>> 成功：算法交易开关已打开，10027 应已解除")
    else:
        print("\n>>> 仍为 False：需要进一步手段")
    mt5.shutdown()
