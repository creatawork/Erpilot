"""erp_store：mini-ERP 领域模型与种子数据（M2 末起步）。

- 三个模块：商品、库存、订单（含报价计算）
- 开发期 SQLite（SQLAlchemy 无缝切换），M6 起生产路径 PostgreSQL + pgvector
- 种子数据要有真实感：数百条商品、跨越数月的订单流水、少量异常数据
  （异常数据是边界 case 评测的来源）
"""
