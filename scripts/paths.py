#!/usr/bin/env python3
"""投资人档案路径的唯一出口（single source of truth）。

硬规则：本 skill 下任何脚本都不许自己拼库路径，一律 `from paths import ARCHIVE_DB, MSG_DB`。

正本
    ~/Library/Application Support/MarxAssistant/marx.sqlite
解析顺序与 App 端 `src/server/data-paths.ts` 保持一致：
    1. 环境变量 MARX_DATA_DIR / MARX_DATA / MUNGER_DATA_DIR / MUNGER_DATA
    2. ~/Library/Application Support/MarxAssistant/
    3. 目录内有 marx.sqlite 就用它；没有才退回 munger.sqlite（历史兼容）

已知的副本（都不是正本，只允许读来分析差异，不许写）
    见 LEGACY_COPIES。`archive.py --check` 会把它们和正本逐项对比。
"""
import os
import sqlite3
import sys

APP_DIR = os.path.expanduser("~/Library/Application Support/MarxAssistant")

# 正本必须同时具备这些表，缺任一张就说明连错了库
REQUIRED_TABLES = ("investors", "organizations", "external_intel",
                   "opportunities", "projects", "profiles")

ENV_KEYS = ("MARX_DATA_DIR", "MARX_DATA", "MUNGER_DATA_DIR", "MUNGER_DATA")


def data_root():
    for k in ENV_KEYS:
        v = os.environ.get(k)
        if v:
            return os.path.expanduser(v)
    return APP_DIR


def _pick(root, names):
    for n in names:
        p = os.path.join(root, n)
        if os.path.exists(p):
            return p, n
    return os.path.join(root, names[0]), names[0]


ARCHIVE_DB, ARCHIVE_NAME = _pick(data_root(), ("marx.sqlite", "munger.sqlite"))
MSG_DB = os.path.join(data_root(), "reader-bridge.sqlite")
# L2 事实库（knowledge_facts / knowledge_investors 等），与 marx.sqlite 同属 L2
KNOWLEDGE_DB = os.path.join(data_root(), "knowledge.sqlite")

# L3 wiki 画像（派生层，由 L2 重生成；禁手改）
WIKI_ROOT = os.path.join(data_root(), "knowledge")
PEOPLE_PAGES_DIR = os.path.join(WIKI_ROOT, "people")
ORG_PAGES_DIR = os.path.join(WIKI_ROOT, "orgs")

# 正本应该是 marx.sqlite；退回 munger.sqlite 说明正本丢了，必须报警
IS_CANONICAL = ARCHIVE_NAME == "marx.sqlite"

LEGACY_COPIES = (
    ("~/Projects/active/marx-assistant/data/munger.sqlite",
     "项目内快照，2026-10-27 停更；713 人、2 条推送、无 organizations/external_intel"),
    ("~/Library/Application Support/MungerAssistant/munger.sqlite",
     "旧 App（芒格助手）运行时库，已被 MarxAssistant 取代"),
    ("~/WorkBuddy/FA投资人梳理/marx.sqlite",
     "0 字节空占位文件，历史遗留，无数据"),
)


def open_ro(path=None):
    """只读打开。库不存在返回 None，由调用方决定报错还是降级。"""
    p = os.path.expanduser(path or ARCHIVE_DB)
    if not os.path.exists(p):
        return None
    return sqlite3.connect(f"file:{p}?mode=ro", uri=True)


def open_rw(path=None):
    p = os.path.expanduser(path or ARCHIVE_DB)
    if not os.path.exists(p):
        sys.exit(f"找不到档案库：{p}")
    return sqlite3.connect(p)


def tables(db):
    return {r[0] for r in db.execute(
        "select name from sqlite_master where type='table'")}


def check(path=None):
    """返回正本健康度字典，供 archive.py 与各关卡共用。"""
    p = os.path.expanduser(path or ARCHIVE_DB)
    out = {"path": p, "exists": os.path.exists(p), "canonical": IS_CANONICAL,
           "tables": [], "missing": [], "counts": {}, "mtime": None, "size": 0}
    if not out["exists"]:
        return out
    st = os.stat(p)
    out["mtime"] = st.st_mtime
    out["size"] = st.st_size
    db = open_ro(p)
    try:
        out["tables"] = sorted(tables(db))
        out["missing"] = [t for t in REQUIRED_TABLES if t not in out["tables"]]
        for t in REQUIRED_TABLES:
            if t in out["tables"]:
                out["counts"][t] = db.execute(f"select count(*) from {t}").fetchone()[0]
    finally:
        db.close()
    return out


def describe():
    """人话说明当前解析结果，直接贴进文档顶部。"""
    c = check()
    lines = [f"档案正本：{c['path']}"]
    if not c["exists"]:
        lines.append("⚠️ 正本不存在——先确认 MarxAssistant 是否安装 / MARX_DATA_DIR 是否被改。")
        return "\n".join(lines)
    if not c["canonical"]:
        lines.append("⚠️ 正本 marx.sqlite 缺失，已退回 munger.sqlite（旧表结构）。")
    if c["missing"]:
        lines.append(f"⚠️ 缺表：{', '.join(c['missing'])}——连错库了。")
    lines.append("规模：" + " / ".join(f"{k} {v}" for k, v in c["counts"].items()))
    lines.append(f"微信消息库：{MSG_DB}（{'存在' if os.path.exists(MSG_DB) else '缺失'}）")
    lines.append(f"事实库：{KNOWLEDGE_DB}（{'存在' if os.path.exists(KNOWLEDGE_DB) else '缺失'}）")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
