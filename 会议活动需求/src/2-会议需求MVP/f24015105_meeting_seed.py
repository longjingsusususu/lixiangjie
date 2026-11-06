# -*- coding: utf-8 -*-
"""
f24015105_meeting_seed.py —— 灌入 50 项虚构演练样本（f12 口径，与真实数据隔离）+ 五角色账号。

用法：python f24015105_meeting_seed.py [--force]   （--force 清空重建）
"""
import hashlib
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from f24015105_meeting_server import DB_PATH, SCHEMA, init_schema  # noqa: E402

FORCE = "--force" in sys.argv
if FORCE and DB_PATH.exists():
    DB_PATH.unlink()

conn = sqlite3.connect(DB_PATH)
conn.executescript(SCHEMA)

USERS = [("lin", "林亦舟", "staff"), ("tang", "唐映秋", "coordinator"), ("guml", "顾明岚", "biz_owner"),
         ("fujing", "傅景", "qa"), ("qinrui", "秦芮", "admin")]
for uname, disp, role in USERS:
    h = hashlib.sha256(f"{uname}:123456".encode()).hexdigest()
    conn.execute("INSERT OR IGNORE INTO users(username,password_hash,display_name,role) VALUES(?,?,?,?)",
                 (uname, h, disp, role))

if not conn.execute("SELECT 1 FROM batches WHERE id=1").fetchone():
    conn.execute("INSERT INTO batches(id,name,planned_items) VALUES(1,'单批次试点',50)")

CATS = ["equipment", "language", "catering", "venue", "other"]
CONTENTS = [
    "主会场双语同传设备 2 套", "签到台 iPad 4 台", "贵宾休息室茶歇（含清真餐 6 份）",
    "同传翻译（英中）2 名", "主视觉背板 6m×3m", "全场无线麦克 8 支",
    "嘉宾接送机（浦东机场 12 人次）", "酒店住宿（大床房 20 间夜）", "合影台阶搭建",
    "议程手册印刷 300 份", "灯光音响彩排 2 小时", "现场医疗点及急救包",
]
DIRTY = ["（群消息转发，原文缺失）", "音频设备——详见截图，文字描述缺失", "*重复登记待合并*"]
n_total = 50
for i in range(1, n_total + 1):
    cat = CATS[i % 5]
    content = CONTENTS[(i - 1) % len(CONTENTS)] + f"（#{i}）"
    dirty = i in (46, 47, 48, 49, 50)          # 5 条脏数据/缺项样本
    if dirty:
        content = DIRTY[(i - 46) % len(DIRTY)]
    owner = None if dirty else ("林亦舟" if i % 2 else "唐映秋")
    status = "missing" if dirty else ("confirmed" if i <= 10 else "unconfirmed")
    src = ["servicedesk", "ledger", "filelib", "manual"][i % 4]
    ext = f"{src[:2].upper()}-{1000 + i}"
    biz = f"2026-11-{(i % 5) + 1:02d} {8 + i % 9:02d}:{i % 60:02d}:00"
    recv = f"2026-11-{((i % 5) + 1) if i < 46 else 6:02d} 1{i % 8}:30:00"   # 接收晚于发生（双时间戳可区分）
    uid = conn.execute("SELECT id FROM users WHERE username=?", ("lin" if owner == "林亦舟" else "tang",)).fetchone()
    conn.execute(
        "INSERT INTO requirement_items(id,batch_id,category,content,owner_id,status,source_tool,source_row_ref,"
        "record_channel,biz_occurred_at,received_at,confirmed_by,confirmed_at,created_by) "
        "VALUES(?,1,?,?,?,?,?,?, 'im_first', ?, ?, ?, ?, ?)",
        (i, cat, content, uid[0] if uid else None, status, src, ext, biz, recv,
         3 if status == "confirmed" else None,
         "2026-11-06 09:00:00" if status == "confirmed" else None, 1))
    conn.execute("INSERT INTO item_changes(item_id,field_name,old_value,new_value,changed_by,change_source) "
                 "VALUES(?, 'status', NULL, ?, 1, 'import')", (i, status))
    conn.execute("INSERT OR IGNORE INTO id_mappings(source_tool,external_no,item_id,mapped_by) VALUES(?,?,?,1)",
                 (src, ext, i))

conn.commit()
n = conn.execute("SELECT COUNT(*) FROM requirement_items").fetchone()[0]
dist = dict(conn.execute("SELECT status,COUNT(*) FROM requirement_items GROUP BY status").fetchall())
print(f"种子完成：用户 {len(USERS)}，需求项 {n}，状态分布 {dist}，DB={DB_PATH}")
