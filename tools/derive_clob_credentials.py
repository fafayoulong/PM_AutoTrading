# -*- coding: utf-8 -*-
"""用 .env 里的 PM_PRIVATE_KEY 派生 Polymarket CLOB API 凭据。

用法:
    python tools/derive_clob_credentials.py          # 只显示，不修改 .env
    python tools/derive_clob_credentials.py --write  # 把派生结果写回 .env
"""

import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
HOST = "https://clob.polymarket.com"
CHAIN_ID = 137


def load_env(path: Path) -> dict:
    values = {}
    if not path.exists():
        raise SystemExit(f"找不到 {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        values[key.strip()] = raw.strip().strip('"').strip("'")
    return values


def mask(value: str, keep: int = 8) -> str:
    if not value:
        return "(empty)"
    return f"{value[:keep]}..."


def write_env(updates: dict) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    seen = set()
    output = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                output.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        output.append(line)
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(output) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="把派生结果写回 .env")
    args = parser.parse_args()

    values = load_env(ENV_PATH)
    private_key = values.get("PM_PRIVATE_KEY", "")
    if not private_key:
        raise SystemExit(".env 里缺少 PM_PRIVATE_KEY")

    proxy = values.get("PM_PROXY") or os.getenv("PM_PROXY") or "http://127.0.0.1:7897"
    if proxy:
        os.environ.setdefault("HTTP_PROXY", proxy)
        os.environ.setdefault("HTTPS_PROXY", proxy)
        print(f"proxy      = {proxy}")

    from eth_account import Account

    signer = Account.from_key(private_key).address
    funder = values.get("PM_FUNDER_ADDRESS", "")
    signature_type = int(values.get("PM_SIGNATURE_TYPE", "0") or 0)
    print(f"signer     = {signer}")
    print(f"funder     = {funder or '(none)'}")
    print(f"signature  = {signature_type}")

    from py_clob_client.client import ClobClient

    client = ClobClient(
        HOST,
        key=private_key,
        chain_id=CHAIN_ID,
        signature_type=signature_type,
        funder=funder or None,
    )

    if hasattr(client, "create_or_derive_api_creds"):
        creds = client.create_or_derive_api_creds()
    else:
        creds = client.create_or_derive_api_key()

    if not creds:
        raise SystemExit("派生失败：未返回凭据。请检查 VPN/代理是否能连上 clob.polymarket.com")

    print()
    print("派生结果（掩码显示）:")
    print("  PM_API_KEY        =", mask(creds.api_key))
    print("  PM_API_SECRET     =", mask(creds.api_secret))
    print("  PM_API_PASSPHRASE =", mask(creds.api_passphrase))

    current = values.get("PM_API_KEY", "")
    print()
    if current == creds.api_key:
        print("与 .env 中的 PM_API_KEY 一致，无需更新。")
        return

    print("与 .env 中的 PM_API_KEY 不一致。")
    if args.write:
        write_env(
            {
                "PM_API_KEY": creds.api_key,
                "PM_API_SECRET": creds.api_secret,
                "PM_API_PASSPHRASE": creds.api_passphrase,
            }
        )
        print(f"已写回 {ENV_PATH}")
    else:
        print("如需写回，请重新运行：python tools/derive_clob_credentials.py --write")


if __name__ == "__main__":
    main()
