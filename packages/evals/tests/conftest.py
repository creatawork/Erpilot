"""evals 测试共享件：小规模确定性种子库 + 占位符解析 + MCP 工具面。

LLM 在 HTTP 边界 mock（agent_core.testing）的用例不烧 token；真实链路评测
在 test_evals_live.py（marker `eval`，默认排除）。
"""

from pathlib import Path

import pytest
from erp_store.db import make_engine
from erp_store.repository import ErpRepository
from erp_store.seed import seed_database
from mcp_erp import build_agent_tools


@pytest.fixture(scope="package")
def seeded_db(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("evals") / "erp.db"
    seed_database(path, n_products=60, n_orders=80)
    return path


@pytest.fixture(scope="package")
def repo(seeded_db) -> ErpRepository:
    return ErpRepository(make_engine(seeded_db))


@pytest.fixture(scope="package")
def resolved(repo) -> dict[str, str]:
    from evals.context import resolve

    return resolve(repo)


@pytest.fixture(scope="package")
def tools(seeded_db) -> list:
    return build_agent_tools(seeded_db)
