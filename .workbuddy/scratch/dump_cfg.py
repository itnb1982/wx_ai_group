import os, sys

BASES = [
    r"C:\Users\15588\AppData\Roaming\MetaQuotes\Terminal\C54ABD31694C1B0FC8715C4F2B20FBAF\config",
    r"C:\Users\15588\AppData\Roaming\MetaQuotes\Terminal\D8E196A488CAFD45BB0BBB0BC09A258A\config",
    r"C:\Users\15588\AppData\Roaming\MetaQuotes\Terminal\5F9A67BD35361E686BC6A4D01A0B18D4\config",
]

def read_text(p):
    raw = open(p, "rb").read()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-16")
    except Exception:
        return raw.decode("utf-8", errors="replace")

target = sys.argv[1] if len(sys.argv) > 1 else "settings.ini"
only_first = "--all" not in sys.argv

for base in (BASES[:1] if only_first else BASES):
    p = os.path.join(base, target)
    print("=" * 20, p, "=" * 20)
    if not os.path.exists(p):
        print("(not exists)")
        continue
    print(read_text(p))
