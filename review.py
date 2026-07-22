import os, requests
from datetime import datetime, timedelta

# ===== 从 GitHub Secrets 读取（本地测试用默认值） =====
TOKEN = os.environ.get("NOTION_TOKEN", "ntn_U10348912549fc7XCmbmK2T8i6z41vEW2QsDYYcJG285tm")
H = {"Authorization": f"Bearer {TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
PAGE_ID = os.environ.get("NOTION_REVIEW_PAGE", "7445e6d5-15ca-4691-b34b-ca2db921909f")
KR_DB = os.environ.get("NOTION_KR_DB", "e9056e7c-2110-4b09-badb-da80f2ec42ee")
TASK_DB = os.environ.get("NOTION_TASK_DB", "31fa03dc-ff4a-47b1-8d27-ca3a749cb648")
# 设 WRITE_TO_NOTION=1 才真正写入 Notion；本地测试默认只打印
WRITE = os.environ.get("WRITE_TO_NOTION") == "1"


def query(db_id, filter_body=None):
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    out = []; body = {"page_size": 100}
    if filter_body:
        body.update(filter_body)
    while True:
        r = requests.post(url, headers=H, json=body, timeout=20)
        d = r.json()
        if r.status_code != 200:
            print("查询失败:", d.get("message"))
            return out
        out += d.get("results", [])
        if not d.get("has_more"):
            break
        body["start_cursor"] = d["next_cursor"]
    return out


def g(p, k):
    v = p.get(k, {}); t = v.get("type")
    if t in ("title", "rich_text"):
        return "".join([x.get("plain_text", "") for x in v.get(t, [])])
    if t == "status":
        s = v.get("status"); return s.get("name") if s else None
    if t == "select":
        s = v.get("select"); return s.get("name") if s else None
    if t == "number":
        return v.get("number")
    if t == "date":
        dd = v.get("date"); return dd.get("start")[:10] if dd else None
    if t == "relation":
        return v.get("relation") or []
    return None


now = datetime.utcnow()
today = now.strftime("%Y-%m-%d")
week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")

# 1. KR 状态
krs = query(KR_DB)
kr_lines = []
for r in krs:
    p = r.get("properties", {})
    name = g(p, "关键成果") or ""
    st = g(p, "状态") or ""
    conf = g(p, "信心指数") or ""
    act = g(p, "下次推进动作") or ""
    if st in ("未开始", "进行中"):
        kr_lines.append(f"• {name} | {st} | 信心:{conf} | 下一步:{act[:40]}")

# 2. 任务统计
tasks = query(TASK_DB)
done = [t for t in tasks if g(t.get("properties", {}), "状态") == "完成"]
total_done = len(done)
push = sum(1 for t in done if g(t.get("properties", {}), "项目管理数据库"))
maintain = total_done - push
push_pct = round(push * 100 / total_done) if total_done else 0

# 真延期：推进型 + 有截止日期 + 截止<今天 + 未完成
overdue = [t for t in tasks if (lambda p: g(p, "任务性质") == "推进型"
                                and g(p, "状态") != "完成"
                                and g(p, "截止时间")
                                and g(p, "截止时间") < today)(t.get("properties", {}))]
n_overdue = len(overdue)

# 本周到期未完成（截止日期在近 7 天内）
due_week = [t for t in tasks if (lambda p: g(p, "截止时间")
                                 and week_ago <= g(p, "截止时间") <= today
                                 and g(p, "状态") != "完成")(t.get("properties", {}))]
n_due_week = len(due_week)

content = f"""【Workbuddy 自动生成 · 周度复盘 - {now.strftime('%Y-%m-%d')}】

📊 任务结构（累计）
- 累计完成任务：{total_done} 条
- 推进型（挂项目）：{push} 条（{push_pct}%）
- 维护型：{maintain} 条（{100 - push_pct}%）
{"⚠️ 推进型<30%：本周偏简单重复，下周需把 KR 任务拉上来" if push_pct < 30 else "✓ 推进型占比健康"}

🚨 真延期（推进型·逾期未完成）：{n_overdue} 条  ← 本周重点清理对象
📅 本周到期未完成：{n_due_week} 条

🎯 需关注 KR（未开始/进行中）
""" + "\n".join(kr_lines) + """

📝 你的动作
1. 回顾上周「下周三件事」完成情况
2. 处理真延期任务：要么拆解到可启动，要么改合理截止日
3. 更新各 KR 信心指数
4. 确定下周三件事（必须关联 KR）
"""


def write_to_page(text):
    block = {
        "type": "callout",
        "callout": {
            "rich_text": [{"type": "text", "text": {"content": text[:2000]}}],
            "icon": {"type": "emoji", "emoji": "🤖"},
        },
    }
    r = requests.patch(f"https://api.notion.com/v1/blocks/{PAGE_ID}/children",
                       headers=H, json={"children": [block]}, timeout=20)
    print("写入回顾纠偏页面:", r.status_code, r.json().get("message", "") if r.status_code != 200 else "OK")


if __name__ == "__main__":
    print(f"KR需关注:{len(kr_lines)} | 累计完成:{total_done} | 推进型%:{push_pct} | 真延期:{n_overdue} | 本周到期:{n_due_week}")
    print("=" * 50)
    print(content)
    if WRITE:
        write_to_page(content)
    else:
        print("=" * 50)
        print("【本地干跑，未写入 Notion。GitHub Actions 运行时会自动写入】")
