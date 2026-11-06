#!/usr/bin/env python3
"""数据新鲜度关卡：确认本次话术基于的是最新数据，而不是过期快照。

任何话术生成前必须先跑。三项任一不达标，不得直接出稿。

用法：
  python3 freshness.py
  python3 freshness.py --name 张三 李四     # 顺带查这些人在微信里的最近往来
  python3 freshness.py --max-msg-hours 48
"""
import argparse
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 路径只从 paths.py 取，不许在本文件里写字面量（连错库就是这么来的）
from paths import ARCHIVE_DB as INV_DB, MSG_DB  # noqa: E402
import paths  # noqa: E402


def ro(path):
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


AGE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
               "%Y-%m-%dT%H:%M", "%Y-%m-%d")


def age(ts_str, now):
    """返回 (小时数, 人话)。时间格式不一，逐个试，别静默吞成 None。"""
    if not ts_str:
        return None, "无记录"
    t = None
    for fmt in AGE_FORMATS:
        try:
            t = datetime.strptime(ts_str.strip(), fmt)
            break
        except ValueError:
            continue
    if t is None:
        return None, f"无法解析：{ts_str}"
    hours = (now - t).total_seconds() / 3600
    if hours < 24:
        return hours, f"{hours:.0f} 小时前"
    return hours, f"{hours/24:.1f} 天前"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--name", nargs="*", default=None)
    p.add_argument("--max-msg-hours", type=int, default=48)
    p.add_argument("--msg-db", default=MSG_DB)
    p.add_argument("--inv-db", default=INV_DB)
    a = p.parse_args()

    now = datetime.now()
    findings = []  # (项, 状态, 说明)

    print(f"数据新鲜度校验 · {now:%Y-%m-%d %H:%M}\n")

    # 0. 先验库的身份：连的是不是正本、表齐不齐（连错库的坑踩过两次）
    c = paths.check()
    if not c["exists"]:
        findings.append(("档案正本", "FAIL", f"不存在：{c['path']}"))
    elif c["missing"]:
        findings.append(("档案正本", "FAIL", f"缺表 {', '.join(c['missing'])}，连错库了：{c['path']}"))
    elif not c["canonical"]:
        findings.append(("档案正本", "FAIL", "marx.sqlite 缺失，已退回 munger.sqlite 旧结构，先修库"))
    else:
        findings.append(("档案正本", "PASS", c["path"]))

    # 1. 微信消息库
    mdb = ro(a.msg_db)
    if not mdb:
        findings.append(("微信消息库", "FAIL", f"找不到 {a.msg_db}"))
    else:
        row = mdb.execute(
            "select max(create_time), count(*) from reader_messages").fetchone()
        last_ts = datetime.fromtimestamp(row[0]).strftime("%Y-%m-%d %H:%M:%S") if row[0] else None
        hours, human = age(last_ts, now)
        total = row[1]
        if hours is None:
            findings.append(("微信消息库", "WARN", "无任何消息"))
        elif hours > 24 * 7:
            findings.append(("微信消息库", "FAIL",
                             f"最后一条 {last_ts}（{human}），已超过 7 天，务必先同步微信"))
        elif hours > a.max_msg_hours:
            findings.append(("微信消息库", "WARN",
                             f"最后一条 {last_ts}（{human}），建议先同步微信再出稿"))
        else:
            findings.append(("微信消息库", "PASS",
                             f"最后一条 {last_ts}（{human}），共 {total} 条 / "
                             f"{mdb.execute('select count(distinct wxid) from reader_messages').fetchone()[0]} 个会话"))

        # 近 7 天活跃度：判断这个库是不是"活的"
        recent = mdb.execute(
            "select count(*) from reader_messages where create_time >= strftime('%s','now')-7*86400"
        ).fetchone()[0]
        print(f"  近 7 天新增消息：{recent} 条")
        if recent == 0:
            findings.append(("微信活跃度", "WARN", "近 7 天零消息，可能未同步"))

    # 2. 投资人画像库
    idb = ro(a.inv_db)
    if not idb:
        findings.append(("投资人画像库", "FAIL", f"找不到 {a.inv_db}"))
    else:
        up = idb.execute("select max(updated_at) from investors").fetchone()[0]
        hours, human = age(up, now)
        n = idb.execute("select count(*) from investors where excluded=0").fetchone()[0]
        if hours is None:
            # updated_at 全库为空（App 不写这列）。退化为看库文件写入时间，不能当"无数据"报错。
            mt = datetime.fromtimestamp(os.path.getmtime(os.path.expanduser(a.inv_db)))
            hours = (now - mt).total_seconds() / 3600
            findings.append(("投资人画像库",
                             "PASS" if hours <= 24 * 30 else "WARN",
                             f"库文件写入 {mt:%Y-%m-%d %H:%M}（{hours/24:.1f} 天前），有效 {n} 人"
                             + ("" if hours <= 24 * 30 else "，超过 30 天没动过")))
        elif hours > 24 * 30:
            findings.append(("投资人画像库", "WARN",
                             f"最后更新 {up}（{human}），超过 30 天，备注可能已过期"))
        else:
            findings.append(("投资人画像库", "PASS", f"最后更新 {up}（{human}），有效 {n} 人"))

        # 3. 推送记录回填 —— 最容易失真的环节
        o = idb.execute(
            "select count(*), max(last_outbound_at) from opportunities").fetchone()
        cnt, last_out = o
        hours, human = age(last_out, now)
        if cnt == 0:
            findings.append(("推送记录", "FAIL", "opportunities 表为空，完全无法去重"))
        elif hours is not None and hours > 24 * 7:
            findings.append(("推送记录", "WARN",
                             f"仅 {cnt} 条，最后回填 {last_out}（{human}）。"
                             f"**推送记录已失真，不能作为去重依据，必须逐人核微信**"))
        else:
            findings.append(("推送记录", "PASS", f"{cnt} 条，最后回填 {last_out}"))

    # 4. 指定对象的最近往来
    if a.name and mdb:
        print("\n逐人最近往来：")
        for kw in a.name:
            r = idb.execute(
                "select name, wxid from investors where name like ? limit 1", (f"%{kw}%",)
            ).fetchone() if idb else None
            if not r:
                print(f"  {kw}：库内查无此人")
                continue
            nm, wxid = r
            if not wxid:
                print(f"  {nm}：无 wxid，从未加微信或未映射")
                continue
            m = mdb.execute(
                """select datetime(create_time,'unixepoch','localtime'),
                          case when json_extract(data,'$.from_me')=1 then '我' else '对方' end
                   from reader_messages where wxid=?
                   order by create_time desc limit 1""", (wxid,)).fetchone()
            if m:
                h, human = age(m[0], now)
                print(f"  {nm}：最后互动 {m[0]}（{human}，最后发言方={m[1]}）")
            else:
                print(f"  {nm}：有 wxid 但无消息记录")

    # 结论
    print("\n=== 校验结果 ===")
    for item, status, note in findings:
        icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "⛔"}[status]
        print(f"{icon} {item}：{note}")

    fails = [f for f in findings if f[1] == "FAIL"]
    warns = [f for f in findings if f[1] == "WARN"]
    print()
    if fails:
        print("结论：⛔ 不得出稿。先解决上列 FAIL 项。")
        sys.exit(1)
    if warns:
        print("结论：⚠️ 可以出稿，但必须在文档顶部写明数据截至时间与失真项。")
    else:
        print("结论：✅ 数据新鲜，可以出稿。")


if __name__ == "__main__":
    main()
