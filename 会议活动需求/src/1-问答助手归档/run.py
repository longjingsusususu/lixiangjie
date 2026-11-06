# -*- coding: utf-8 -*-
"""
run.py —— 一键启动入口

用法：
    python run.py                 # 默认 http://127.0.0.1:8010
    MEETING_PORT=9000 python run.py

做的事：
    1. 把 app/ 加入模块搜索路径（应用代码保持顶层模块风格，不强制包化）
    2. 读取仓库根目录的 .env（不引入 python-dotenv 依赖）
    3. 数据库默认落在 data/meeting.db（首次启动自动建表 + 导入样例需求项）
    4. 启动 uvicorn
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
APP_DIR = ROOT / "app"
sys.path.insert(0, str(APP_DIR))

# ── 读取 .env（KEY=VALUE，忽略注释与空行；已存在的环境变量优先）──
_env_file = ROOT / ".env"
if _env_file.exists():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        if _v.strip():
            os.environ.setdefault(_k.strip(), _v.strip())

(ROOT / "data").mkdir(exist_ok=True)
os.environ.setdefault("MEETING_DB", str(ROOT / "data" / "meeting.db"))

import uvicorn  # noqa: E402

from portal import app  # noqa: E402

if __name__ == "__main__":
    _host = os.environ.get("MEETING_HOST", "127.0.0.1")
    _port = int(os.environ.get("MEETING_PORT", "8010"))
    print("=" * 56)
    print(" 会议活动服务需求汇总 · 需求查询与交接平台")
    print(f" 员工端   http://{_host}:{_port}/")
    print(f" 管理后台 http://{_host}:{_port}/admin")
    print(f" 数据库   {os.environ['MEETING_DB']}")
    print("=" * 56)
    uvicorn.run(app, host=_host, port=_port)
