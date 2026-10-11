# B4 多轮草稿任务与历史会话

用户已授权沿业务里程碑继续开发。本阶段补齐结构化草稿发现、确定性行修改和历史会话入口，不部署。

## 行为与取舍

采用现有不可变草稿作为任务事实，不复制客户、商品、数量、备注到新的模型状态表。相比从对话提取槽位，这能避免裁剪和重启丢失；相比另建通用任务引擎，保持实现边界可验证。

当前任务定义为会话内未提交草稿：没有为 none；唯一为 active（过期仍保留，必须刷新后提交）；多份为 ambiguous，必须明确草稿后才能修改。不能按更新时间猜测。查询最多展示 20 个候选，has_more 标明截断，不因此误判唯一。

`get_order_task()` 无模型可见 session_id，返回 `{status,draft,candidates,has_more}`；draft 仅 active 时为现有完整草稿，candidates 为 `{draft_id,revision,customer,status}`。模型每轮涉及草稿时先查询；数量指代按该版本 items 的 1 起算行号。明确草稿后 `revise_order_draft_quantity(draft_id,expected_revision,item_number,quantity)` 只改一行，保留客户、备注与其他商品；版本改变、越界、非整数均拒绝。只保存报价，不建单；提交仍走原审批。

GET `/api/sessions/{id}/order-task` 返回同一结构，未启用草稿为 503。会话状态增加可选 order_task。读工具与接口在重启后由持久化草稿重建，不依赖聊天消息。

GET `/api/sessions?limit=20&offset=0` 返回 `{items:[{session_id,title,updated_at,status}],has_more}`；limit 1~50，offset >=0。title 为首轮用户输入前 80 字；updated_at 为带 Z 的 UTC 时间，浏览器按当地时区展示；按更新时间及稳定 ID 倒序。来源为已有 RunStore；空会话没有运行记录不入列表。状态是运行记录投影，进入会话后原 checkpoint 状态为事实。无历史存储为 503（当前应用默认始终创建 RunStore）。

页面新增历史会话按钮与列表、分页和刷新；进入历史不自动执行任何操作。当前活动流期间禁止切换，待审批和中断可以离开并重新找回，不取消原审批。切换前清空旧展示、草稿、审批摘要和事件游标；读取失败保持输入锁定。异步响应必须绑定目标会话及请求代次，旧请求失败也不能污染新会话。

## 验收与边界

真实 SQLite/MCP/HTTP 验证多轮修改、客户/备注保持、行序、版本冲突、过期、隔离、重建、多个草稿澄清及历史分页。浏览器验证切换、恢复、待审批再找回、加载失败锁定和窄屏。脚本客户端代替付费模型，不宣称实际自然语言模型准确率。agent_core 保持零 ERP 依赖；无新第三方依赖；Python >=3.12；保留已有非 B4 改动。
