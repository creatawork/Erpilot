"""mcp_erp 入口：独立进程跑 FastMCP server（stdio），供外部 MCP 客户端连接。

    uv run --package mcp-erp python -m mcp_erp serve [--db data/erpilot.db]
"""

import argparse
from pathlib import Path

from erp_store.db import DEFAULT_DB

from mcp_erp.server import create_server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mcp_erp", description="Erpilot MCP 工具服务")
    sub = parser.add_subparsers(dest="command", required=True)
    p_serve = sub.add_parser("serve", help="以 stdio 传输启动 MCP server")
    p_serve.add_argument("--db", type=Path, default=DEFAULT_DB, help="ERP 数据库路径")
    args = parser.parse_args(argv)

    if args.command == "serve":  # pragma: no cover —— 唯一子命令
        create_server(args.db).run()  # fastmcp 默认 stdio 传输，阻塞到连接结束


if __name__ == "__main__":
    main()
