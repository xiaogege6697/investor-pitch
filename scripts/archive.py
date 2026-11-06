#!/usr/bin/env python3
"""投资人档案 · 统一入口。查档案在哪、有几份、哪份是正本、某人某机构有什么。

用法：
  python3 archive.py                 # 健康检查（默认，出稿前先跑）
  python3 archive.py --find 某创投      # 查机构/投资人：档案里的全部信息
  python3 archive.py --dupes         # 只看副本差异
  python3 archive.py --json          # 机器可读

退出码：0 正常；1 正本异常（缺失/缺表/退回旧库）。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime


def _safe(name):
    """与 build_org_wiki / build_people_wiki 的文件名规则保持一致"""
    return re.sub(r"[/\\:*?\"<>|]", "_", name or "").strip() or "未命名"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402


def fmt_age(ts):
    if not ts:
        return "未知"
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            t = datetime.strptime(ts.strip()[:19], f)
            break
        except ValueError:
            continue
    else:
        return ts
    h = (datetime.now() - t).total_seconds() / 3600
    return f"{t:%Y-%m-%d %H:%M}" + (f"（{h/24:.1f} 天前）" if h >= 24 else f"（{h:.0f} 小时前）")


def scalar(db, sql, default=None):
    try:
        r = db.execute(sql).fetchone()
        return r[0] if r and r[0] is not None else default
    except sqlite3.Error:
        return default


def probe(path):
    """探一份库：存在性、大小、时间、表、人数。"""
    p = os.path.expanduser(path)
    out = {"path": p, "exists": os.path.exists(p), "size": 0, "mtime": None,
           "investors": None, "organizations": None, "intel": None, "has_new_schema": False}
    if not out["exists"]:
        return out
    out["size"] = os.stat(p).st_size
    out["mtime"] = datetime.fromtimestamp(os.stat(p).st_mtime).strftime("%Y-%m-%d %H:%M")
    db = paths.open_ro(p)
    if db is None:
        return out
    try:
        ts = paths.tables(db)
        out["has_new_schema"] = "organizations" in ts and "external_intel" in ts
        out["investors"] = scalar(db, "select count(*) from investors") if "investors" in ts else None
        out["organizations"] = scalar(db, "select count(*) from organizations") if "organizations" in ts else None
        out["intel"] = scalar(db, "select count(*) from external_intel") if "external_intel" in ts else None
    finally:
        db.close()
    return out


def health():
    c = paths.check()
    h = {"archive": c, "msg_db": paths.MSG_DB,
         "msg_exists": os.path.exists(paths.MSG_DB),
         "msg_last": None, "msg_count": None,
         "opp_last": None, "opp_count": None, "dupes": []}

    if os.path.exists(paths.MSG_DB):
        db = paths.open_ro(paths.MSG_DB)
        if db:
            try:
                h["msg_last"] = scalar(db, "select datetime(max(create_time),'unixepoch','localtime') from reader_messages")
                h["msg_count"] = scalar(db, "select count(*) from reader_messages")
            finally:
                db.close()

    if c["exists"]:
        db = paths.open_ro()
        if db:
            try:
                h["opp_last"] = scalar(db, "select max(last_outbound_at) from opportunities")
                h["opp_count"] = scalar(db, "select count(*) from opportunities")
            finally:
                db.close()

    seen = {os.path.realpath(os.path.expanduser(c["path"]))}
    for raw, note in paths.LEGACY_COPIES:
        p = probe(raw)
        p["note"] = note
        rp = os.path.realpath(p["path"])
        if rp in seen:
            continue
        seen.add(rp)
        h["dupes"].append(p)
    return h


def head_block(h):
    a = h["archive"]
    L = []
    L.append("═══ 投资人档案 · 唯一入口 ═══")
    L.append(f"正本    {a['path']}")
    if not a["exists"]:
        L.append("状态    ⛔ 不存在")
        L.append("        先确认 MarxAssistant 装没装、MARX_DATA_DIR 有没有被改。")
        return "\n".join(L)
    ok = a["canonical"] and not a["missing"]
    L.append(f"状态    {'✅ 正常' if ok else '⚠️ 异常'}"
             + ("" if a["canonical"] else "（marx.sqlite 缺失，已退回 munger.sqlite 旧结构）")
             + ("" if not a["missing"] else f"（缺表：{', '.join(a['missing'])}）"))
    L.append(f"文件    {a['size']/1048576:.1f} MB · 最后写入 "
             f"{datetime.fromtimestamp(a['mtime']):%Y-%m-%d %H:%M}")
    L.append("规模    " + " / ".join(f"{k} {v}" for k, v in a["counts"].items()))
    if h["opp_count"] is not None:
        L.append(f"推送    {h['opp_count']} 条 · 最后回填 {fmt_age(h['opp_last'])}")
    if h["msg_exists"]:
        L.append(f"微信    {h['msg_count']} 条消息 · 最后 {fmt_age(h['msg_last'])}")
    else:
        L.append("微信    ⛔ reader-bridge.sqlite 不存在，去重与风格提取都不可用")
    return "\n".join(L)


def layers():
    """三层增量视图：L1 实时微信 → L2 事实与结论 → L3 wiki 画像。

    维护方式固定：只读 L1 的增量，写 L2，再由 L2 重生成 L3。不许越级写、不许反向写。
    """
    R = paths.data_root()
    l = {"root": R, "l1": {}, "l2": {}, "l3": {}}

    # ---- L1 微信热读取（实时增量，只读） ----
    rb = os.path.join(R, "reader-bridge.sqlite")
    l["l1"]["path"] = rb
    l["l1"]["exists"] = os.path.exists(rb)
    if l["l1"]["exists"]:
        db = paths.open_ro(rb)
        if db:
            try:
                l["l1"]["msgs"] = scalar(db, "select count(*) from reader_messages")
                l["l1"]["convs"] = scalar(db, "select count(distinct wxid) from reader_messages")
                l["l1"]["last_msg"] = scalar(
                    db, "select datetime(max(create_time),'unixepoch','localtime') from reader_messages")
                l["l1"]["cursors"] = scalar(db, "select count(*) from reader_sync_cursors") or 0
                l["l1"]["last_sync"] = scalar(db, "select max(last_sync_at) from reader_sync_cursors")
                l["l1"]["pending_conv"] = scalar(db, """select count(*) from reader_sync_cursors c
                    where exists (select 1 from reader_messages m
                                  where m.wxid=c.wxid and m.create_time > c.covered_through)""") or 0
                l["l1"]["pending_msgs"] = scalar(db, """select count(*) from reader_messages m
                    where exists (select 1 from reader_sync_cursors c
                                  where c.wxid=m.wxid and m.create_time > c.covered_through)""") or 0
                l["l1"]["pending_analysis"] = scalar(
                    db, "select count(*) from reader_sync_cursors where analysis_pending_revision>0") or 0
            finally:
                db.close()

    # ---- L2 事实与结论（两个库分工不同） ----
    kn = os.path.join(R, "knowledge.sqlite")
    l["l2"]["knowledge_path"] = kn
    l["l2"]["knowledge_exists"] = os.path.exists(kn)
    if l["l2"]["knowledge_exists"]:
        db = paths.open_ro(kn)
        if db:
            try:
                for k, t in (("facts", "knowledge_facts"), ("materials", "knowledge_materials"),
                             ("watermarks", "knowledge_watermarks"), ("imported_people", "knowledge_investors")):
                    l["l2"][k] = scalar(db, f"select count(*) from {t}")
            finally:
                db.close()
    a = paths.check()
    l["l2"]["business"] = a["counts"]
    l["l2"]["business_path"] = a["path"]
    l["l2"]["business_mtime"] = a["mtime"]

    # ---- L3 wiki 画像（markdown 页） ----
    pdir, odir = os.path.join(R, "knowledge", "people"), os.path.join(R, "knowledge", "orgs")
    pages = []
    for d in (pdir, odir):
        if os.path.isdir(d):
            pages += [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".md")]
    l["l3"]["people_dir"], l["l3"]["orgs_dir"] = pdir, odir
    l["l3"]["people_pages"] = len([f for f in os.listdir(pdir) if f.endswith(".md")]) if os.path.isdir(pdir) else 0
    l["l3"]["org_pages"] = len([f for f in os.listdir(odir) if f.endswith(".md")]) if os.path.isdir(odir) else 0
    l["l3"]["newest"] = (datetime.fromtimestamp(max(os.path.getmtime(p) for p in pages)).strftime("%Y-%m-%d %H:%M")
                         if pages else None)

    db = paths.open_ro()
    if db:
        try:
            people = {r[0] for r in db.execute("select name from investors")}
            orgs = {r[0] for r in db.execute("select name from organizations")}
            orgs_with_sum = {r[0] for r in db.execute(
                "select name from organizations where coalesce(strategy_summary,'')!=''")}
            have_p = {f[:-3] for f in os.listdir(pdir) if f.endswith(".md")} if os.path.isdir(pdir) else set()
            have_o = {f[:-3] for f in os.listdir(odir) if f.endswith(".md")} if os.path.isdir(odir) else set()
            # 生成器会把名字里的 / \ : * ? " < > | 换成下划线，比对时同步归一化，
            # 否则 "Ady | 亿梯资本" 这类名字会被误判成缺页
            people = {_safe(n) for n in people}
            orgs = {_safe(n) for n in orgs}
            orgs_with_sum = {_safe(n) for n in orgs_with_sum}
            l["l3"]["people_total"] = len(people)
            l["l3"]["org_total"] = len(orgs)
            l["l3"]["people_missing"] = len(people - have_p)
            l["l3"]["org_missing"] = len(orgs - have_o)
            l["l3"]["org_missing_with_summary"] = len(orgs_with_sum - have_o)
        finally:
            db.close()
    return l


def layers_block():
    l = layers()
    L = ["═══ 三层增量视图 ═══", "维护顺序固定：只读 L1 增量 → 写 L2 → 由 L2 重生成 L3。不许越级，不许反向。", ""]

    x = l["l1"]
    L.append("─── L1 微信热读取（实时增量，只读）───")
    if not x.get("exists"):
        L.append("⛔ reader-bridge.sqlite 不存在")
    else:
        L.append(f"{x['path']}")
        L.append(f"  消息 {x['msgs']} 条 / {x['convs']} 会话 · 最后 {fmt_age(x['last_msg'])}")
        L.append(f"  游标 {x['cursors']} 个 · 最后同步 {fmt_age((x['last_sync'] or '').replace('T', ' ').replace('.000Z','').replace('Z',''))}")
        if x["pending_msgs"]:
            L.append(f"  ⚠️ 待入 L2：{x['pending_msgs']} 条（{x['pending_conv']} 个会话游标之后还有新消息）")
        else:
            L.append("  ✅ 游标已覆盖到最后一条，L1 无积压")
        if x["pending_analysis"]:
            L.append(f"  ⚠️ 标记重分析的会话：{x['pending_analysis']} 个")
    L.append("")

    y = l["l2"]
    L.append("─── L2 事实与结论（唯一数据库）───")
    if y.get("knowledge_exists"):
        L.append(f"  事实库 knowledge.sqlite：事实 {y.get('facts')} / 材料 {y.get('materials')} / "
                 f"水位 {y.get('watermarks')} / 导入人 {y.get('imported_people')}")
    b = y["business"]
    L.append(f"  业务库 marx.sqlite：投资人 {b.get('investors')} / 机构 {b.get('organizations')} / "
             f"证据 {b.get('external_intel')} / 推送 {b.get('opportunities')} / 画像 {b.get('profiles')}")
    L.append("")

    z = l["l3"]
    L.append("─── L3 wiki 画像（人页 / 机构页）───")
    L.append(f"  人页 {z.get('people_pages')} / {z.get('people_total')}（不同姓名）"
             + (f"，缺 {z['people_missing']}" if z.get("people_missing") else "，全覆盖 ✅"))
    L.append(f"  机构页 {z.get('org_pages')} / {z.get('org_total')}"
             + (f"，缺 {z['org_missing']}（其中有策略摘要的 {z.get('org_missing_with_summary')} 家）"
                if z.get("org_missing") else "，全覆盖 ✅"))
    L.append(f"  最后生成 {fmt_age(z['newest'])}")
    if z.get("org_missing_with_summary"):
        L.append(f"  ⚠️ 断点在这里：L2 已有机构结论、L3 没页 → 先补这 {z['org_missing_with_summary']} 家")
    L.append("")
    L.append("下一步：" + next_step(l))
    return "\n".join(L)


def next_step(l):
    x, z = l["l1"], l["l3"]
    if x.get("pending_msgs"):
        return f"先跑 L1→L2 增量（{x['pending_msgs']} 条新消息），再谈别的。"
    if z.get("org_missing_with_summary"):
        return f"L1 已清空，欠账全在 L3：补 {z['org_missing_with_summary']} 家机构页（L2 有结论、L3 无页）。"
    if z.get("people_missing"):
        return f"补 {z['people_missing']} 个人页。"
    return "三层齐平，可以出稿。"


def dupes_block(h):
    a = h["archive"]
    L = ["─── 其他副本（都不是正本，只读对照，禁写）───"]
    if not h["dupes"]:
        L.append("（无）")
    for d in h["dupes"]:
        if not d["exists"]:
            L.append(f"· {d['path']}\n    已不存在")
            continue
        if d["size"] == 0:
            L.append(f"⛔ {d['path']}\n    0 字节空文件，无数据 —— {d['note']}")
            continue
        cnt = f"{d['investors']} 人" if d["investors"] is not None else "无 investors 表"
        tag = "⛔" if not d["has_new_schema"] else "△"
        L.append(f"{tag} {d['path']}\n    {cnt} · {d['size']/1024:.0f} KB · 最后修改 {d['mtime']}"
                 f" · {'无 organizations/external_intel 表' if not d['has_new_schema'] else '有完整表结构'}")
        L.append(f"    {d['note']}")
        # 只有「结构更新且人更多」才可能是真分流；旧结构库的 mtime 常被只读检查刷动，不作依据
        if d["has_new_schema"] and d["investors"] and a["counts"].get("investors") \
                and d["investors"] > a["counts"]["investors"]:
            L.append("    ⚠️ 这份人比正本多——可能有人一直在往这里写，先查清再合并")
    return "\n".join(L)


def render(h):
    return head_block(h) + "\n\n" + dupes_block(h)


def find(kw):
    db = paths.open_ro()
    if db is None:
        sys.exit("正本不存在")
    db.row_factory = sqlite3.Row
    L = []
    orgs = db.execute(
        "select * from organizations where name like ? or coalesce(aliases,'') like ?",
        (f"%{kw}%", f"%{kw}%")).fetchall()
    for o in orgs:
        L.append(f"■ 机构 {o['name']}  [{o['id']}]")
        for k in ("org_type", "fund_info", "contact_policy", "region"):
            if k in o.keys() and o[k]:
                L.append(f"   {k}: {str(o[k])[:120]}")
        s = (o["strategy_summary"] if "strategy_summary" in o.keys() else "") or ""
        if s:
            L.append(f"   策略摘要({len(s)} 字): {s[:200]}")
        n = scalar(db, f"select count(*) from external_intel where subject_id='{o['id']}'")
        L.append(f"   外部情报: {n} 条")
        for r in db.execute("select title,fetched_at,reliability from external_intel "
                            "where subject_id=? order by fetched_at desc limit 3", (o["id"],)):
            L.append(f"     · [{r['reliability']}] {r['title'][:60]} ({r['fetched_at'] or '无日期'})")
        ppl = db.execute("select name,title,stage,ticket,heat,relation_status from investors "
                         "where organization like ? limit 8", (f"%{o['name']}%",)).fetchall()
        if ppl:
            L.append("   关联投资人:")
            for p in ppl:
                L.append(f"     · {p['name']} / {p['title'] or '—'} / 阶段 {p['stage'] or '—'}"
                         f" / 关系 {p['relation_status'] or '空'}({p['heat'] or '—'})")
        L.append("")

    people = db.execute(
        "select id,name,organization,title,stage,ticket,heat,relation_status,region_pref,notes "
        "from investors where name like ? or organization like ? limit 15",
        (f"%{kw}%", f"%{kw}%")).fetchall()
    for r in people:
        L.append(f"● {r['name']} / {r['organization'] or '—'} / {r['title'] or '—'}")
        L.append(f"   阶段 {r['stage'] or '—'} · 单笔 {r['ticket'] or '—'} · 地域 {r['region_pref'] or '—'}"
                 f" · 关系 {r['relation_status'] or '空'}（{r['heat'] or '—'}）")
        for o in db.execute("select status,last_outbound_at from opportunities where investor_id=?"
                            " order by last_outbound_at desc limit 3", (r["id"],)):
            L.append(f"   推送: {o['status']} @ {o['last_outbound_at'] or '—'}")
        n = scalar(db, f"select count(*) from external_intel where subject_id='{r['id']}'")
        if n:
            L.append(f"   外部情报: {n} 条")
        if r["notes"]:
            L.append(f"   备注: {r['notes'][:160]}")
        L.append("")

    db.close()
    if not L:
        return f"没找到「{kw}」——机构名、别名、投资人姓名都没有命中。"
    return "\n".join(L)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--find", metavar="关键词")
    p.add_argument("--layers", action="store_true", help="三层增量视图（L1 微信 → L2 事实 → L3 画像）")
    p.add_argument("--dupes", action="store_true")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    h = health()
    if a.json:
        print(json.dumps(h, ensure_ascii=False, indent=2, default=str))
    elif a.find:
        print(head_block(h))
        print()
        print(find(a.find))
    elif a.layers:
        print(layers_block())
    elif a.dupes:
        print(dupes_block(h))
    else:
        print(render(h))

    bad = (not h["archive"]["exists"]) or h["archive"]["missing"] or not h["archive"]["canonical"]
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
