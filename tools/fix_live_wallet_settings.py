# -*- coding: utf-8 -*-
"""修改 .env 的实盘资金钱包与签名类型（不打印任何密钥）。

用法:
    python tools/fix_live_wallet_settings.py --funder 0x... --signature-type 3
"""

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--funder", required=True, help="资金钱包（maker）地址")
    parser.add_argument("--signature-type", required=True, help="签名类型：0/1/2/3")
    args = parser.parse_args()

    updates = {
        "PM_FUNDER_ADDRESS": args.funder,
        "PM_SIGNATURE_TYPE": args.signature_type,
    }

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
    for key, value in updates.items():
        print(f"{key} = {value}")
    print(f"written {ENV_PATH}")


if __name__ == "__main__":
    main()
