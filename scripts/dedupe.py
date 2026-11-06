#!/usr/bin/env python3
"""项目级去重闸门：实扫微信消息库，判定「谁已经收到过这个项目的材料」。

为什么不查 opportunities 表：
  某项目实测——微信实推 46 人，系统只录 6 条。该表从 2026-08 起就没回填过，
  任何基于它的去重都会漏人、导致重复推送。唯一可信源是微信消息原文。

用法：
  # 必填：本项目强特征词（出现即认定推过，选不会撞其他项目的词）
  python3 dedupe.py --strong 数采手套 镓铟 液态金属 --weak 手套 种子轮 --days 120

  # 只查某几个人有没有推过
  python3 dedupe.py --strong 数采手套 --only 张三 李四

  # 反查：某人最近被推过哪些项目（拿不准时用）
  python3 dedupe.py --name 张三 --days 90

输出三档：
  ⛔ 已推过        → 不许重发开场，只能走跟进型
  ⚠️ 疑似          → 命中弱特征，人工核一眼再定
  ✅ 干净          → 可发开场

退出码：0 正常；2 参数错误。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 路径只从 paths.py 取，不许在本文件里写字面量
from paths import ARCHIVE_DB as INVESTOR_DB, MSG_DB  # noqa: E402


def conn(path):
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def load_messages(mdb, days):
    """拉出窗口期内所有文本消息（含发送方向）。"""
    since = int(datetime.now().timestamp()) - days * 86400
    # 不限 kind：link 的 XML 里含 title（如"某项目-XX 线上会议"），
    # 文件消息含文件名，这些往往比正文更能证明"已经推过"。
    rows = mdb.execute(
        """select wxid,
                  datetime(create_time,'unixepoch','localtime') t,
                  json_extract(data,'$.from_me') me,
                  json_extract(data,'$.text') txt
           from reader_messages
           where create_time >= ?
             and json_extract(data,'$.text') is not null
           order by create_time""", (since,)).fetchall()
    return [r for r in rows if r[2] is not None and r[3]]


def investor_map(idb):
    """三张索引：wxid→人、姓名→人、机构→[人]"""
    by_wxid, by_name, by_org = {}, {}, defaultdict(list)
    for r in idb.execute("select id,name,organization,title,wxid from investors "
                         "where excluded=0 or excluded is null"):
        iid, name, org, title, wxid = r
        rec = {"id": iid, "name": name, "org": org or "", "title": title or ""}
        if wxid:
            by_wxid[wxid] = rec
        by_name[name] = rec
        if org:
            by_org[org].append(rec)
    return by_wxid, by_name, by_org


def brief(txt, n=70):
    """把 link/file 的 XML 压成标题，避免刷屏。"""
    t = txt.strip()
    if t.startswith("<?xml") or t.startswith("<msg"):
        m = re.search(r"<title>(.*?)</title>", t, re.S)
        t = "[卡片] " + (m.group(1).strip() if m else "（无标题）")
    return re.sub(r"\s+", " ", t)[:n]


def scan(msgs, strong, weak, only_wxids=None):
    """按会话聚合，判定命中等级。"""
    per = defaultdict(lambda: {"strong": [], "weak": [], "mine": [], "theirs": [],
                               "last_t": "", "last_who": ""})
    for wxid, t, me, txt in msgs:
        if only_wxids and wxid not in only_wxids:
            continue
        s = [k for k in strong if k in txt]
        w = [k for k in weak if k in txt]
        if not (s or w):
            if per[wxid]["last_t"] < t:
                per[wxid]["last_t"] = t
                per[wxid]["last_who"] = "我" if me == 1 else "对方"
            continue
        d = per[wxid]
        hook = {"t": t, "who": "我" if me == 1 else "对方", "txt": brief(txt), "kw": s or w}
        (d["strong"] if s else d["weak"]).append(hook)
        (d["mine"] if me == 1 else d["theirs"]).append(hook)
        if d["last_t"] < t:
            d["last_t"], d["last_who"] = t, "我" if me == 1 else "对方"
    return per


def days_ago(t):
    try:
        return (datetime.now() - datetime.strptime(t, "%Y-%m-%d %H:%M:%S")).days
    except Exception:
        return 999


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--strong", nargs="*", default=[], help="强特征词：出现即认定推过")
    p.add_argument("--weak", nargs="*", default=[], help="弱特征词：需人工确认")
    p.add_argument("--days", type=int, default=120, help="回看天数，默认 120")
    p.add_argument("--only", nargs="*", default=None, help="只查这些人（姓名）")
    p.add_argument("--name", nargs="*", default=None, help="反查：这人最近被推过什么")
    p.add_argument("--dbs", default="")
    p.add_argument("--project", default="", help="项目名：并查 opportunities 表，两个源取并集")
    a = p.parse_args()

    if not a.strong and not a.weak and not a.name:
        sys.exit("至少给一个 --strong / --weak / --name")

    inv_path, msg_path = INVESTOR_DB, MSG_DB
    if a.dbs:
        parts = a.dbs.split(",")
        if len(parts) == 2:
            inv_path, msg_path = parts

    idb, mdb = conn(inv_path), conn(msg_path)
    if not idb or not mdb:
        sys.exit(f"库打不开：{inv_path} / {msg_path}")

    by_wxid, by_name, by_org = investor_map(idb)
    msgs = load_messages(mdb, a.days)
    print(f"去重扫描：窗口 {a.days} 天 · 消息 {len(msgs)} 条 · "
          f"强特征 {a.strong or '—'} · 弱特征 {a.weak or '—'}\n")

    # 模式二：反查某人被推过什么
    if a.name:
        for nm in a.name:
            rec = by_name.get(nm) or next((r for r in by_wxid.values() if nm in r["name"]), None)
            if not rec:
                print(f"⛔ [{nm}] 库里查不到\n")
                continue
            wx = next((w for w, r in by_wxid.items() if r["id"] == rec["id"]), None)
            hits = [m for m in msgs if m[0] == wx and m[2] == 1]
            print(f"── {rec['name']} | {rec['org']} | 本人发出 {len(hits)} 条")
            for _, t, _, txt in hits[-8:]:
                print(f"   {t} {brief(txt, 100)}")
            print()
        return

    only = None
    if a.only:
        only = set()
        for nm in a.only:
            rec = by_name.get(nm)
            wx = next((w for w, r in by_wxid.items()
                       if rec and r["id"] == rec["id"]), None)
            if wx:
                only.add(wx)
            else:
                print(f"⚠️ [{nm}] 无 wxid，无法扫微信（可能未绑定）")

    per = scan(msgs, a.strong, a.weak, only)

    blocked, suspect = [], []
    for wxid, d in per.items():
        rec = by_wxid.get(wxid, {})
        name = rec.get("name") or wxid
        if a.only and name not in a.only:
            continue
        if d["strong"] and d["mine"]:
            blocked.append((name, rec, d))
        elif d["weak"] and d["mine"]:
            suspect.append((name, rec, d))

    def show(title, items, icon):
        if not items:
            return
        print(f"=== {icon} {title}（{len(items)} 人）===")
        for name, rec, d in sorted(items, key=lambda x: -len(x[2]["mine"])):
            gap = days_ago(d["last_t"])
            tail = f"最后一条方向={d['last_who']}，{gap} 天前"
            print(f"\n  {name} | {rec.get('org','?')} | {rec.get('title','')}")
            print(f"   命中 {len(d['mine'])} 条本人发出 / {len(d['theirs'])} 条对方提及 · {tail}")
            for h in (d["mine"] or d["theirs"])[:3]:
                print(f"     {h['t']} [{h['who']}] {h['txt']}")
        print()

    show("已推过本项目（微信原文为证）", blocked, "⛔")
    show("疑似（弱特征命中）", suspect, "⚠️")

    # 第二源：opportunities 表。它记的是"建过机会"的人，比微信多（含电话/会议/群发），
    # 但只记了系统里建过的，也会漏临时转发。两个源谁都不能单独作准，取并集。
    if a.project:
        rows = idb.execute(
            """select i.name, i.organization, o.status, o.last_outbound_at, o.feedback
               from opportunities o join investors i on i.id=o.investor_id
               join projects p on p.id=o.project_id
               where p.name like ? order by o.last_outbound_at desc""",
            (f"%{a.project}%",)).fetchall()
        if rows:
            print(f"=== 📋 系统记录已推（opportunities · 项目「{a.project}」）{len(rows)} 人 ===")
            for name, org, st, t, fb in rows:
                print(f"  {name} | {org or '?'} | {st} | {t or '无时间'}"
                      + (f" | {(fb or '')[:40]}" if fb else ""))
            print("\n  与上面微信命中交集才算双重确认；只在这一节出现的人，说明推过但微信没留痕（电话/会议/群发）。")
            print("  这一节里的人同样不许重发开场。\n")
        else:
            print(f"=== 📋 系统记录：opportunities 里没有项目「{a.project}」的推送记录 ===\n")

    if not blocked and not suspect and not a.project:
        print("✅ 窗口期内无人收到过本项目材料，全部可发开场\n")
    else:
        clean_note = ("⛔ 的人不许重发开场，只能走跟进型（见 references/pitch-patterns.md）；"
                      "⚠️ 的人先核一眼上下文再定。")
        print(clean_note + "\n")

    print("去重口径：微信原文（证明真发出去了）+ opportunities（证明系统建过机会）取并集。")
    print("推完请立刻回填 opportunities，否则下一轮两个源都会缺。")


if __name__ == "__main__":
    main()
