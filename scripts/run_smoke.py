# -*- coding: utf-8 -*-
"""
run_smoke.py —— 依次执行 tests/ 下全部冒烟脚本并汇总结果

用法（在仓库根目录）：
    python scripts/run_smoke.py

约定：每个冒烟脚本独立进程运行、各自建库（互不污染），
末尾输出形如「结果：通过 N 项，失败 0 项」或「结果：X 通过 / 0 失败」。
"""

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

CASES = [
    "smoke_test.py",          # 员工端查询入口（20 项）
    "admin_smoke.py",         # 管理后台 登录/权限（22 项）
    "admin_batch2_smoke.py",  # 需求项库/导入/编号映射（30 项）
    "admin_batch34_smoke.py", # 规则配置/审计（28 项）
    "agent_smoke.py",         # 工具层顺序约束 + 红队（38 项）
    "agent_trace_smoke.py",   # Agent 编排轨迹（58 项）
]

SUMMARY = re.compile(r"(\d+)\s*(?:项|通过)?\s*[/，,]\s*(\d+)\s*(?:项)?\s*(?:失败|失败)")


def main() -> int:
    total_pass = total_fail = 0
    rows = []
    for name in CASES:
        path = TESTS / name
        if not path.exists():
            rows.append((name, "跳过", "文件不存在"))
            continue
        print(f"\n▶ {name}")
        proc = subprocess.run(
            [sys.executable, str(path)],
            cwd=str(TESTS),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        tail = proc.stdout.strip().splitlines()[-6:]
        print("\n".join("  " + line for line in tail))

        passed = failed = 0
        for line in reversed(proc.stdout.splitlines()):
            if "结果" in line:
                m = SUMMARY.search(line)
                if m:
                    passed, failed = int(m.group(1)), int(m.group(2))
                break
        if not failed and proc.returncode != 0:
            failed = 1  # 脚本崩溃但没打印汇总：按失败计
        total_pass += passed
        total_fail += failed
        rows.append((name, str(passed), "0" if not failed else str(failed)))

    print("\n" + "=" * 58)
    print(f"{'脚本':<26}{'通过':>8}{'失败':>8}")
    print("-" * 58)
    for name, p, f in rows:
        print(f"{name:<26}{p:>8}{f:>8}")
    print("-" * 58)
    print(f"{'合计':<26}{total_pass:>8}{total_fail:>8}")
    print("=" * 58)
    return 1 if total_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
