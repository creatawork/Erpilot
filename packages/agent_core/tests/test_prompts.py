from agent_core.demo_tools import WRITES_PROMPT


def test_write_prompt_requires_stock_read_before_outbound_approval() -> None:
    assert "出库或减少库存前必须先调用 get_stock 并等待查询结果" in WRITES_PROMPT
    assert "库存不足时不得调用 adjust_stock 或发起审批" in WRITES_PROMPT
