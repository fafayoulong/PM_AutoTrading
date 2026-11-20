# -*- coding: utf-8 -*-
"""用官方 polymarket SDK 下小额测试单（兼容存款钱包 / 会话密钥签名）。

用法:
    python tools/place_test_order_official.py            # 只做只读体检
    python tools/place_test_order_official.py --post     # 真正提交限价单
"""

import argparse
import os
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

# 2026-09-10 网球场次，Philip Sekulic 一侧；价格低于买一，挂单不立即成交
DEFAULT_TOKEN_ID = "61626157277164849706878735144520069914007661982533925197847012235874601600407"
DEFAULT_PRICE = Decimal("0.40")
DEFAULT_SIZE = Decimal("5")


def load_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw = line.split("=", 1)
        values[key.strip()] = raw.strip().strip('"').strip("'")
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token-id", default=DEFAULT_TOKEN_ID)
    parser.add_argument("--price", type=Decimal, default=DEFAULT_PRICE)
    parser.add_argument("--size", type=Decimal, default=DEFAULT_SIZE)
    parser.add_argument("--post", action="store_true", help="真正提交订单")
    args = parser.parse_args()

    values = load_env(ENV_PATH)
    proxy = values.get("PM_PROXY", "http://127.0.0.1:7897")
    os.environ.setdefault("HTTP_PROXY", proxy)
    os.environ.setdefault("HTTPS_PROXY", proxy)

    from polymarket import RelayerApiKey, SecureClient

    client = SecureClient.create(
        private_key=values["PM_PRIVATE_KEY"],
        wallet=values["PM_FUNDER_ADDRESS"],
        api_key=RelayerApiKey(
            key=values["PM_RELAYER_API_KEY"],
            address=values["PM_RELAYER_API_KEY_ADDRESS"],
        ),
    )

    ctx = client._ctx
    print(f"wallet_type = {ctx.wallet_type}")
    print(f"signer_type = {ctx.signer_type}")
    print(f"wallet      = {ctx.wallet}")
    print(f"signer      = {ctx.signer.address}")
    print(f"gasless     = {client.is_gasless_ready()}")
    try:
        print(f"balance     = {client.get_balance_allowance(asset_type='COLLATERAL')}")
    except Exception as exc:  # noqa: BLE001
        print(f"balance FAIL: {type(exc).__name__}: {exc}")

    cost = args.size * args.price
    print()
    print(f"BUY {args.size} shares @ {args.price} = {cost:.2f} USDC")

    if not args.post:
        print("dry-run：未提交。确认无误后加 --post 提交。")
        client.close()
        return

    response = client.place_limit_order(
        token_id=args.token_id,
        price=args.price,
        size=args.size,
        side="BUY",
    )
    print("== 提交结果 ==")
    print(response)
    client.close()


if __name__ == "__main__":
    main()
