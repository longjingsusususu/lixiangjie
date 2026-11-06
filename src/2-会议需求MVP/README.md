# 2-会议需求MVP

本目录代码即仓库根目录的 `mvp/`（唯一来源，避免两份代码分叉），部署副本见 `meeting-mvp-deploy/`。

- 工程架构 / Agent 框架 / 知识库 / 四道人工确认点：见 `mvp/f24015105_MVP工程架构与人工确认点说明.md`
- 启动：`cd mvp && python f24015105_meeting_seed.py --force && python f24015105_meeting_server.py`（8800 端口）
- 测试：`cd mvp && python f24015105_meeting_tests.py`（五场景 81 项断言）
- 线上演示：https://meeting-demand-mvp.app.workbuddy.host/?sharecode=IS5Y7TXFZW
