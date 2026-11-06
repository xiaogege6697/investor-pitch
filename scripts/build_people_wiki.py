#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L2 → L3 人页生成（确定性投影，零新增事实）

从 marx.sqlite 的 investors / opportunities / external_intel 与
knowledge.sqlite 的 knowledge_facts 机械投影成 wiki 人页，
模板照抄 2026-10-31 那批人页（结构化结论 / 证据链 / 推荐流行为 / 待核实 / 矛盾）。

挑选规则必须确定性：
- 证据按 category 优先级（反馈 > 承诺 > 身份 > 沟通风格 > 需求 > 风险 > 材料 > 其他）
  排序，同级按时间倒序，上限 --max-facts
- 不合并、不改写、不补充任何库里没有的内容

用法：
  python3 scripts/build_people_wiki.py --limit 5        # 预览到 /tmp/peoplepreview
  python3 scripts/build_people_wiki.py --apply          # 写入 knowledge/people/
  python3 scripts/build_people_wiki.py --apply --only 李哲 孙继刚
"""
import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from paths import ARCHIVE_DB, PEOPLE_PAGES_DIR, data_root  # noqa: E402

TODAY = datetime.now().strftime("%Y-%m-%d")
KNOW_DB = os.path.join(data_root(), "knowledge.sqlite")

# 证据优先级：越靠前越能说明"这人怎么看项目、能不能推"
CAT_RANK = {
    "feedback": 1, "commitment": 2, "identity": 3, "comm_style": 4,
    "demand": 5, "risk": 6, "pending": 7, "material_exchange": 8,
    "media_text": 9, "misc": 10,
}
CAT_LABEL = {
    "feedback": "反馈", "commitment": "承诺", "identity": "身份",
    "comm_style": "沟通风格", "demand": "需求", "risk": "风险",
    "pending": "待办", "material_exchange": "材料往来",
    "media_text": "媒体摘录", "misc": "其他",
}
# 这些话术性内容对判断无用，降级展示
NOISE = ("刚刚把你添加到通讯录", "我通过了你的朋友验证", "以上是打招呼的内容")


def safe_name(name):
    return re.sub(r"[/\\:*?\"<>|]", "_", name).strip() or "未命名"


def flat(text, limit=0):
    s = re.sub(r"\s*\n+\s*", " ／ ", (text or "").strip())
    s = re.sub(r"\s{2,}", " ", s)
    if limit and len(s) > limit:
        s = s[:limit] + "…"
    return s


def clean_quote(q, limit=180):
    """引用去掉 XML 外壳，只留人话"""
    if not q:
        return ""
    s = re.sub(r"<[^>]+>", " ", q)
    s = re.sub(r"&#x[0-9a-fA-F]+;", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def load_people(db, only=None, limit=0, overwrite=False):
    """按姓名分组：每组选一条主记录出页，其余作为 siblings 附在同页，避免同名覆盖"""
    existing = {f[:-3] for f in os.listdir(PEOPLE_PAGES_DIR) if f.endswith(".md")} \
        if os.path.isdir(PEOPLE_PAGES_DIR) else set()
    rows = db.execute("""
        select id, name, organization, title, classification, sectors,
               stage, stage_as_of, ticket, ticket_as_of,
               region_pref, region_as_of, relation_status, heat,
               raw_remark, raw_description, notes, phone, card_ocr, excluded, wxid
        from investors
        order by case coalesce(relation_status,'')
                   when 'active' then 1 when 'cooling' then 2
                   when 'no_chat' then 3 when 'skip' then 4 else 5 end,
                 name
    """).fetchall()
    keys = ["id", "name", "org", "title", "classification", "sectors",
            "stage", "stage_as_of", "ticket", "ticket_as_of",
            "region", "region_as_of", "relation", "heat",
            "raw_remark", "raw_desc", "notes", "phone", "card_ocr", "excluded", "wxid"]
    groups = {}
    for r in rows:
        d = dict(zip(keys, r))
        if only and d["name"] not in only:
            continue
        if not overwrite and not only and d["name"] in existing:
            continue
        groups.setdefault(d["name"], []).append(d)

    out = []
    for name, items in groups.items():
        out.append((name, items))
        if limit and len(out) >= limit:
            break
    return out


def facts_of(kdb, inv_id, max_n=12):
    """knowledge_facts 里该投资人的事实，按优先级 + 时间倒序"""
    if not kdb:
        return [], 0
    try:
        rows = kdb.execute("""
            select kf.category, kf.content, kf.quote, kf.source_time
            from knowledge_facts kf
            join knowledge_investors ki
              on ki.source_key = kf.source_key
             and ki.source_investor_id = kf.source_investor_id
            where ki.marx_investor_id = ?
              and (kf.state is null or kf.state != 'revoked')
        """, (inv_id,)).fetchall()
    except sqlite3.Error:
        return [], 0
    rows = [r for r in rows if r[1] or r[2]]
    rows.sort(key=lambda r: (CAT_RANK.get(r[0] or "", 99),
                             "" if r[3] else "0",
                             r[3] or ""), reverse=False)
    # 同 category 内按时间倒序
    rows.sort(key=lambda r: CAT_RANK.get(r[0] or "", 99))
    picked = rows[:max_n]
    picked.sort(key=lambda r: (CAT_RANK.get(r[0] or "", 99),
                               -(int(r[3][:4]) if r[3] and r[3][:4].isdigit() else 0)))
    return picked, len(rows)


def pushes_of(db, inv_id):
    try:
        return db.execute("""
            select coalesce(p.name,'—'), o.status, o.last_outbound_at,
                   coalesce(o.feedback,'')
            from opportunities o left join projects p on p.id = o.project_id
            where o.investor_id = ?
            order by coalesce(o.last_outbound_at,'') desc limit 10
        """, (inv_id,)).fetchall()
    except sqlite3.Error:
        return []


def intel_of(db, inv_id):
    """投资人级外部情报。subject_type='investor'，按发布/检索时间倒序。"""
    try:
        rows = db.execute("""
            select title, url, source_name, reliability, snippet,
                   published_at, fetched_at
            from external_intel
            where subject_type = 'investor' and subject_id = ?
              and (superseded_by is null or superseded_by = '')
            order by coalesce(published_at,'') desc, coalesce(fetched_at,'') desc
        """, (inv_id,)).fetchall()
    except sqlite3.Error:
        return []
    keys = ["title", "url", "source_name", "reliability", "snippet",
            "published_at", "fetched_at"]
    return [dict(zip(keys, r)) for r in rows]


def render(p, facts, total_facts, pushes, siblings=(), intel=()):
    L = []
    L.append(f"# {p['name']} · {p['org'] or '（机构未记录）'}\n")
    L.append(f"> 整理：{TODAY} · 由 L2 既有记录投影生成 · 私人只读证据整理"
             + (f" · 机构页：[[{p['org']}]]" if p["org"] else ""))
    L.append(f"> investor_id: `{p['id']}` · classification: "
             f"{p['classification'] or '未记录'} · 精确关联机构字段 organization="
             f"{p['org'] or '空'}")
    L.append("> 本轮未重新联系本人、未重新查证；下列内容均为既有记录，"
             "不能据此宣称当前仍然有效。\n")

    L.append("## 结构化结论（SQLite 同步）")
    cls = p["classification"] or "未记录"
    title = flat(p["title"]) or "未记录"
    L.append(f"- classification: {cls} ｜ 职位: {title}")
    L.append(f"- **阶段: {flat(p['stage']) or '未记录'}**"
             f"（口径：{p['stage_as_of'] or '未记录'}）")
    L.append(f"- **单笔: {flat(p['ticket']) or '未记录'}**"
             f"（口径：{p['ticket_as_of'] or '未记录'}）")
    L.append(f"- 地域: {flat(p['region']) or '未记录'}"
             f"（口径：{p['region_as_of'] or '未记录'}）")
    rel = p["relation"] or "未记录"
    L.append(f"- 关系: {rel} ｜ 温度: {p['heat'] or '未记录'}"
             + (f" ｜ 电话: {p['phone']}" if (p["phone"] or "").strip() else ""))
    sec = flat(p["sectors"]) or ""
    if sec and sec != "[]":
        L.append(f"- 行业标签（库内）：{sec}")
    if p["excluded"]:
        L.append("- ⚠ 该记录在库内标记为 excluded（已排除出推荐池），"
                 "原因见备注；本页保留供追溯，不代表可直接触达。")
    L.append("")
    if not any([p["stage"], p["ticket"], p["region"]]):
        L.append("_三维口径（阶段/单笔/地域）均为空。这类联系人只能靠聊天继续挖，"
                 "不能按已有记录推断偏好。_\n")

    L.append("## 证据链（按优先级排列）\n")
    if facts:
        L.append(f"共 {total_facts} 条既有事实，下表按「反馈 > 承诺 > 身份 > 沟通风格 > "
                 f"需求 > 风险 > 材料往来 > 其他」取前 {len(facts)} 条。\n")
        L.append("| 证据 | 内容 | 时间 | 引用原文 |")
        L.append("|---|---|---|---|")
        for cat, content, quote, t in facts:
            L.append("| {} | {} | {} | {} |".format(
                CAT_LABEL.get(cat or "", cat or "其他"),
                flat(content, 90).replace("|", "/") or "—",
                (t or "")[:10] or "—",
                clean_quote(quote, 90).replace("|", "/") or "—",
            ))
        L.append("")
        if total_facts > len(facts):
            L.append(f"_其余 {total_facts - len(facts)} 条见 "
                     f"`knowledge.sqlite → knowledge_facts`。_\n")
    else:
        L.append("_本人在 `knowledge_facts` 中没有已抽取的事实；"
                 "证据仅有下列备注类字段。_\n")

    L.append("### 备注类字段（原始记录）")
    L.append(f"- 备注编码（raw_remark）：{flat(p['raw_remark']) or '未记录'}")
    L.append(f"- 手写说明（notes）：{flat(p['notes']) or '未记录'}")
    L.append(f"- 自述（raw_description）：{flat(p['raw_desc']) or '未记录'}")
    if flat(p["card_ocr"]):
        L.append(f"- 名片OCR：{flat(p['card_ocr'], 600)}")
    if not any([flat(p["raw_remark"]), flat(p["notes"]),
                flat(p["raw_desc"]), flat(p["card_ocr"])]):
        L.append("- 证据缺口：该联系人除结构化字段外没有任何备注、自述或名片，"
                 "须保留未知，不能以机构信息补齐个人偏好。")
    L.append("")

    if intel:
        L.append("## 外部情报（可追溯）\n")
        L.append("以下为档案 `external_intel` 中已收录的本人级查证记录，"
                 "按发布时间/检索时间倒序；与上文「备注类字段」分属不同来源，"
                 "冲突时以带原始链接的为准并记入「矛盾」。\n")
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
                meta.append(f"检索 {(it['fetched_at'] or '')[:10]}")
            L.append(f"- **{head}**" + (f"（{' · '.join(meta)}）" if meta else ""))
            if it["snippet"]:
                L.append(f"  - {it['snippet'].strip().replace(chr(10), ' ')}")
            if it["url"]:
                L.append(f"  - {it['url']}")
        L.append("")

    L.append("## 推荐流行为\n")
    if pushes:
        L.append("| 项目 | 状态 | 最后推送 | 反馈 |")
        for pj, st, t, fb in pushes:
            L.append(f"| {pj} | {st or '—'} | {(t or '—')[:10]} | "
                     f"{flat(fb, 60).replace('|', '/') or '—'} |")
        L.append("")
    else:
        L.append("_库内 `opportunities` 没有该联系人的推送记录。"
                 "可能是从未推过，也可能推过没回填——微信原文才是准的。_\n")

    L.append("## 待核实")
    if not p["stage"]:
        L.append("- [ ] 阶段口径未记录：下次聊天直接问「最近主要看哪个阶段」。")
    if not p["ticket"]:
        L.append("- [ ] 单笔口径未记录：问「单笔一般多大」，这决定项目够不够格。")
    if not p["region"]:
        L.append("- [ ] 地域偏好未记录。")
    L.append("- [ ] 关系状态与温度取既有字段，未做本轮验证；触达前先看微信最近往来。")
    L.append("")

    L.append("## ⚠ 矛盾\n")
    L.append("暂无（本页只做单源投影，未跨源比对，因此空着不等于没有矛盾）。")
    L.append("")

    if siblings:
        L.append("## 同名其他记录\n")
        L.append(f"库内还有 {len(siblings)} 条同名记录，属不同主体，"
                 "**不是重复数据，不要合并**：\n")
        L.append("| investor_id | 机构 | 职位 | 关系 | 备注编码 |")
        L.append("|---|---|---|---|---|")
        for s in siblings:
            L.append("| `{}` | {} | {} | {} | {} |".format(
                s["id"], s["org"] or "—", flat(s["title"], 20) or "—",
                s["relation"] or "—",
                flat(s["raw_remark"], 40).replace("|", "/") or "—"))
        L.append("")

    L.append("## 来源与历史口径")
    L.append(f"- 来源：本机业务库 `marx.sqlite` → `investors.id={p['id']}`。")
    if facts:
        L.append("- 事实来源：`knowledge.sqlite` → `knowledge_facts`，"
                 "经 `knowledge_investors.marx_investor_id` 关联；已剔除 `state=revoked`。")
    L.append(f"- 推送来源：同库 `opportunities`，按 investor_id 关联。")
    L.append(f"- {TODAY}：由 `build_people_wiki.py` 从既有记录投影生成本页；"
             "没有新增数据库事实，没有重算画像。")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--only-file", default="", help="从文件读姓名清单（一行一个），避免命令行转义")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="连手工页也覆盖（默认保护：页头不是生成器签名的当手工页，跳过）")
    ap.add_argument("--max-facts", type=int, default=12)
    a = ap.parse_args()

    if not os.path.exists(ARCHIVE_DB):
        sys.exit(f"⛔ 主库不存在：{ARCHIVE_DB}")
    db = sqlite3.connect(f"file:{ARCHIVE_DB}?mode=ro", uri=True)
    db.text_factory = lambda b: b.decode("utf-8", "replace")

    kdb = None
    if os.path.exists(KNOW_DB):
        kdb = sqlite3.connect(f"file:{KNOW_DB}?mode=ro", uri=True)
        kdb.text_factory = lambda b: b.decode("utf-8", "replace")

    out_dir = os.path.expanduser(a.out_dir) if a.out_dir else (
        PEOPLE_PAGES_DIR if a.apply else "/tmp/peoplepreview")
    os.makedirs(out_dir, exist_ok=True)

    only = a.only
    if a.only_file:
        with open(os.path.expanduser(a.only_file), encoding="utf-8") as f:
            only = [ln.strip() for ln in f if ln.strip()]
    people = load_people(db, only, a.limit, a.overwrite)
    if not people:
        print("没有需要生成的人页（可能已全覆盖）。")
        return

    SIGN = "由 L2 既有记录投影生成"
    wrote, sib_total, protected = 0, 0, []
    REL_RANK = {"active": 1, "cooling": 2, "no_chat": 3, "skip": 4}
    for name, items in people:
        # 主记录：实据多的优先（页要有内容），其次关系活的优先
        scored = []
        for it in items:
            f, tot = facts_of(kdb, it["id"], a.max_facts)
            scored.append((tot, -REL_RANK.get(it["relation"] or "", 9), it, f))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        _, _, primary, facts = scored[0]
        total = scored[0][0]
        siblings = [s[2] for s in scored[1:]]
        md = render(primary, facts, total, pushes_of(db, primary["id"]), siblings,
                    intel_of(db, primary["id"]))
        path = os.path.join(out_dir, safe_name(name) + ".md")
        if a.apply and a.overwrite and not a.force and os.path.exists(path):
            head = open(path, encoding="utf-8", errors="replace").read(400)
            if SIGN not in head:
                protected.append(name)
                continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(md)
        wrote += 1
        sib_total += len(siblings)

    if protected:
        print(f"⚠ 保护 {len(protected)} 页手工页未覆盖（要强改加 --force）："
              + "、".join(protected[:8]) + ("…" if len(protected) > 8 else ""))
    print(f"{'已写入' if a.apply else '预览生成'} {wrote} 页 → {out_dir}"
          + (f"（含 {sib_total} 条同名附属记录）" if sib_total else ""))
    if a.apply:
        total = db.execute("select count(*) from investors").fetchone()[0]
        have = len([f for f in os.listdir(PEOPLE_PAGES_DIR) if f.endswith(".md")])
        names_have = len({f[:-3] for f in os.listdir(PEOPLE_PAGES_DIR) if f.endswith(".md")})
        names_all = db.execute("select count(distinct name) from investors").fetchone()[0]
        print(f"L3 人页：{have} 页 / 投资人 {total} 人 · "
              f"姓名覆盖 {names_have}/{names_all}（{names_have / names_all:.0%}）")


if __name__ == "__main__":
    main()
