# src · 代码包说明

> 版本 v1.1 · 2026-11-06 ｜ src = 问答助手归档 + 会议需求 MVP（工程架构 / Agent 框架 / 知识库 / 人工确认点）

## 组成

| 目录 | 内容 |
|---|---|
| `1-问答助手归档/` | 问答助手完整代码（员工问答 SSE + 管理后台 + Agent Harness + 10 工具 + 196 项冒烟）。二期 LLM 参考实现，一期不运行 |
| `2-会议需求MVP/` | 会议活动服务需求汇总系统 MVP，可独立运行（详见目录内《f24015105_MVP工程架构与人工确认点说明.md》） |

## 2-会议需求MVP 四要素对照

1. **工程架构**：四层单体（入口/接口/编排/服务/数据），SQLite WAL，11 张表，双时间戳+五态分列第一天进 schema；
2. **Agent 框架**：`f24015105_agent_harness.py`——decide→invoke→observe 三段编排，规则驱动零 LLM（f01），工具槽位化（TOOLBOX），二期仅替换 decide 为 LLM 规划器；
3. **知识库**：`f24015105_knowledge.py` + `f24015105_kb_data.json`——10 篇规则文章（锚定 f 编号），bigram 加权检索 + 覆盖门（0.25，校准：有效问题 ≥0.29/无关 0.0），未覆盖一律转人工；二期接 RAG 接口不变；
4. **人工确认/审核点（4 处 ≥ 要求 3 处）**：
   - ① 需求确认签字 `POST /api/items/{id}/confirm`（编辑权≠确认权，f22）
   - ② 手动定版 `POST /api/versions/{vid}/finalize`（有效期+确认状态双条件，f24/f34）
   - ③ 交接三要素卡口+接收确认 `POST /api/handovers`→`/ack`（缺一拦截，f20）
   - ④ 异常队列人工复核 `POST /api/exceptions/{eid}/claim`→`/close`（不得默认通过，f44/f33）
   - 附加：`/api/ask` 只读，Agent 无审批权；知识库未覆盖自动转人工。

## 运行

```bash
cd 2-会议需求MVP
pip install fastapi uvicorn python-multipart
python f24015105_meeting_seed.py --force
python f24015105_meeting_server.py     # http://127.0.0.1:8800
```

账号：lin/tang/guml/fujing/qinrui（密码 123456，演示数据 f12 虚构样本）。问答入口：`GET /api/ask?q=交接需要哪些要素`。

## 测试记录（真实执行，2026-11-06）

- 五场景 81 项断言 **81/81 全绿**（`f24015105_meeting_tests.py` 可重复运行，日志在 test_logs/）；
- Agent/知识库实测：规则问题命中带引用 6/6（KB-02/03/04/08…），无关问题转人工 3/3，回归 81 项无破坏。
