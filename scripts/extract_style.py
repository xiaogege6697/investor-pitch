#!/usr/bin/env python3
"""从本地微信消息库提取「本人真实发出的话术」，归纳可复用的风格档案。

用途：话术风格不应靠模型想象，应来自本人真实发过的消息。
输出分两部分：① 样本清单（供人工判断）② 统计特征（供生成时对照）。

用法：
  python3 extract_style.py                    # 抽样 60 条，打印到终端
  python3 extract_style.py --limit 200        # 抽更多
  python3 extract_style.py --out ~/style.md   # 同时写成 markdown 档案
  python3 extract_style.py --days 180         # 只看最近半年（默认全部）
"""
import argparse
import json
import os
import re
import sqlite3
import statistics
import sys
from collections import Counter
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# 路径只从 paths.py 取，不许在本文件里写字面量
from paths import MSG_DB as DEFAULT_DB  # noqa: E402

# 功能词表：用于识别本人习惯用的「动作词」和「软化词」
ACTION_WORDS = ["引荐", "介绍", "对接", "看看", "聊聊", "请教", "咨询", "发您", "给您", "约",
                "方便", "能否", "可以", "帮我", "麻烦"]
SOFTENERS = ["不占您时间", "不合适", "打扰", "不好意思", "多谢", "谢谢", "辛苦",
             "不用回", "不急", "随手"]
HEDGES = ["可能", "大概", "差不多", "简单", "大概齐"]
OPENING_Q = ["关注吗", "感兴趣", "有没有", "是否", "在看吗", "看吗"]


def load(db_path, days, limit):
    if not os.path.exists(db_path):
        sys.exit(f"找不到消息库：{db_path}\n请用 --db 指定实际路径。")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    sql = """
        select create_time, wxid, json_extract(data,'$.text') as t
        from reader_messages
        where json_extract(data,'$.from_me')=1
          and json_extract(data,'$.kind')='text'
          and json_extract(data,'$.text') is not null
          and length(json_extract(data,'$.text')) between 18 and 400
    """
    params = []
    if days:
        sql += " and create_time >= strftime('%s','now') - ?"
        params.append(days * 86400)
    sql += " order by create_time desc limit ?"
    params.append(limit)
    rows = db.execute(sql, params).fetchall()
    db.close()
    return rows


def is_url_junk(t):
    """过滤纯链接、纯表情、纯标点。"""
    s = t.strip()
    if re.fullmatch(r"[\W_]+", s):
        return True
    if s.startswith("http") and len(s) < 120:
        return True
    if re.fullmatch(r"\[[^\]]{1,6}\]", s):
        return True
    return False


HOOK_PAT = re.compile(r"(关注吗|看吗|感兴趣|有兴趣|在看|看这方向|这方向)")
CARD_PAT = re.compile(r"(拟融|投后|轮|估值)")
NEED_PAT = re.compile(r"(产线|落地|扩产|固投|返投|迁址|注册地|诉求)")
ADDRESS_PAT = re.compile(r"^([\u4e00-\u9fa5]{1,3}(?:总|老师|经理|董|主任))[，,、]")
CLOSING_PAT = re.compile(r"(约|吃个饭|发您|发给您|方便|拜访|见面|聊一下|聊聊|坐坐)")


def split_roles(texts):
    """把本人消息分成三类：钩子句 / 详情卡 / 其他。"""
    hooks, cards, others = [], [], []
    for t in texts:
        if HOOK_PAT.search(t) and len(t) <= 80:
            hooks.append(t)
        elif CARD_PAT.search(t) and len(t) >= 25:
            cards.append(t)
        else:
            others.append(t)
    return hooks, cards, others


def collect_fields(cards):
    """从详情卡里统计"字段出现频次"，用来还原固定字段顺序。"""
    fields = {
        "产品/技术定位": 0, "客户验证": 0, "需求或订单量级": 0,
        "阶段与轮次": 0, "拟融金额+投后": 0, "地理位置": 0,
        "落地/产线诉求": 0, "解除顾虑": 0,
    }
    for c in cards:
        if re.search(r"(自研|全栈|基于|做)", c):
            fields["产品/技术定位"] += 1
        if re.search(r"(供货|客户|进.*供应链|订单|装机|应用于)", c):
            fields["客户验证"] += 1
        if re.search(r"(套|台|吨|量级|需求.*万|意向)", c):
            fields["需求或订单量级"] += 1
        if re.search(r"(种子|天使|Pre-?A|A轮|B轮|早期|成长期)", c):
            fields["阶段与轮次"] += 1
        if re.search(r"(拟融|投后|估值)", c):
            fields["拟融金额+投后"] += 1
        if re.search(r"(深圳|上海|北京|杭州|苏州|南京|武汉|成都|广州|在.{2,4}$)", c):
            fields["地理位置"] += 1
        if NEED_PAT.search(c):
            fields["落地/产线诉求"] += 1
        if re.search(r"(没有绑|不绑|不限|可以谈|都可以)", c):
            fields["解除顾虑"] += 1
    return fields


def analyze(rows):
    texts = [r[2].strip() for r in rows if not is_url_junk(r[2])]
    if not texts:
        sys.exit("没有可用的文本样本，放宽 --days 或 --limit 再试。")

    lens = [len(t) for t in texts]
    stats = {
        "样本数": len(texts),
        "字数中位数": int(statistics.median(lens)),
        "字数均值": int(statistics.mean(lens)),
        "字数P90": sorted(lens)[int(len(lens) * 0.9) - 1],
        "带问号比例": f"{sum('?' in t or '？' in t for t in texts) / len(texts):.0%}",
    }

    opens = Counter(t[:8] for t in texts)
    sents = [len(re.findall(r"[。！？!?；\n]", t)) + 1 for t in texts]

    def hits(words):
        c = Counter()
        for t in texts:
            for w in words:
                if w in t:
                    c[w] += 1
        return c

    # 称呼习惯
    addr = Counter()
    for t in texts:
        m = ADDRESS_PAT.match(t)
        if m:
            addr[m.group(1)] += 1

    hooks, cards, others = split_roles(texts)
    fields = collect_fields(cards)
    closing = hits(["约", "吃个饭", "发您", "方便", "线下", "拜访"])

    return dict(texts=texts, stats=stats, opens=opens, sents=sents,
                act=hits(ACTION_WORDS), soft=hits(SOFTENERS), openq=hits(OPENING_Q),
                addr=addr, hooks=hooks, cards=cards, others=others,
                fields=fields, closing=closing)


def render_md(d, days):
    stats, opens, sents = d["stats"], d["opens"], d["sents"]
    L = []
    L.append("# 本人微信话术风格档案\n")
    L.append(f"> 由 extract_style.py 自动生成 · {datetime.now():%Y-%m-%d %H:%M}"
             f" · 样本范围：{'最近' + str(days) + '天' if days else '全部历史'}\n")
    L.append("## 一、结构特征\n")
    L.append("| 指标 | 数值 |")
    L.append("|---|---|")
    for k, v in stats.items():
        L.append(f"| {k} | {v} |")
    L.append(f"| 平均分句数 | {statistics.mean(sents):.1f} |\n")

    L.append("## 二、称呼习惯\n")
    for k, n in d["addr"].most_common(10):
        L.append(f"- {k} ×{n}")
    L.append("\n> 生成时统一用「姓氏+总」，不要用「尊敬的」「您好」开头。\n")

    L.append("## 三、钩子句（第一段，用于开场）\n")
    L.append(f"共识别 {len(d['hooks'])} 条。真实样本：\n")
    for t in d["hooks"][:12]:
        L.append(f"- {t}")
    L.append("")

    L.append("## 四、详情卡字段出现频次（还原固定字段顺序）\n")
    L.append("| 字段 | 出现次数 |")
    L.append("|---|---|")
    for k, v in sorted(d["fields"].items(), key=lambda x: -x[1]):
        L.append(f"| {k} | {v} |")
    L.append("\n真实详情卡样本：\n")
    for t in d["cards"][:8]:
        L.append(f"- {t}")
    L.append("")

    L.append("## 五、动作词与收尾\n")
    L.append("动作词：" + "、".join(f"{k}({n})" for k, n in d["act"].most_common()))
    L.append("\n收尾词：" + "、".join(f"{k}({n})" for k, n in d["closing"].most_common()))
    L.append("\n> 本人最常用的收尾动作是「约见面」，不是「发BP」。\n")

    L.append("## 六、软化词\n")
    if d["soft"]:
        L.append("、".join(f"{k}({n})" for k, n in d["soft"].most_common()))
    else:
        L.append("（样本中几乎不用软化词，生成时不要滥用「不占您时间」）")
    L.append("")

    L.append("## 七、其他消息样本（含顾虑预答、跟进话术）\n")
    for t in d["others"][:15]:
        L.append(f"- {t}")
    L.append("")
    return "\n".join(L)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--days", type=int, default=0)
    p.add_argument("--limit", type=int, default=60)
    p.add_argument("--out", default="")
    a = p.parse_args()

    rows = load(os.path.expanduser(a.db), a.days, a.limit)
    d = analyze(rows)

    print("=== 结构特征 ===")
    for k, v in d["stats"].items():
        print(f"{k}: {v}")
    print(f"平均分句数: {statistics.mean(d['sents']):.1f}")

    print("\n=== 称呼习惯 ===")
    print("  " + "、".join(f"{k}({n})" for k, n in d["addr"].most_common(8)))

    print(f"\n=== 钩子句（{len(d['hooks'])} 条）===")
    for t in d["hooks"][:10]:
        print(f"  · {t}")

    print(f"\n=== 详情卡字段频次（{len(d['cards'])} 条）===")
    for k, v in sorted(d["fields"].items(), key=lambda x: -x[1]):
        print(f"  {k}: {v}")
    print("\n  详情卡样本：")
    for t in d["cards"][:5]:
        print(f"  · {t[:160]}")

    print("\n=== 动作词 ===")
    print("  " + ", ".join(f"{k}({n})" for k, n in d["act"].most_common()))
    print("=== 收尾词 ===")
    print("  " + ", ".join(f"{k}({n})" for k, n in d["closing"].most_common()))
    print("=== 软化词 ===")
    print("  " + (", ".join(f"{k}({n})" for k, n in d["soft"].most_common()) or "（几乎不用）"))

    if a.out:
        path = os.path.expanduser(a.out)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(render_md(d, a.days))
        print(f"\n已写入风格档案：{path}")


if __name__ == "__main__":
    main()
