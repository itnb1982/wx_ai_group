import hmac, hashlib, base64, json, time, urllib.request

SECRET = "5b195a9f0e0998b66e51faaeb49c10e36a52246d33e964cfd6696648bbbfa4b6"
USER_ID = "6f50aea4-7879-4d6d-8046-9b9d9f1989a3"

def b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=")

header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
now = int(time.time())
payload = b64url(json.dumps({"sub": USER_ID, "exp": now + 3600, "iat": now}).encode())
sig = b64url(hmac.new(SECRET.encode(), header + b"." + payload, hashlib.sha256).digest())
token = (header + b"." + payload + b"." + sig).decode()

url = "http://127.0.0.1:8080/api/dashboard/debug-history"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
try:
    with urllib.request.urlopen(req, timeout=240) as r:
        data = json.loads(r.read().decode())
    import pprint
    pprint.pprint(data)
except Exception as e:
    print("ERROR:", repr(e))
