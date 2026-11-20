# -*- coding: utf-8 -*-
"""一次性小额实盘测试单：先构建签名订单检查 maker/signer，再由 --post 真正提交。

用法:
    python tools/place_test_order.py            # 只构建，不提交
    python tools/place_test_order.py --post     # 构建并提交限价单
"""

import argparse
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

# 默认目标：2026-09-10 AS Roma 场次，Yes 一侧
DEFAULT_TOKEN_ID = "66212165995022029603175153927881038102702901052395698916953787694127926213352"
DEFAULT_PRICE = 0.40
DEFAULT_SIZE = 5.0


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
    parser.add_argument("--price", type=float, default=DEFAULT_PRICE)
    parser.add_argument("--size", type=float, default=DEFAULT_SIZE)
    parser.add_argument("--funder", default=None, help="覆盖 .env 中的资金钱包地址")
    parser.add_argument("--signature-type", type=int, default=None, help="覆盖 .env 中的签名类型")
    parser.add_argument("--post", action="store_true", help="真正提交订单")
    args = parser.parse_args()

    values = load_env(ENV_PATH)
    proxy = values.get("PM_PROXY", "http://127.0.0.1:7897")
    os.environ.setdefault("HTTP_PROXY", proxy)
    os.environ.setdefault("HTTPS_PROXY", proxy)

    from py_clob_client_v2 import Side
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds, OrderArgs, OrderType

    signature_type = (
        args.signature_type
        if args.signature_type is not None
        else int(values.get("PM_SIGNATURE_TYPE", "1") or 1)
    )
    funder = args.funder or values["PM_FUNDER_ADDRESS"]
    creds = ApiCreds(
        api_key=values["PM_API_KEY"],
        api_secret=values["PM_API_SECRET"],
        api_passphrase=values["PM_API_PASSPHRASE"],
    )

    client = ClobClient(
        host="https://clob.polymarket.com",
        key=values["PM_PRIVATE_KEY"],
        chain_id=137,
        signature_type=signature_type,
        funder=funder,
    )
    client.set_api_creds(creds)

    print(f"signature_type = {signature_type}")
    print(f"funder         = {funder}")
    print(f"token_id       = {args.token_id}")
    print(f"BUY {args.size} shares @ {args.price} = {args.size * args.price:.2f} USDC")

    order_args = OrderArgs(
        token_id=args.token_id,
        price=args.price,
        size=args.size,
        side=Side.BUY,
    )
    signed = client.create_order(order_args, None)
    print()
    print("== 签名订单 ==")
    for field in ("maker", "signer", "signatureType", "makerAmount", "takerAmount", "side"):
        print(f"  {field} = {getattr(signed, field, '(n/a)')}")

    if not args.post:
        print()
        print("dry-run：未提交。确认无误后加 --post 提交。")
        return

    print()
    response = client.post_order(signed, OrderType.GTC)
    print("== 提交结果 ==")
    print(response)


if __name__ == "__main__":
    main()
