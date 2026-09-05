import requests
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
for sym in ["dxy.us","dxy","xauusd","^dxy","gc.f"]:
    url = f"https://stooq.com/q/d/l/?s={sym}&i=d"
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
        txt = r.text.strip().replace("\n"," | ")
        print(f"[{sym}] HTTP {r.status_code} | {txt[:90]}")
    except Exception as e:
        print(f"[{sym}] ERR {e}")
