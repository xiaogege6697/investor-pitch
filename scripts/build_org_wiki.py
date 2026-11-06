#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L2 → L3 机构页生成（确定性投影，零新增事实）

从 marx.sqlite 的 organizations / investors / external_intel 机械投影成 wiki 页，
模板照抄 2026-10-31 那批「既有机构卡转 Wiki」页（79 页同构），不引入任何新判断。

为什么不用 LLM：这一层的纪律是"只整理、不新增"。让模型参与写作，它必然补事实、
润色措辞、补推测，违反该层最核心的约束。纯模板渲染毫秒级完成，也不需要并行。

用法：
  python3 scripts/build_org_wiki.py --dry-run --limit 5      # 预览到 /tmp/orgpreview
  python3 scripts/build_org_wiki.py --apply --min-summary 1  # 写入 knowledge/orgs/
  python3 scripts/build_org_wiki.py --apply --only 某创投 某资本
  python3 scripts/build_org_wiki.py --apply --include-empty  # 连无策略摘要的也出页
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import ARCHIVE_DB, ORG_PAGES_DIR  # noqa: E402

TODAY = datetime.now().strftime("%Y-%m-%d")

BOUNDARY = """## ⚠ 内外口径差异与使用边界
- 当前材料未形成可确认的明确矛盾；这不等于内外口径已完成一致性验证。
- 机构卡中的基金规模、管理规模、注册资本、在投金额均不是个人单笔投资额，不得互相替代。
- 个人带时间原话优先于机构宣传；空口径/legacy记录不能包装成本月最新确认。
- 关系状态仅是既有记录；它不授权自动触达，也不能单独证明当前投资权限。"""

TODO = """## 待核实
- [ ] 重新打开机构官方原始来源，核实主体、在管规模、策略和最新日期，并补可追溯链接。
- [ ] 对阶段、单笔、落地有约束的联系人，以带日期本人原话复核；未取得新证据前保留既有时间。
- [ ] 没有个人页面的联系人按真实复盘优先级名单补页；本次不虚构排名。"""

REL_LABEL = {
    "active": "active（近期有往来）",
    "cooling": "cooling（转冷）",
    "dead": "dead",
    "skip": "skip",
    "no_chat": "no_chat（无聊天记录）",
}


def safe_name(name):
    """机构名转合法文件名"""
    return re.sub(r"[/\\:*?\"<>|]", "_", name).strip() or "未命名"


def cell(value, as_of):
    v = (value or "").strip()
    a = (as_of or "").strip()
    if v and a:
        return f"{v}；口径时间：{a}"
    if v:
        return f"{v}；口径时间：未记录"
    return "未记录；口径时间：未记录，不能视为当前确认"


def flat(text, limit=0):
    """把多行字段压成单行，避免打断 markdown 列表结构"""
    s = re.sub(r"\s*\n+\s*", " ／ ", (text or "").strip())
    s = re.sub(r"\s{2,}", " ", s)
    if limit and len(s) > limit:
        s = s[:limit] + "…（截断，完整内容见库内 card_ocr）"
    return s


def load_orgs(db, only=None, include_empty=False, min_summary=1):
    q = """select id, name, org_type, fund_info, strategy_summary,
                  manual_notes, contact_policy, updated_at, aliases
           from organizations"""
    rows = db.execute(q).fetchall()
    out = []
    for r in rows:
        (oid, name, otype, fund, strat, manual, policy, upd, aliases) = r
        if only and name not in only:
            continue
        if not include_empty and len((strat or "").strip()) < min_summary:
            continue
        out.append(dict(id=oid, name=name, org_type=otype, fund_info=fund,
                        strategy=strat, manual=manual, policy=policy,
                        updated_at=upd, aliases=aliases))
    return out


def contacts_of(db, org_name):
    rows = db.execute("""
        select id, name, title, stage, stage_as_of, ticket, ticket_as_of,
               region_pref, region_as_of, relation_status, heat,
               raw_remark, notes, card_ocr, excluded
        from investors
        where organization = ?
        order by case coalesce(relation_status,'')
                   when 'active' then 1 when 'cooling' then 2
                   when 'no_chat' then 3 when 'skip' then 4 else 5 end,
                 name
    """, (org_name,)).fetchall()
    keys = ["id", "name", "title", "stage", "stage_as_of", "ticket", "ticket_as_of",
            "region_pref", "region_as_of", "relation_status", "heat",
            "raw_remark", "notes", "card_ocr", "excluded"]
    return [dict(zip(keys, r)) for r in rows]


def intel_of(db, oid):
    try:
        rows = db.execute("""
            select title, url, source_name, reliability, snippet,
                   published_at, fetched_at
            from external_intel
            where subject_type = 'org' and subject_id = ?
            order by coalesce(fetched_at,'') desc, coalesce(published_at,'') desc
        """, (oid,)).fetchall()
    except sqlite3.Error:
        return []
    keys = ["title", "url", "source_name", "reliability", "snippet",
            "published_at", "fetched_at"]
    return [dict(zip(keys, r)) for r in rows]


def render(org, contacts, intel):
    L = []
    L.append(f"# {org['name']}\n")
    L.append(f"> 整理：{TODAY} · 既有机构卡转Wiki · 私人只读证据整理")
    L.append(f"> org_id: `{org['id']}` · 机构卡 updated_at: `{org['updated_at']}`"
             f" · 精确关联联系人 {len(contacts)} 人")
    L.append("> 本轮未逐项重新获取外部原始来源；下列机构判断均保留为既有SQLite快照，"
             "不能据此宣称当前仍然有效。\n")

    L.append("## 基本盘（既有机构卡）")
    L.append(f"- **机构性质**：{flat(org['org_type']) or '未记录'}")
    L.append(f"- **基金与规模**：{flat(org['fund_info']) or '未记录'}")
    L.append(f"- **投资策略**：{flat(org['strategy']) or '未记录'}")
    L.append(f"- **接触/业务口径**：{flat(org['policy']) or '未记录'}")
    if flat(org["manual"]):
        L.append(f"- **整理备注**：{flat(org['manual'])}")
    try:
        al = [a for a in json.loads(org["aliases"] or "[]") if a and a != org["name"]]
    except (ValueError, TypeError):
        al = []
    if al:
        L.append(f"- **库内别名**：{'、'.join(al)}")
    L.append("")

    if intel:
        L.append("## 外部情报（可追溯）\n")
        L.append("以下为档案 `external_intel` 中已收录的查证记录，按检索时间倒序；"
                 "不含本页新增判断。\n")
        for it in intel:
            head = it["title"] or "（无标题）"
            meta = []
            if it["source_name"]:
                meta.append(it["source_name"])
            if it["reliability"]:
                meta.append(f"可靠性 {it['reliability']}")
            if it["published_at"]:
                meta.append(f"发布 {it['published_at']}")
            if it["fetched_at"]:
                meta.append(f"检索 {it['fetched_at']}")
            L.append(f"- **{head}**" + (f"（{' · '.join(meta)}）" if meta else ""))
            if it["snippet"]:
                L.append(f"  - {it['snippet'].strip().replace(chr(10), ' ')}")
            if it["url"]:
                L.append(f"  - {it['url']}")
        L.append("")

    L.append("## 内部联系人与个人口径\n")
    L.append("以下仅按 investors.organization 与机构名称精确匹配；"
             "职位、关系和偏好均是已有记录，未重新联系本人确认。\n")
    if contacts:
        L.append("| 姓名 | 已存职位 | 阶段及口径时间 | 单笔及口径时间 | 地区及口径时间 | 已存关系 |")
        L.append("|---|---|---|---|---|---|")
        for c in contacts:
            L.append("| {} | {} | {} | {} | {} | {} |".format(
                c["name"].replace("|", "/"),
                (c["title"] or "").strip() or "未记录",
                cell(c["stage"], c["stage_as_of"]),
                cell(c["ticket"], c["ticket_as_of"]),
                cell(c["region_pref"], c["region_as_of"]),
                c["relation_status"] or "未记录",
            ))
    else:
        L.append("_本机库内没有以该机构名精确匹配的联系人。可能是机构名写法不一致"
                 "（如简称/全称），或该机构尚未建人。_")
    L.append("")

    L.append("## 个人证据摘录（不新增判断）\n")
    if contacts:
        for c in contacts:
            L.append(f"### {c['name']} · `{c['id']}`")
            L.append(f"- 本人备注编码：{flat(c['raw_remark']) or '未记录'}")
            L.append(f"- 本人手写说明：{flat(c['notes']) or '未记录'}")
            if flat(c["card_ocr"]):
                L.append(f"- 既有名片OCR：{flat(c['card_ocr'], 1200)}")
            if not flat(c["notes"]) and not flat(c["card_ocr"]):
                L.append("- 证据缺口：本页没有该联系人的手写说明或名片OCR，"
                         "须保留未知，不能以机构卡补齐个人偏好。")
            if c["heat"]:
                L.append(f"- 关系温度（既有字段）：{c['heat']}")
            L.append("")
    else:
        L.append("_无联系人，故无个人证据可摘录。_\n")

    L.append(BOUNDARY + "\n")
    L.append(TODO + "\n")

    L.append("## 来源与历史口径")
    L.append(f"- 来源：本机业务库 `marx.sqlite` → `organizations.id={org['id']}`，"
             f"字段 org_type/fund_info/strategy_summary/contact_policy，"
             f"记录更新时间 {org['updated_at']}。")
    L.append("- 联系人来源：同库 investors 以 organization 精确匹配；"
             "个人三维口径时间取 stage_as_of/ticket_as_of/region_as_of，"
             "不以本次整理日期替代。")
    if intel:
        L.append(f"- 外部情报来源：同库 external_intel，subject_type='org'、"
                 f"subject_id=`{org['id']}`，共 {len(intel)} 条，逐条带来源与可靠性。")
    L.append(f"- {TODAY}：由 `build_org_wiki.py` 从既有记录投影生成本页；"
             "没有新增数据库事实，没有重算画像，没有触碰人工notes表或user_corrections。")
    L.append("")
    return "\n".join(L)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="真正写入；默认只预览")
    p.add_argument("--out-dir", default="", help="预览输出目录，默认 /tmp/orgpreview")
    p.add_argument("--only", nargs="*", default=None, help="只处理指定机构名")
    p.add_argument("--only-file", default="", help="从文件读机构名清单（一行一个），避免命令行转义")
    p.add_argument("--limit", type=int, default=0, help="只处理前 N 家")
    p.add_argument("--include-empty", action="store_true", help="连无策略摘要的也出页")
    p.add_argument("--min-summary", type=int, default=1, help="策略摘要最小字数门槛")
    p.add_argument("--overwrite", action="store_true", help="覆盖已存在的页（默认跳过）")
    p.add_argument("--force", action="store_true",
                   help="连手工页也覆盖（默认保护：页头不是生成器签名的当手工页，跳过）")
    a = p.parse_args()

    if not os.path.exists(ARCHIVE_DB):
        sys.exit(f"⛔ 主库不存在：{ARCHIVE_DB}")
    db = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
    db.text_factory = lambda b: b.decode("utf-8", "replace")

    out_dir = os.path.expanduser(a.out_dir) if a.out_dir else (
        ORG_PAGES_DIR if a.apply else "/tmp/orgpreview")
    os.makedirs(out_dir, exist_ok=True)

    existing = {f[:-3] for f in os.listdir(ORG_PAGES_DIR) if f.endswith(".md")} \
        if os.path.isdir(ORG_PAGES_DIR) else set()

    only = a.only
    if a.only_file:
        with open(os.path.expanduser(a.only_file), encoding="utf-8") as f:
            only = [ln.strip() for ln in f if ln.strip()]
    orgs = load_orgs(db, only, a.include_empty, a.min_summary)
    if not a.overwrite and not only:
        orgs = [o for o in orgs if o["name"] not in existing]
    if a.limit:
        orgs = orgs[:a.limit]

    if not orgs:
        print("没有需要生成的机构页（可能已全覆盖）。")
        return

    SIGN = "既有机构卡转Wiki"
    wrote, skipped_empty, protected = 0, 0, []
    for o in orgs:
        contacts = contacts_of(db, o["name"])
        intel = intel_of(db, o["id"])
        md = render(o, contacts, intel)
        if not a.apply and len(md) < 300:
            skipped_empty += 1
        path = os.path.join(out_dir, safe_name(o["name"]) + ".md")
        if a.apply and a.overwrite and not a.force and os.path.exists(path):
            head = open(path, encoding="utf-8", errors="replace").read(400)
            if SIGN not in head:
                protected.append(o["name"])
                continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        wrote += 1

    mode = "已写入" if a.apply else "预览生成"
    print(f"{mode} {wrote} 页 → {out_dir}")
    print(f"口径：策略摘要 ≥{a.min_summary} 字"
          + ("，且包含无摘要机构" if a.include_empty else "")
          + ("，覆盖已有页" if a.overwrite else "，跳过已有页"))

    if protected:
        print(f"⚠ 保护 {len(protected)} 页手工页未覆盖（要强改加 --force）："
              + "、".join(protected[:8]) + ("…" if len(protected) > 8 else ""))

    # 覆盖率
    if a.apply:
        total = db.execute("select count(*) from organizations").fetchone()[0]
        have = len([f for f in os.listdir(ORG_PAGES_DIR) if f.endswith(".md")])
        print(f"L3 机构页覆盖率：{have}/{total}（{have / total:.0%}）")


if __name__ == "__main__":
    main()
