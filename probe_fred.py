import requests
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
url = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTWEXBGS"
try:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    print("HTTP", r.status_code)
    lines = [l for l in r.text.strip().splitlines() if l.strip()]
    print("HEAD:", lines[0])
    print("TAIL:", lines[-1])
    print("ROWS:", len(lines)-1)
except Exception as e:
    print("ERR", e)
