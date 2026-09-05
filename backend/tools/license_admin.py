"""
授权签发工具 —— ★ 平台侧专用，绝不能进客户安装包 ★

这个脚本会接触 **私钥**。私钥一旦泄漏，任何人都能签出永久授权，
整套商业授权体系当场归零、且无法通过升级挽回（已签发的令牌永远有效）。
所以：
  · 私钥默认写到项目外的 .secrets/ 并已加入 .gitignore
  · 打包客户端时，tools/ 整个目录必须排除（见 build 脚本白名单）
  · 换私钥 = 所有老客户令牌失效，必须重新签发，非万不得已不做

用法：
    # 1) 首次：生成密钥对（会提示把公钥指纹写进 token.py）
    python tools/license_admin.py genkey

    # 2) 签发一张未绑定机器的授权（客户激活时再绑）
    python tools/license_admin.py issue --customer "张三" --edition standard --days 365

    # 3) 签发绑定令牌（客户激活流程里由服务端调用，这里可手工补发）
    python tools/license_admin.py issue --customer "张三" --edition pro --days 365 \
        --accounts 10 --machine board=<hash>,cpu=<hash>,mac=<hash>

    # 4) 查看一张令牌里到底写了什么（不需要私钥）
    python tools/license_admin.py inspect --token WXAI1.xxx.yyy
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

SECRETS_DIR = PROJECT_ROOT / ".secrets"
PRIVATE_KEY_FILE = SECRETS_DIR / "license_private.pem"
PUBLIC_KEY_FILE = BACKEND_DIR / "app" / "licensing" / "keys" / "license_public.pem"


def cmd_genkey(args) -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if PRIVATE_KEY_FILE.exists() and not args.force:
        print(f"[!] 私钥已存在：{PRIVATE_KEY_FILE}")
        print("    覆盖会使所有已签发授权失效。确定要重来请加 --force")
        return 1

    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()

    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)

    PRIVATE_KEY_FILE.write_bytes(
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    PUBLIC_KEY_FILE.write_bytes(
        pub.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    digest = hashlib.sha256(raw).hexdigest()

    print(f"[+] 私钥（平台保管，勿外传）: {PRIVATE_KEY_FILE}")
    print(f"[+] 公钥（随客户端分发）  : {PUBLIC_KEY_FILE}")
    print()
    print("[!] 请把下面这行写进 app/licensing/token.py 的 EXPECTED_PUBLIC_KEY_SHA256：")
    print(f'    EXPECTED_PUBLIC_KEY_SHA256 = "{digest}"')
    return 0


def _load_private():
    from cryptography.hazmat.primitives.serialization import load_pem_private_key

    if not PRIVATE_KEY_FILE.exists():
        print(f"[x] 找不到私钥 {PRIVATE_KEY_FILE}，先跑 genkey")
        sys.exit(2)
    return load_pem_private_key(PRIVATE_KEY_FILE.read_bytes(), password=None)


def _parse_machine(s: str) -> dict:
    """解析 board=xxx,cpu=yyy,mac=zzz"""
    out = {}
    for part in (s or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            if k in ("board", "cpu", "mac"):
                out[k] = v.strip()
    return out


def cmd_issue(args) -> int:
    from app.licensing.token import build_payload, generate_license_key, sign_payload

    priv = _load_private()
    key = args.key or generate_license_key()
    valid_from = datetime.utcnow()
    valid_until = None if args.days <= 0 else valid_from + timedelta(days=args.days)

    payload = build_payload(
        license_key=key,
        edition=args.edition,
        customer=args.customer,
        max_accounts=args.accounts,
        valid_from=valid_from,
        valid_until=valid_until,
        machine=_parse_machine(args.machine),
        activation_id=args.activation_id or "",
    )
    token = sign_payload(payload, priv)

    print("授权码 :", key)
    print("客户   :", args.customer or "(未填)")
    print("档位   :", args.edition, f"（{args.accounts} 个账号）")
    print("有效期 :", "永久" if valid_until is None else f"{args.days} 天，至 {valid_until:%Y-%m-%d}")
    print("绑定机器:", payload["machine"] or "未绑定（首次激活时绑定）")
    print()
    print("令牌（发给客户 / 写入客户端）：")
    print(token)

    if args.out:
        Path(args.out).write_text(token, encoding="utf-8")
        print(f"\n[+] 已写入 {args.out}")
    return 0


def cmd_inspect(args) -> int:
    from app.licensing.token import verify_token

    token = args.token
    if args.file:
        token = Path(args.file).read_text(encoding="utf-8").strip()
    try:
        claims = verify_token(token)
    except Exception as e:
        print(f"[x] 验签失败：{e}")
        return 1
    print(json.dumps(claims.to_dict(), ensure_ascii=False, indent=2))
    print("原始 payload:")
    print(json.dumps(claims.raw, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="万象Ai 授权签发工具（平台侧）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("genkey", help="生成 Ed25519 密钥对")
    g.add_argument("--force", action="store_true", help="覆盖已有私钥（会作废全部已签发授权）")
    g.set_defaults(func=cmd_genkey)

    i = sub.add_parser("issue", help="签发授权令牌")
    i.add_argument("--customer", default="", help="客户名称")
    i.add_argument("--edition", default="standard", choices=["trial", "standard", "pro"])
    i.add_argument("--days", type=int, default=365, help="有效天数，0 = 永久")
    i.add_argument("--accounts", type=int, default=1, help="可绑定的 MT5 账号数配额")
    i.add_argument("--key", default="", help="指定授权码，留空自动生成")
    i.add_argument("--machine", default="", help="绑定机器指纹 board=..,cpu=..,mac=..")
    i.add_argument("--activation-id", default="", dest="activation_id")
    i.add_argument("--out", default="", help="令牌写入文件")
    i.set_defaults(func=cmd_issue)

    s = sub.add_parser("inspect", help="查看令牌内容（只需公钥）")
    s.add_argument("--token", default="")
    s.add_argument("--file", default="")
    s.set_defaults(func=cmd_inspect)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
