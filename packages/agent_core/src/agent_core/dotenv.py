"""最小 .env 加载：不引 python-dotenv 依赖，KEY=VALUE 逐行注入环境变量。

已存在的环境变量不覆盖（真实环境优先于文件）；支持 # 注释与引号包裹的值。
"""

import os
from pathlib import Path


def find_dotenv(start: Path) -> Path | None:
    """从 start 向上逐级查找 .env（约定放仓库根目录）。"""
    return next((p / ".env" for p in [start, *start.parents] if (p / ".env").is_file()), None)


def load_dotenv(path: Path) -> None:
    """把 .env 的 KEY=VALUE 注入环境变量；已存在的变量不覆盖。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
