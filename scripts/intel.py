#!/usr/bin/env python3
"""外部情报落库：把联网查证到的机构/投资人事实，按既有规范写进 external_intel。

为什么单独做这一步：
  查完就写在 md 里，下次还得重查。档案库里的 organizations.strategy_summary 已经
  累积了大量结论，但很多**没有对应证据条目**——无法追溯来源、无法判断新鲜度、
  过期了也不知道。这个脚本把"结论"和"证据"配对补齐。

用法：
  # 看一眼某机构已有什么情报
  python3 intel.py --show 某创投

  # 先 dry-run 看会写什么（默认不写库）
  python3 intel.py --file batch.jsonl

  # 确认后写入
  python3 intel.py --file batch.jsonl --apply

  # 同时把结论追加到机构卡 strategy_summary（带日期，去重）
  python3 intel.py --file batch.jsonl --append-strategy --apply

JSONL 每条字段（subject + title + snippet 必填，其余可空）：
  {"subject":"某创投",            # 机构名或投资人姓名，自动解析成 org-xxx
   "title":"某创投连投某智能、某项目",
   "snippet":"某创投参与某智能天使轮…；2026-02 领投某项目数亿元…",
   "source_name":"搜狐财经/投资界",
   "reliability":"media",          # official|registry|business_registry|media
   "url":"https://...",
   "published_at":"2026-02-10",
   "append_to_strategy":"机器人与具身案例密集：某智能（数采手套路线）…"}
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 路径只从 paths.py 取，不许在本文件里写字面量
from paths import ARCHIVE_DB as DB  # noqa: E402


def conn(path, readonly=True):
    path = os.path.expanduser(path)
    if not os.path.exists(path):
        sys.exit(f"找不到库：{path}")
    if readonly:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return sqlite3.connect(path)


def resolve(db, subject):
    """机构名/别名/投资人姓名 → (subject_type, subject_id, 显示名)。"""
    s = subject.strip()
    row = db.execute("select id,name from organizations where name=?", (s,)).fetchone()
    if row:
        return "org", row[0], row[1]
    # 别名（JSON 数组）
    for oid, name, aliases in db.execute("select id,name,aliases from organizations"):
        if aliases and s in aliases:
            return "org", oid, name
    row = db.execute("select id,name from investors where name=?", (s,)).fetchone()
    if row:
        return "investor", row[0], row[1]
    # 模糊：机构名包含
    rows = db.execute("select id,name from organizations where name like ? limit 2",
                      (f"%{s}%",)).fetchall()
    if len(rows) == 1:
        return "org", rows[0][0], rows[0][1]
    return None, None, None


def show(db, subject):
    st, sid, name = resolve(db, subject)
    if not sid:
        sys.exit(f"查不到：{subject}")
    print(f"=== {name}（{st} / {sid}）已有情报 ===\n")
    rows = db.execute("""select title,source_name,reliability,snippet,published_at,fetched_at,superseded_by
                         from external_intel where subject_type=? and subject_id=?
                         order by fetched_at desc""", (st, sid)).fetchall()
    if not rows:
        print("（无。机构卡上的结论目前没有证据条目支撑）")
        return
    for t, src, rel, snip, pub, fetched, sup in rows:
        flag = " [已被更新]" if sup else ""
        print(f"· {t}{flag}")
        print(f"  来源={src or '—'} 可靠性={rel} 发布={pub or '—'} 检索={fetched}")
        print(f"  {snip[:200]}\n")
    print(f"共 {len(rows)} 条")


def load_jsonl(path):
    items = []
    with open(os.path.expanduser(path)) as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError as e:
                sys.exit(f"第 {ln} 行不是合法 JSON：{e}")
            for k in ("subject", "title", "snippet"):
                if not d.get(k):
                    sys.exit(f"第 {ln} 行缺字段：{k}")
            items.append(d)
    return items


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--file", default=None)
    p.add_argument("--show", default=None)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--append-strategy", action="store_true")
    p.add_argument("--db", default=DB)
    a = p.parse_args()

    if a.show:
        show(conn(a.db), a.show)
        return
    if not a.file:
        sys.exit("用 --file 给 JSONL，或用 --show 查某机构")

    ro = conn(a.db)
    items = load_jsonl(a.file)

    plan, skipped, unresolved = [], [], []
    for d in items:
        st, sid, name = resolve(ro, d["subject"])
        if not sid:
            unresolved.append(d["subject"])
            continue
        # 去重：同主体同标题不重复写
        dup = ro.execute("""select id from external_intel
                            where subject_type=? and subject_id=? and title=?
                              and (superseded_by is null or superseded_by='')""",
                         (st, sid, d["title"])).fetchone()
        if dup:
            skipped.append((name, d["title"]))
            continue
        plan.append((st, sid, name, d))

    print("=== 落库计划 ===" if not a.apply else "=== 写入 ===")
    for st, sid, name, d in plan:
        print(f"  + [{name}] {d['title']}  ({d.get('reliability','media')} / {d.get('source_name','—')})")
    for name, title in skipped:
        print(f"  = 已存在，跳过：[{name}] {title}")
    for s in unresolved:
        print(f"  ! 无法解析主体，跳过：{s}")

    if not a.apply:
        print(f"\n[DRY-RUN] 将写入 {len(plan)} 条，跳过 {len(skipped)} 条，无法解析 {len(unresolved)} 条。加 --apply 执行。")
        return

    db = conn(a.db, readonly=False)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    n = 0
    for st, sid, name, d in plan:
        iid = f"intel-{sid}-{stamp}-{n}"
        db.execute("""insert into external_intel
            (id,subject_type,subject_id,title,url,source_name,reliability,snippet,
             published_at,fetched_at,fetched_by,superseded_by)
            values (?,?,?,?,?,?,?,?,?,?,'websearch','')""",
                   (iid, st, sid, d["title"], d.get("url", ""), d.get("source_name", ""),
                    d.get("reliability", "media"), d["snippet"], d.get("published_at", ""), now))
        n += 1
        # 可选：把结论追加进机构卡
        if a.append_strategy and d.get("append_to_strategy") and st == "org":
            cur = db.execute("select strategy_summary from organizations where id=?", (sid,)).fetchone()
            old = (cur[0] or "") if cur else ""
            add = d["append_to_strategy"]
            if add not in old:
                new = (old + "；" if old else "") + f"[{now[:10]} 查证] " + add
                db.execute("update organizations set strategy_summary=?, updated_at=? where id=?",
                           (new, now, sid))

    db.commit()
    print(f"\n已写入 {n} 条外部情报。")
    if a.append_strategy:
        print("机构卡 strategy_summary 已同步追加（带 [日期 查证] 前缀，可追溯）。")


if __name__ == "__main__":
    main()
