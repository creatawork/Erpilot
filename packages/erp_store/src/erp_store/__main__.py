"""`python -m erp_store seed`：生成/重建演示数据库（默认 data/erpilot.db）。

    uv run --package erp-store python -m erp_store seed
    uv run --package erp-store python -m erp_store seed --products 50 --orders 80
"""

import argparse
import sys
from pathlib import Path

from erp_store.db import DEFAULT_DB
from erp_store.seed import DEFAULT_ORDERS, DEFAULT_PRODUCTS, DEFAULT_SEED, seed_database


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="erp_store", description="mini-ERP 数据工具")
    sub = parser.add_subparsers(dest="command", required=True)
    p_seed = sub.add_parser("seed", help="全量重建种子数据库（确定性：同 seed 同数据）")
    p_seed.add_argument("--db", type=Path, default=DEFAULT_DB, help="数据库文件路径")
    p_seed.add_argument("--products", type=int, default=DEFAULT_PRODUCTS)
    p_seed.add_argument("--orders", type=int, default=DEFAULT_ORDERS)
    p_seed.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args(argv)

    if args.command == "seed":  # pragma: no cover —— 只有这一个子命令
        stats = seed_database(
            args.db, seed=args.seed, n_products=args.products, n_orders=args.orders
        )
        print(
            f"已生成 {args.db}\n"
            f"  商品 {stats.products}（下架 {stats.off_sale}）· "
            f"库存 {stats.stocks}（零库存 {stats.zero_stock}）\n"
            f"  订单 {stats.orders}（取消 {stats.cancelled_orders} / 退款 "
            f"{stats.refunded_orders} / 含下架商品 {stats.orders_with_off_sale_item}）· "
            f"明细 {stats.order_items}（快照价≠现价 {stats.price_drift_items}）"
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(130)
