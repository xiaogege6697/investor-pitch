#!/usr/bin/env python3
"""补档派单：盘点 L3 薄页面，输出可直接派给子 Agent 的批次任务包。

为什么要有这一步：
  L3 页面「齐」不等于「厚」。1211 个人页里大半只有一句备注，机构页里还有一批
  既没有外部情报也没有策略摘要。靠人肉翻目录挑对象既慢又容易漏，而且每次补档
  都要重新拼清单。这个脚本把「谁薄、薄在哪、该补什么」一次算清楚，拆成 N 个
  JSONL 批次，主进程只管照单派活、收单验收。

分档（按补档价值排序，不按姓名）：
  P  可推进的活人：relation_status=active/cooling 且事实 <3  —— 补完马上能用
  O  空机构：external_intel 0 条且 strategy_summary 为空    —— 外部可查，性价比最高
  S  纯骨架人：事实 0、无描述、无备注、无电话               —— 只有身份，补的是身份核实
  死号（dead/skip）默认不补，省 token。

用法：
  # 盘点，看会派多少活
  python3 plan_backfill.py

  # 生成 5 个批次到指定目录
  python3 plan_backfill.py --out ~/Projects/active/marx-assistant/outputs/2026-11-04-补档 --batches 5

  # 只盘点某一档
  python3 plan_backfill.py --tiers P

产出：
  <out>/batch-01.jsonl … batch-NN.jsonl   每个文件 = 一个子 Agent 的任务包
  <out>/manifest.json                      批次构成与总账

子 Agent 的交付格式见 intel.py 文件头的 JSONL 字段说明，回传后由主进程
`intel.py --file ... --apply` 统一落库，再由 build_*_wiki.py --overwrite 重生成页面。
"""
import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import ARCHIVE_DB, KNOWLEDGE_DB  # noqa: E402

PITCHABLE_REL = ("active", "cooling")
# 只补真投资人；peer/FA/同行、朋友、企业联系人补「投资机构情报」没有意义
INVESTOR_CLASS = ("investor", "suspected_investor")


def ro(path):
    if not os.path.exists(path):
        sys.exit(f"找不到库：{path}")
    import sqlite3
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def facts_by_investor(kdb):
    """knowledge_facts 的 id 是 source 命名空间，必须先过 knowledge_investors 映射。"""
    s2m = {s: m for s, m in kdb.execute(
        "select source_investor_id, marx_investor_id from knowledge_investors "
        "where marx_investor_id is not null and marx_investor_id <> ''")}
    out = Counter()
    for sid, n in kdb.execute(
            "select source_investor_id, count(*) from knowledge_facts "
            "where coalesce(revoked_at,'')='' group by 1"):
        mid = s2m.get(sid)
        if mid:
            out[mid] += n
    return out


def has_text(v):
    v = (v or "").strip()
    return bool(v) and v not in ("*", "-", "—", "未记录", "无", "[]", "{}")


def collect(db, kdb, tiers, include_dead, limits):
    fac = facts_by_investor(kdb)
    items = []

    # ── P 可推进的活人 ──
    if "P" in tiers:
        got = []
        for (iid, name, org, title, rel, stage, ticket, sec, desc, notes, phone, cls) in db.execute(
                "select id,name,organization,title,relation_status,stage,ticket,sectors,"
                "raw_description,notes,phone,classification from investors order by name"):
            if rel not in PITCHABLE_REL:
                continue
            if cls not in INVESTOR_CLASS or not has_text(org):
                continue
            if fac.get(iid, 0) >= 3:
                continue
            known = []
            if has_text(stage):
                known.append(f"阶段={stage}")
            if has_text(ticket):
                known.append(f"单笔={ticket}")
            if has_text(sec):
                known.append(f"赛道={sec}")
            if has_text(phone):
                known.append("有手机号")
            got.append({
                "tier": "P", "type": "investor", "id": iid, "name": name,
                "org": org or "", "title": title or "",
                "known": "｜".join(known) or "（几乎空白）",
                "facts": fac.get(iid, 0),
                "missing": [m for m, v in (
                    ("对外身份/关注方向", has_text(desc)),
                    ("代表案例与已投项目", False),
                    ("近期动态（近 12 个月）", False),
                ) if not v],
                "hint": (desc or notes or "")[:120],
            })
        items += got[:limits.get("P", len(got))]

    # ── O 空机构 ──
    if "O" in tiers:
        intel = {r[0] for r in db.execute(
            "select distinct subject_id from external_intel where subject_type='org'")}
        got = []
        for (oid, name, aliases, otype, summary, fund) in db.execute(
                "select id,name,aliases,org_type,strategy_summary,fund_info "
                "from organizations order by name"):
            if oid in intel or has_text(summary) or has_text(fund):
                continue
            got.append({
                "tier": "O", "type": "org", "id": oid, "name": name,
                "aliases": aliases or "", "org_type": otype or "",
                "known": "（零情报：无类型、无规模、无策略、无案例）",
                "missing": ["机构类型（国资/民营/美元/产业资本/CVC）",
                            "管理规模与基金期数", "投资策略与看的方向", "代表案例 2–3 个"],
            })
        items += got[:limits.get("O", len(got))]

    # ── S 纯骨架人 ──
    if "S" in tiers:
        got = []
        for (iid, name, org, title, rel, desc, notes, phone, cls) in db.execute(
                "select id,name,organization,title,relation_status,raw_description,notes,phone,"
                "classification from investors order by name"):
            if not include_dead and rel in ("dead", "skip"):
                continue
            if cls not in INVESTOR_CLASS:
                continue
            if fac.get(iid, 0) > 0:
                continue
            if has_text(desc) or has_text(notes) or has_text(phone):
                continue
            got.append({
                "tier": "S", "type": "investor", "id": iid, "name": name,
                "org": org or "", "title": title or "",
                "known": "（只有姓名和机构，连备注都没有）",
                "facts": 0,
                "missing": ["这个人是否真实存在且在该机构在职", "职位与关注方向"],
            })
        items += got[:limits.get("S", len(got))]

    return items


def split(items, n):
    """轮转切分：每一批都吃到 P/O/S 各档，避免某批全是难啃的。"""
    n = max(1, n)
    buckets = [[] for _ in range(n)]
    for i, it in enumerate(items):
        buckets[i % n].append(it)
    return [b for b in buckets if b]


def main():
    ap = argparse.ArgumentParser(description="盘点 L3 薄页面并拆成派单批次")
    ap.add_argument("--out", default="", help="输出目录；不传只打印盘点结果")
    ap.add_argument("--batches", type=int, default=5)
    ap.add_argument("--tiers", default="POS", help="P=活人 O=空机构 S=纯骨架")
    ap.add_argument("--include-dead", action="store_true", help="把 dead/skip 也算进来（默认不算）")
    ap.add_argument("--limit", default="", help="各档上限，如 P=60,O=150")
    ap.add_argument("--db", default=ARCHIVE_DB)
    ap.add_argument("--kdb", default=KNOWLEDGE_DB)
    a = ap.parse_args()

    limits = {}
    for part in filter(None, a.limit.split(",")):
        k, v = part.split("=")
        limits[k.strip().upper()] = int(v)

    db, kdb = ro(a.db), ro(a.kdb)
    items = collect(db, kdb, set(a.tiers.upper()), a.include_dead, limits)
    if not items:
        print("没有需要补的对象（或筛选条件太窄）。")
        return

    by_tier = Counter(i["tier"] for i in items)
    print(f"═══ 补档盘点 ═══  共 {len(items)} 个对象")
    label = {"P": "可推进活人（补完马上能用）", "O": "空机构（零情报）", "S": "纯骨架人（只有身份）"}
    for t in ("P", "O", "S"):
        if by_tier.get(t):
            print(f"  {t} {label[t]}：{by_tier[t]}")

    batches = split(items, a.batches)
    print(f"  切分：{len(batches)} 批，每批 "
          f"{min(len(b) for b in batches)}–{max(len(b) for b in batches)} 个")

    if not a.out:
        print("\n（未传 --out，只盘点不落盘）")
        return

    out = os.path.expanduser(a.out)
    os.makedirs(out, exist_ok=True)
    man = {"generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
           "db": a.db, "total": len(items), "by_tier": dict(by_tier), "batches": []}
    for idx, b in enumerate(batches, 1):
        p = os.path.join(out, f"batch-{idx:02d}.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for it in b:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        man["batches"].append({"file": os.path.basename(p), "count": len(b),
                               "tiers": dict(Counter(i["tier"] for i in b))})
        print(f"  → {p}  {len(b)} 个")
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=2)
    print(f"\n批次包已生成：{out}")
    print("下一步：派子 Agent（每批一个），回传 intel JSONL → intel.py --apply → build_*_wiki.py --overwrite")


if __name__ == "__main__":
    main()
