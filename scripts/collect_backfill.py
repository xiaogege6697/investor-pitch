#!/usr/bin/env python3
"""补档验收：收子 Agent 的回传，校验后合成可直接落库的 intel JSONL。

为什么要单独一道：
  派出去的子 Agent 会犯几类固定错误——subject 抄错一个字（落库时解析不到主体）、
  URL 编造或用域名根、把推断写进 snippet、标题自创导致去重失效。这些在落库前
  卡掉，比写进库再回滚便宜得多。本脚本只做机械校验，不做事实判断。

用法：
  # 看每个批次的合格率与问题分布（默认，不写任何东西）
  python3 collect_backfill.py --dir ~/Projects/.../2026-11-04-补档

  # 生成合并文件，供 intel.py 落库
  python3 collect_backfill.py --dir ... --emit

校验规则（与任务单一一对应）：
  subject  必须与输入 batch 里的 name 一字不差
  url      必须 http(s) 且长度 > 20，不能是域名根/搜索页
  snippet  ≤200 字（任务单要求 160，留 40 字容错），且不含推断词
  title    机构须以「机构背景卡」结尾，个人须以「履历与关注方向」结尾
  status   ok / not_found 二选一；not_found 不落库，只统计

产出（--emit）：
  <dir>/intel-merged.jsonl   合格且可解析的记录，直接喂 intel.py
  <dir>/reject.jsonl         被卡掉的，附 reason
  <dir>/report.md            批次合格率 + 缺口清单
"""
import argparse
import glob
import json
import os
import re
import sys

INFER_WORDS = ("可能", "据悉", "有望", "或将", "或许", "预计将", "据传", "疑似")
BAD_URL = re.compile(r"^https?://[^/]+/?$")          # 域名根
SEARCH_HOSTS = ("baidu.com/s", "google.com/search", "bing.com/search",
                "sogou.com/web", "so.com/s")


def load_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append((ln, json.loads(line)))
            except json.JSONDecodeError:
                out.append((ln, {"__badjson__": line[:80]}))
    return out


def norm(s):
    return (s or "").strip()


def check(d, batch_item):
    """返回 (ok?, reason)。batch_item 为该 subject 在任务包里的原始对象。"""
    if "__badjson__" in d:
        return False, "非法 JSON"
    status = norm(d.get("status"))
    if status not in ("ok", "not_found"):
        return False, f"status 非法：{status or '空'}"
    if status == "not_found":
        return True, "not_found"

    for k in ("subject", "title", "snippet", "url"):
        if not norm(d.get(k)):
            return False, f"缺字段 {k}"

    typ = batch_item.get("type")
    title = norm(d["title"])
    # 标题是幂等键，格式不对就按约定改写（不丢数据），只有完全不沾边才拒
    if typ == "org" and not title.endswith("机构背景卡"):
        if "机构背景" in title:
            title = norm(d["subject"]) + " 机构背景卡"
            d["title"] = title
        else:
            return False, f"title 不合规（机构须以「机构背景卡」结尾）：{title}"
    if typ == "investor" and not title.endswith("履历与关注方向"):
        if "履历" in title or "关注方向" in title:
            title = norm(d["subject"]) + " 履历与关注方向"
            d["title"] = title
        else:
            return False, f"title 不合规（个人须以「履历与关注方向」结尾）：{title}"

    url = norm(d["url"])
    if not url.startswith(("http://", "https://")):
        return False, "url 非 http(s)"
    if len(url) <= 20 or BAD_URL.match(url):
        return False, "url 像域名根/占位，不算来源"
    if any(h in url for h in SEARCH_HOSTS):
        return False, "url 是搜索结果页，不是原始来源"

    sn = norm(d["snippet"])
    if len(sn) > 200:
        # 超几个字就丢一条真事实不值得，截断保留前半（信息密度最高的部分）
        sn = sn[:199].rstrip("，,；;、 ") + "…"
        d["snippet"] = sn
    if len(sn) < 12:
        return False, "snippet 过短，没有信息量"
    hit = [w for w in INFER_WORDS if w in sn]
    if hit:
        return False, f"snippet 含推断词 {hit}"

    rel = norm(d.get("reliability")) or "media"
    if rel not in ("official", "registry", "business_registry", "media", "social"):
        return False, f"reliability 非法：{rel}"
    return True, "ok"


def main():
    ap = argparse.ArgumentParser(description="校验并合并补档回传")
    ap.add_argument("--dir", required=True)
    ap.add_argument("--emit", action="store_true", help="生成 intel-merged.jsonl / reject.jsonl / report.md")
    a = ap.parse_args()

    root = os.path.expanduser(a.dir)
    batches = sorted(glob.glob(os.path.join(root, "batch-*.jsonl")))
    if not batches:
        sys.exit(f"{root} 下没有 batch-*.jsonl")

    # 输入侧：每批允许的 subject → 对象
    allowed = {}
    for bp in batches:
        for _, item in load_jsonl(bp):
            allowed[norm(item.get("name"))] = item

    merged, rejects = [], []
    seen_subject = set()
    per_batch = []

    results = sorted(glob.glob(os.path.join(root, "result-*.jsonl")))
    for rp in results:
        rows = load_jsonl(rp)
        ok_n = nf_n = bad_n = 0
        covered = set()
        for ln, d in rows:
            subj = norm(d.get("subject"))
            item = allowed.get(subj)
            if not item:
                bad_n += 1
                rejects.append({"file": os.path.basename(rp), "line": ln,
                                "subject": subj, "reason": "subject 不在任务包内（抄错或自创）"})
                continue
            covered.add(subj)
            good, why = check(d, item)
            if not good:
                bad_n += 1
                rejects.append({"file": os.path.basename(rp), "line": ln,
                                "subject": subj, "reason": why})
                continue
            if why == "not_found":
                nf_n += 1
                continue
            if subj in seen_subject and norm(d["title"]).endswith(("机构背景卡", "履历与关注方向")):
                # 同主体同标题只留一条，防多批重复
                if any(m["subject"] == subj and m["title"] == norm(d["title"]) for m in merged):
                    bad_n += 1
                    rejects.append({"file": os.path.basename(rp), "line": ln,
                                    "subject": subj, "reason": "同主体同标题重复"})
                    continue
            seen_subject.add(subj)
            ok_n += 1
            rec = {"subject": subj, "title": norm(d["title"]), "snippet": norm(d["snippet"]),
                   "source_name": norm(d.get("source_name")), "reliability": norm(d.get("reliability")) or "media",
                   "url": norm(d["url"]), "published_at": norm(d.get("published_at"))}
            if item.get("type") == "org" and norm(d.get("append_to_strategy")):
                rec["append_to_strategy"] = norm(d["append_to_strategy"])
            merged.append(rec)
        # 对应批次的输入（batch-03 ↔ result-03）
        num = re.search(r"(\d+)", os.path.basename(rp))
        bp = os.path.join(root, f"batch-{num.group(1)}.jsonl") if num else ""
        miss = []
        if bp and os.path.exists(bp):
            miss = sorted(norm(i.get("name")) for _, i in load_jsonl(bp)
                          if norm(i.get("name")) not in covered)
        per_batch.append({"file": os.path.basename(rp), "ok": ok_n, "not_found": nf_n,
                          "rejected": bad_n, "未覆盖": miss})

    total = len(allowed)
    print("═══ 补档验收 ═══")
    print(f"任务对象 {total} 个 ｜ 收到 {len(results)} 个结果文件")
    print(f"合格可落库 {len(merged)} 条 ｜ not_found {sum(b['not_found'] for b in per_batch)} 条 "
          f"｜ 卡掉 {len(rejects)} 条")
    for b in per_batch:
        print(f"  {b['file']}：ok {b['ok']} / nf {b['not_found']} / 卡 {b['rejected']}"
              + (f" / 未覆盖 {len(b['未覆盖'])}" if b["未覆盖"] else ""))
    if rejects:
        print("\n卡掉原因分布：")
        from collections import Counter
        for r, n in Counter(x["reason"].split("：")[0] for x in rejects).most_common():
            print(f"  {n} 条 · {r}")

    if not a.emit:
        print("\n（未传 --emit，只校验不落盘）")
        return

    mp = os.path.join(root, "intel-merged.jsonl")
    with open(mp, "w", encoding="utf-8") as f:
        for r in merged:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(root, "reject.jsonl"), "w", encoding="utf-8") as f:
        for r in rejects:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    uncovered = [k for k in allowed if k not in seen_subject]

    # 受影响清单：落库后只重生成这些页（避免全量 --overwrite 冲掉手工查证页）
    touched_org, touched_ppl = [], []
    for r in merged:
        item = allowed.get(r["subject"]) or {}
        (touched_org if item.get("type") == "org" else touched_ppl).append(r["subject"])
    for fn, names in (("touched-orgs.txt", touched_org), ("touched-people.txt", touched_ppl)):
        with open(os.path.join(root, fn), "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(set(names))) + ("\n" if names else ""))

    lines = ["# 补档验收报告", "",
             f"- 任务对象：{total}",
             f"- 合格情报：{len(merged)} 条（可落库）",
             f"- 查无实据（not_found）：{sum(b['not_found'] for b in per_batch)} 个对象",
             f"- 校验卡掉：{len(rejects)} 条",
             f"- 完全未覆盖：{len(uncovered)} 个对象", ""]
    if uncovered:
        lines += ["## 未覆盖清单", ""] + [f"- {u}" for u in uncovered[:80]] + [""]
    if rejects:
        lines += ["## 卡掉明细（前 40 条）", "", "| 对象 | 原因 |", "|---|---|"]
        lines += [f"| {r['subject']} | {r['reason']} |" for r in rejects[:40]]
    with open(os.path.join(root, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n→ {mp}")
    print(f"→ {os.path.join(root, 'report.md')}")
    print("落库：intel.py --file intel-merged.jsonl --apply --append-strategy")


if __name__ == "__main__":
    main()
