import os, json, requests
from datetime import datetime, timedelta
TOKEN = os.environ["NOTION_TOKEN"]
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Notion-Version": "2022-06-28",
"Content-Type": "application/json"}
PAGE_ID = os.environ["NOTION_REVIEW_PAGE"]
KR_DB = os.environ["NOTION_KR_DB"]
TASK_DB = os.environ["NOTION_TASK_DB"]
def query(db_id, filter_body=None):
url = f"https://api.notion.com/v1/databases/{db_id}/query"
out = []
body = {"page_size": 100}
if filter_body: body.update(filter_body)
while True:
r = requests.post(url, headers=HEADERS, json=body)
d = r.json()
out += d.get("results", [])
if not d.get("has_more"): break
body["start_cursor"] = d["next_cursor"]
return out
def g(p, k):
v = p.get(k, {})
t = v.get("type")
if t in ("title","rich_text"): return "".join([x.get("plain_text","") for x in v.get(t,[])])
if t == "status": s = v.get("status"); return s.get("name") if s else None
if t == "select": s = v.get("select"); return s.get("name") if s else None
if t == "number": return v.get("number")
if t == "date": d = v.get("date"); return d.get("start")[:10] if d else None
if t == "relation": return v.get("relation") or []
return None
# 1. KR 状态
krs = query(KR_DB)
kr_lines = []
for r in krs:
p = r.get("properties",{})
name = g(p,"关键成果") or ""
st = g(p,"状态") or ""
conf = g(p,"信⼼指数") or ""
act = g(p,"下次推进动作") or ""
if st in ("未开始","进⾏中"):
kr_lines.append(f"• {name} | {st} | 信⼼:{conf} | 下⼀步:{act[:40]}")
# 2. 任务统计
now = datetime.utcnow()
week_ago = now - timedelta(days=7)
tasks = query(TASK_DB)
done = [t for t in tasks if g(t.get("properties",{}),"状态")=="完成"]
total_done = len(done)
push = sum(1 for t in done if g(t.get("properties",{}),"项⽬管理数据库"))
maintain = total_done - push
push_pct = round(push*100/total_done) if total_done else 0
# 3. ⽣成复盘内容
content = f"""【Workbuddy⾃动⽣成 - {now.strftime('%Y-%m-%d %H:%M')} UTC】
📊 本周任务结构分析：
- 完成任务总数：{total_done}条
- 推进型任务（挂了项⽬的）：{push}条（{push_pct}%）
- 维护型任务（未挂项⽬的）：{maintain}条（{100-push_pct}%）
{"⚠️ 推进型<30%：本周陷⼊简单重复，下周需拉KR任务上来" if push_pct < 30 else "✓ 推进型
占⽐正常"}
🎯 需关注的KR（未开始/进⾏中）：
""" + "\n".join(kr_lines) + """
📝 你的任务：
1. 回顾上周「下周三件事」完成情况
2. 更新各KR信⼼指数
3. 确定下周三件事（必须关联KR）
"""
# 4. 写⼊回顾纠偏⻚⾯
url = f"https://api.notion.com/v1/blocks/{PAGE_ID}/children"
block = {
"type": "callout",
"callout": {
"rich_text": [{"type": "text", "text": {"content": content[:2000]}}],
"icon": {"type": "emoji", "emoji": "🤖"}
}
}
r = requests.patch(url, headers=HEADERS, json={"children": [block]})
print(f"复盘草稿已写⼊: {r.status_code}")
print(content)
