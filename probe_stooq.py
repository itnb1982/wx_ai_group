import requests
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
for sym, name in [("dxy","DXY"),("vix","VIX"),("xauusd","XAUUSD")]:
    # stooq 最新一日报价接口
    url = f"https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        print(f"[{name}] HTTP {r.status_code} | {r.text.strip()[:120]}")
    except Exception as e:
        print(f"[{name}] ERR {e}")
