import requests
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
url = "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv"
try:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    print("HTTP", r.status_code, "bytes", len(r.content))
    lines = r.text.strip().splitlines()
    print("HEAD:", lines[0])
    print("TAIL:", lines[-1])
    print("ROWS:", len(lines)-1)
except Exception as e:
    print("ERR", e)
