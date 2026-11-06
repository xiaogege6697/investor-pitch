#!/usr/bin/env python3
"""只挂情报、不动正文：给手工页追加「外部情报（可追溯）」一节。

为什么需要它：
  手工页（页头 `归档：…`）里有大量 L2 里没有的判断和原文摘录，**不能重生成**——一重生成就没了
  （2026-11-04 实测：某集团页丢了「旗下基金」「内部联系人」等 6 节，某投资人 4533 字缩到 1482）。
  但补档新写进 external_intel 的证据，又必须能在这类页上看到。
  所以给它们开一条只追加的路：正文一字不动，只在末尾补一节情报；已有该节就只替换那一节。

用法：
  python3 attach_intel.py --type org --only-file touched-orgs.txt --dry-run
  python3 attach_intel.py --type people --only-file touched-people.txt --apply
  python3 attach_intel.py --type org --only 某集团 --apply

与 build_*_wiki.py 的分工：
  build_*  = 生成器页（投影页）整页重写
  attach   = 手工页只追加情报节
"""
import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import ARCHIVE_DB, PEOPLE_PAGES_DIR, ORG_PAGES_DIR  # noqa: E402

HEAD = "## 外部情报（可追溯）"


def ro(path):
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.text_factory = lambda b: b.decode("utf-8", "replace")
    return db


def intel_rows(db, stype, sid):
    return db.execute("""
        select title, url, source_name, reliability, snippet, published_at, fetched_at
        from external_intel
        where subject_type=? and subject_id=?
          and (superseded_by is null or superseded_by='')
        order by coalesce(published_at,'') desc, coalesce(fetched_at,'') desc
    """, (stype, sid)).fetchall()


def render_section(rows, who):
    if not rows:
        return ""
    L = [HEAD, "",
         f"以下为档案 `external_intel` 中已收录的{who}查证记录，按发布时间/检索时间倒序；"
         "本节由 `attach_intel.py` 自动追加，页内其余内容为人工整理，未经本脚本改动。", ""]
    for title, url, src, rel, snip, pub, fet in rows:
        meta = []
        if src:
            meta.append(src)
        if rel:
            meta.append(f"可靠性 {rel}")
        if pub:
            meta.append(f"发布 {pub}")
        if fet:
            meta.append(f"检索 {fet[:10]}")
        L.append(f"- **{title or '（无标题）'}**" + (f"（{' · '.join(meta)}）" if meta else ""))
        if snip:
            L.append(f"  - {snip.strip().replace(chr(10), ' ')}")
        if url:
            L.append(f"  - {url}")
    L.append("")
    return "\n".join(L)


def splice(text, section):
    """已有该节就替换那一节（到下一个 ## 或文末），没有就追加到末尾。"""
    i = text.find(HEAD)
    if i == -1:
        return text.rstrip("\n") + "\n\n" + section
    j = text.find("\n## ", i + len(HEAD))
    tail = text[j + 1:] if j != -1 else ""
    return text[:i] + section + ("\n" + tail if tail else "")


def main():
    ap = argparse.ArgumentParser(description="给手工页只追加外部情报节")
    ap.add_argument("--type", required=True, choices=("org", "people"))
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--only-file", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=ARCHIVE_DB)
    a = ap.parse_args()

    names = list(a.only)
    if a.only_file:
        with open(os.path.expanduser(a.only_file), encoding="utf-8") as f:
            names += [ln.strip() for ln in f if ln.strip()]
    if not names:
        sys.exit("用 --only 或 --only-file 指定对象")

    db = ro(a.db)
    base = ORG_PAGES_DIR if a.type == "org" else PEOPLE_PAGES_DIR
    stype = "org" if a.type == "org" else "investor"

    done, skipped, missing = 0, [], []
    for name in names:
        # 机构按名精确匹配；人可能有同名多条记录，情报合并取
        if a.type == "org":
            ids = [r[0] for r in db.execute(
                "select id from organizations where name=?", (name,))]
        else:
            ids = [r[0] for r in db.execute("select id from investors where name=?", (name,))]

        rows = []
        for sid in ids:
            rows += intel_rows(db, stype, sid)
        if not rows:
            skipped.append((name, "库内无情报"))
            continue

        path = os.path.join(base, name.replace("/", "_") + ".md")
        if not os.path.exists(path):
            missing.append(name)
            continue
        text = open(path, encoding="utf-8", errors="replace").read()
        new = splice(text, render_section(rows, "机构" if a.type == "org" else "本人"))
        if new == text:
            skipped.append((name, "无变化"))
            continue
        if a.apply:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
        done += 1

    print(f"{'已写入' if a.apply else '预览'}: {done} 页"
          + (f" · 跳过 {len(skipped)}" if skipped else "")
          + (f" · 找不到页 {len(missing)}" if missing else ""))
    for n, why in skipped[:6]:
        print(f"  跳过 {n}：{why}")
    if missing:
        print("  找不到页：" + "、".join(missing[:8]))


if __name__ == "__main__":
    main()
