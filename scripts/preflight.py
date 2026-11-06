#!/usr/bin/env python3
"""推送前机械闸门：查清这个人能不能推、上次推过没、同机构是否撞车。

不靠记忆判断，靠数据判断。生成任何话术之前先跑这个脚本。

用法：
  python3 preflight.py --name 张三
  python3 preflight.py --name 李四 --project <项目代号>
  python3 preflight.py --org 某资本          # 看该机构全部人及推送状态
  python3 preflight.py --name 张三 李四 王五  # 多人批量
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 路径只从 paths.py 取，不许在本文件里写字面量
from paths import ARCHIVE_DB as INVESTOR_DB, MSG_DB  # noqa: E402

STATUS_ICON = {"sent": "已推", "draft": "草稿", "replied": "已回", "rejected": "已拒",
               "closed": "已关闭", "": "无记录"}


def conn(path):
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def find_investors(db, names):
    out = []
    for kw in names:
        rows = db.execute(
            "select id,name,organization,title,sectors,stage,ticket,notes,wxid,excluded,ban_reason "
            "from investors where name like ? or organization like ?",
            (f"%{kw}%", f"%{kw}%")).fetchall()
        out.append((kw, rows))
    return out


def get_opps(db, investor_id, project=None):
    sql = ("select status,last_outbound_at,feedback,next_step,project_id "
           "from opportunities where investor_id=?")
    p = [investor_id]
    if project:
        sql += " and project_id=?"
        p.append(project)
    return db.execute(sql + " order by last_outbound_at desc", p).fetchall()


def get_thread(msg_db, wxid, days=180):
    if not wxid:
        return []
    return msg_db.execute(
        """select datetime(create_time,'unixepoch','localtime') t,
                  case when json_extract(data,'$.from_me')=1 then '我' else '对方' end who,
                  substr(replace(json_extract(data,'$.text'),char(10),' '),1,90) txt
           from reader_messages
           where wxid=? and create_time >= strftime('%s','now') - ?
           order by create_time desc limit 8""",
        (wxid, days * 86400)).fetchall()


def judge(rows_opps, thread):
    """返回 (级别, 原因列表)。"""
    reasons, level = [], "OK"
    if rows_opps:
        last = rows_opps[0]
        reasons.append(f"库内有推送记录：状态={STATUS_ICON.get(last[0], last[0])}，"
                       f"最后外发={last[1] or '空'}")
        level = "WARN"
    if thread and thread[0][1] == "我":
        reasons.append(f"**最后一条是本人发出、对方未回**（{thread[0][0]}）：{thread[0][2]}")
        level = "BLOCK"
    elif thread:
        reasons.append(f"最近对方发言：{thread[0][0]}「{thread[0][2]}」")
    return level, reasons


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", nargs="+", default=None)
    p.add_argument("--org", default=None)
    p.add_argument("--project", default=None)
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--dbs", default="")
    a = p.parse_args()

    inv_path, msg_path = INVESTOR_DB, MSG_DB
    if a.dbs:
        parts = a.dbs.split(",")
        if len(parts) == 2:
            inv_path, msg_path = parts

    db = conn(inv_path)
    mdb = conn(msg_path)
    if not db:
        sys.exit(f"找不到画像库：{inv_path}")

    targets = a.name or ([a.org] if a.org else [])
    if not targets:
        sys.exit("请用 --name 或 --org 指定查询对象")

    print(f"检查时间：{datetime.now():%Y-%m-%d %H:%M}  项目：{a.project or '未指定'}\n")
    org_seen = {}
    verdicts = []

    for kw, rows in find_investors(db, targets):
        if not rows:
            print(f"⛔ [{kw}] 库里查不到\n")
            verdicts.append((kw, "NOT_FOUND", ["不在库内，需先建卡"]))
            continue
        for r in rows:
            (iid, name, org, title, sectors, stage, ticket, notes, wxid,
             excluded, ban) = r
            org_seen.setdefault(org, []).append(name) if org else None
            print(f"── {name} | {org} | {title or '—'} | id={iid}")
            if excluded:
                print(f"   ⛔ 已排除（{ban or '无原因'}）\n")
                verdicts.append((name, "EXCLUDED", [f"已排除：{ban}"]))
                continue
            if notes:
                print(f"   备注：{notes.replace(chr(10),' ')[:200]}")
            if sectors and sectors not in ("[]", ""):
                print(f"   行业：{sectors}  阶段：{stage or '—'}  单笔：{ticket or '—'}")

            opps = get_opps(db, iid, a.project)
            thread = get_thread(mdb, wxid, a.days) if mdb else []
            level, reasons = judge(opps, thread)

            if thread:
                print("   最近往来：")
                for t, who, txt in thread:
                    print(f"     {t} [{who}] {txt}")
            for x in reasons:
                print(f"   → {x}")

            icon = {"OK": "✅ 可推", "WARN": "⚠️ 需注意", "BLOCK": "⛔ 不可直接推"}[level]
            print(f"   结论：{icon}\n")
            verdicts.append((name, level, reasons))

    # 同机构撞车检查
    dup = {o: ns for o, ns in org_seen.items() if len(ns) > 1}
    if dup:
        print("=== 同机构多人，必须错峰 >=3 天 ===")
        for o, ns in dup.items():
            print(f"  {o}: {' / '.join(ns)}")
        print()

    print("=== 汇总 ===")
    for name, level, _ in verdicts:
        print(f"  {name}: {level}")
    blocked = [n for n, l, _ in verdicts if l in ("BLOCK", "EXCLUDED")]
    if blocked:
        print(f"\n需要本人决策才能动的：{'、'.join(blocked)}")


if __name__ == "__main__":
    main()
