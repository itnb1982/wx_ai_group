import requests, jwt, time, json
SECRET="5b195a9f0e0998b66e51faaeb49c10e36a52246d33e964cfd6696648bbbfa4b6"
UID="6f50aea4-7879-4d6d-8046-9b9d9f1989a3"
tok = jwt.encode({"sub": UID, "exp": int(time.time())+3600}, SECRET, algorithm="HS256")
h = {"Authorization": f"Bearer {tok}"}
r = requests.get("http://127.0.0.1:8081/api/dashboard/market-chart?tf=H1", headers=h, timeout=90)
d = r.json()
print("HTTP", r.status_code)
print("macro:", json.dumps(d.get("macro"), ensure_ascii=False)[:500])
print("trend:", d.get("trend"))
