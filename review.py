import os, sys, requests, time
from datetime import datetime, timedelta

# ===== 从 GitHub Secrets 读取（本地测试用默认值） =====
TOKEN = os.environ.get("NOTION_TOKEN", "ntn_U10348912549fc7XCmbmK2T8i6z41vEW2QsDYYcJG285tm")
H = {"Authorization": f"Bearer {TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
PAGE_ID = os.environ.get("NOTION_REVIEW_PAGE", "7445e6d5-15ca-4691-b34b-ca2db921909f")
KR_DB = os.environ.get("NOTION_KR_DB", "e9056e7c-2110-4b09-badb-da80f2ec42ee")
TASK_DB = os.environ.get("NOTION_TASK_DB", "31fa03dc-ff4a-47b1-8d27-ca3a749cb648")
# 「复盘记录数据库」：每次复盘在此库写入/更新一行结构化记录（按「周次」upsert，避免重复）。
SUMMARY_DB = os.environ.get("NOTION_SUMMARY_DB", "3a603daf-2ff6-81dc-ad40-e4e9ab85f938")
# 该目标关联的项目（用于「关联项目」列）。写入前会校验，已删除的会自动剔除，不影响整行写入。
GOAL_PROJECTS = [
    "2c303daf-2ff6-808b-b81d-d6c39aa3afdb",  # 2025POA汇报幻灯（已删除，会被自动剔除）
    "28903daf-2ff6-8081-92e8-de2e64a36b29",  # 熟悉茁乐瑞普多
    "3c883c49-f267-4ab8-82e5-6fdcb6ff6ed4",  # 中青年覆盖
    "2b203daf-2ff6-809b-9ac1-e2a8689a72f6",  # 客情跟进
]
GOAL_NAME = "2026年完成率120%"
# 每周复盘页上两个承诺小节的标题（用于抓取文本入库）
SEC_LAST = "回顾上周承诺"
SEC_NEXT = "确定下周承诺"
# 设 WRITE_TO_NOTION=1 才真正写入 Notion；本地测试默认只打印
WRITE = os.environ.get("WRITE_TO_NOTION") == "1"
# --sync：承诺回写模式（只更新承诺字段，不重新生成/覆盖指标）
SYNC = "--sync" in sys.argv


def query(db_id, filter_body=None):
    url = f"https://api.notion.com/v1/databases/{db_id}/query"
    out = []; body = {"page_size": 100}
    if filter_body:
        body.update(filter_body)
    while True:
        d = None
        for attempt in range(5):  # 网络抖动时重试，避免单次 TLS 中断导致整次复盘失败
            try:
                r = requests.post(url, headers=H, json=body, timeout=20)
                d = r.json()
                break
            except Exception as e:
                if attempt < 4:
                    time.sleep(2 + attempt)
                    continue
                print("查询异常(已重试):", type(e).__name__, str(e)[:80])
                return out
        if d is None or r.status_code != 200:
            print("查询失败:", (d or {}).get("message"))
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


# ===== 通用：读取页面块 / 抓取折叠小节文本 =====
def page_children(bid):
    """读取某块的子块；带重试，失败返回空列表（不中断主流程）。"""
    url = f"https://api.notion.com/v1/blocks/{bid}/children?page_size=100"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=H, timeout=20)
            if r.status_code == 200:
                return r.json().get("results", [])
        except Exception:
            time.sleep(2)
    return []


def block_text(b):
    """提取一个块的纯文本，覆盖页面上出现过的所有文本块类型。"""
    t = b.get("type")
    for k in ("paragraph", "heading_1", "heading_2", "heading_3", "callout", "toggle",
              "bulleted_list_item", "numbered_list_item", "to_do", "quote"):
        if t == k:
            return "".join(x.get("plain_text", "") for x in b.get(k, {}).get("rich_text", []))
    return ""


def read_section(page_id, title):
    """在页面中定位指定标题块（折叠小节），返回其子块文本拼接。
    支持：paragraph（回顾上周承诺用它）与 numbered_list_item（确定下周承诺用它）。"""
    for b in page_children(page_id):
        t = b.get("type")
        if t in ("heading_1", "heading_2", "heading_3", "toggle"):
            name = "".join(x.get("plain_text", "") for x in b.get(t, {}).get("rich_text", []))
            if title in name and b.get("has_children"):
                lines = []
                for c in page_children(b["id"]):
                    s = block_text(c).strip()
                    if s:
                        lines.append(s)
                    # 二级内容（toggle 内嵌套）
                    if c.get("has_children") and c.get("type") in ("toggle", "bulleted_list_item", "numbered_list_item"):
                        for c2 in page_children(c["id"]):
                            s2 = block_text(c2).strip()
                            if s2:
                                lines.append("  - " + s2)
                return "\n".join(lines)
    return ""


def trim(s, limit=1900):
    """Notion 单条 rich_text 上限 2000 字，超限时截断并提示。"""
    s = (s or "").strip()
    return s if len(s) <= limit else s[:limit] + "…（完整正文见本行页面内）"


# 「重要」优先级排序（数字越小越优先；未设置排最后）
PRIORITY = {"战略顶级": 0, "重点关键": 1, "常规重要": 2, "临时任务": 3, "细微末节": 4}

def tname(p):
    return g(p, "任务名称") or "(无标题)"

def prank(p):
    return PRIORITY.get(g(p, "重要"), 9)

def fmt_task(t):
    p = t.get("properties", {})
    imp = g(p, "重要"); due = g(p, "截止时间")
    tag = f"[{imp}] " if imp else ""
    du = f" · 截止{due}" if due else ""
    return f"• {tag}{tname(p)}{du}"


now = datetime.utcnow()
today = now.strftime("%Y-%m-%d")
y, w, _ = now.isocalendar()
week = f"{y}-W{w:02d}"  # 例：2026-W30

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

# ===== 2. 任务统计 —— 滚动窗口口径 =====
# 口径（已确认）：
#   分子 = 窗口内「截止时间」且 状态=完成 的任务数
#   分母 = 窗口内「截止时间」且 状态≠废弃 的任务数（含未完成）
#   依据：完成任务后会把「截止时间」改成完成当天，所以已完成任务的截止时间即完成日，
#         可直接用来归周；未完成任务的截止时间仍是原计划截止日。
#   副产物：拖期完成的任务会随截止时间一起挪到完成那周，因此历史周数字是滚动快照，
#         会随补录而变化（已确认接受）。
def is_work(p):
    dom = g(p, "领域")
    return dom in (None, "", "工作")

tasks = query(TASK_DB)
work = [t for t in tasks if is_work(t.get("properties", {}))]

DONE_ST, DROP_ST = "完成", "废弃"
ACTIVE_ST = ("待办", "日程", "等待")  # 真正欠账的状态；「将来也许/未整理」属停放区，不计

def window(a, b):
    """截止时间落在 [a,b] 的任务 → (分母, 已完成, 未完成)。「废弃」不计入分母。"""
    tot, ok, miss = [], [], []
    for t in work:
        p = t.get("properties", {})
        due = g(p, "截止时间")
        if not (due and a <= due <= b):
            continue
        st = g(p, "状态")
        if st == DROP_ST:
            continue
        tot.append(t)
        (ok if st == DONE_ST else miss).append(t)
    return tot, ok, miss

# 本周（周一~周日，自然周）
_mon = now - timedelta(days=now.weekday())
this_a = _mon.strftime("%Y-%m-%d")
this_b = (_mon + timedelta(days=6)).strftime("%Y-%m-%d")
# 近 4 周（滚动 28 天，含今天）与前 4 周（趋势基线）
w4_a = (now - timedelta(days=27)).strftime("%Y-%m-%d")
w4_b = today
p4_a = (now - timedelta(days=55)).strftime("%Y-%m-%d")
p4_b = (now - timedelta(days=28)).strftime("%Y-%m-%d")

w4_tot, w4_ok, w4_miss = window(w4_a, w4_b)
p4_tot, p4_ok, _ = window(p4_a, p4_b)
wk_tot, wk_ok, _ = window(this_a, this_b)

def rate(ok, tot):
    return round(len(ok) * 100 / len(tot)) if tot else 0

r4 = rate(w4_ok, w4_tot)
r4_prev = rate(p4_ok, p4_tot)
delta = r4 - r4_prev

# 推进型结构：近 4 周「已完成」中挂了项目的比例（不再用累计口径）
push = sum(1 for t in w4_ok if g(t.get("properties", {}), "项目管理数据库"))
maintain = len(w4_ok) - push
push_pct = round(push * 100 / len(w4_ok)) if w4_ok else 0

# 时点：活跃待办（排除已废弃与停放区，反映真实欠账）
active_todo = [t for t in work if g(t.get("properties", {}), "状态") in ACTIVE_ST]
n_active = len(active_todo)

# 清单（只列事实与任务名，不做拖沓/延期判断，由本人看页面时自行评估）
miss_sorted = sorted(w4_miss, key=lambda t: prank(t.get("properties", {})))[:8]
focus = [t for t in work if g(t.get("properties", {}), "状态") not in (DONE_ST, DROP_ST)
         and g(t.get("properties", {}), "重要")]
focus_sorted = sorted(focus, key=lambda t: prank(t.get("properties", {})))[:12]
miss_text = "\n".join(fmt_task(t) for t in miss_sorted) or "（无，近4周内到期任务均已完成）"
focus_text = "\n".join(fmt_task(t) for t in focus_sorted) or "（暂无标注「重要」的任务）"

# 兑现率 → 一句诊断（阈值与 health_status 保持一致）
if r4 >= 85:
    health_note = "✓ 兑现率健康，可考虑把下周承诺加码一件"
elif r4 >= 70:
    health_note = "⚠️ 兑现率偏低：先看下方清单，是估多了还是卡住了"
else:
    health_note = "🚨 兑现率告急：近4周四成以上到期任务没落地，建议下周只承诺 2 件事"

# 本周样本太小时不给百分比，避免 3/3 被读成"完美"
if len(wk_tot) < 5:
    week_note = f"- 样本仅 {len(wk_tot)} 件，不给百分比，只看方向"
elif len(wk_tot) == len(wk_ok):
    week_note = f"- 兑现率 {rate(wk_ok, wk_tot)}%（本周已全部落地）"
else:
    week_note = f"- 兑现率 {rate(wk_ok, wk_tot)}%（周尚未结束，还会变化）"

trend = ("↑" if delta > 0 else "↓" if delta < 0 else "—") + f" {abs(delta)}pt"

content = f"""【Workbuddy 自动生成 · 周度复盘 - {now.strftime('%Y-%m-%d')}】

🎯 近 4 周兑现率（主口径 · {w4_a} ~ {w4_b}）
- 到期 {len(w4_tot)} 件 / 完成 {len(w4_ok)} 件 → {r4}%
- 前 4 周 {r4_prev}% → {trend}
- 推进型（挂项目）{push} 件，占完成量 {push_pct}%
{health_note}

📅 本周（{this_a} ~ {this_b}，截至今天，周未结束）
- 到期 {len(wk_tot)} 件 / 完成 {len(wk_ok)} 件
{week_note}

📌 当前活跃待办：{n_active} 件
（只算 待办/日程/等待；已排除「废弃」与停放区「将来也许/未整理」）

📋 近 4 周内到期 · 尚未完成（只列清单，不做判断）
{miss_text}

⭐ 重点关注（按重要度排序）
{focus_text}

🎯 需关注 KR（未开始/进行中）
""" + "\n".join(kr_lines) + """

📝 你的动作
1. 回顾上周「下周三件事」完成情况
2. 处理上方清单：要么拆解到可启动，要么改合理截止日
3. 更新各 KR 信心指数
4. 确定下周三件事（必须关联 KR）
"""


CALL_MARK = "【Workbuddy 自动生成 · 周度复盘 - "


def archive_old_callouts(keep=4):
    """保留最近 keep 组「Workbuddy 自动生成」callout，归档更早的，避免回顾纠偏页面无限变长。
    每组 = 1 个带日期标题的 callout + 其后续的续接块（长文本被切分产生）。"""
    blocks = page_children(PAGE_ID)
    groups, cur = [], None
    for b in blocks:
        if b.get("type") != "callout":
            cur = None
            continue
        s = block_text(b)
        if s.startswith(CALL_MARK):
            cur = {"ids": [b["id"]]}
            groups.append(cur)
        elif cur is not None:
            cur["ids"].append(b["id"])  # 续接块，跟随上一组一起归档
    if len(groups) <= keep:
        return 0
    n = 0
    for gp in groups[:-keep]:
        for bid in gp["ids"]:
            try:
                r = requests.delete(f"https://api.notion.com/v1/blocks/{bid}", headers=H, timeout=15)
                if r.status_code == 200:
                    n += 1
            except Exception:
                pass
    print(f"历史复盘归档: 保留最近 {keep} 组，已归档 {n} 块")
    return n


def write_to_page(text):
    # Notion 单块富文本上限 2000 字，按行切分为多个 callout，保证整份复盘不丢内容
    children = _text_to_callouts(text, "🤖")
    r = requests.patch(f"https://api.notion.com/v1/blocks/{PAGE_ID}/children",
                       headers=H, json={"children": children}, timeout=20)
    print("写入回顾纠偏页面:", r.status_code,
          r.json().get("message", "") if r.status_code != 200 else f"OK({len(children)}块)")
    if r.status_code == 200:
        archive_old_callouts(keep=4)  # 只留最近 4 周，页面保持清爽


def _text_to_callouts(text, icon, prefix=""):
    """把长文本按 1900 字切块，转成 callout 块列表。"""
    chunks, cur = [], ""
    for ln in text.split("\n"):
        if cur and len(cur) + len(ln) + 1 > 1900:
            chunks.append(cur); cur = ln
        else:
            cur = (cur + "\n" + ln) if cur else ln
    if cur:
        chunks.append(cur)
    return [{"type": "callout", "callout": {
        "rich_text": [{"type": "text", "text": {"content": (prefix + c if i == 0 else c)}}],
        "icon": {"type": "emoji", "emoji": icon}}}
        for i, c in enumerate(chunks)]


def health_status(r):
    """近 4 周兑现率 → 健康度 select（阈值与正文 health_note 保持一致）。"""
    if r >= 85:
        return "健康"
    if r >= 70:
        return "警告"
    return "风险"


# ===== 关联项目：校验并剔除已删除的死链 =====
_PROJ_CACHE = {}

def valid_project_ids(ids):
    """过滤已删除/无权限的项目页，避免 relation 死链导致整行写入失败。"""
    ok = []
    for pid in ids:
        if pid in _PROJ_CACHE:
            if _PROJ_CACHE[pid]:
                ok.append(pid)
            continue
        good = False
        try:
            r = requests.get(f"https://api.notion.com/v1/pages/{pid}", headers=H, timeout=15)
            good = (r.status_code == 200) and (not r.json().get("archived"))
        except Exception:
            good = False
        _PROJ_CACHE[pid] = good
        if good:
            ok.append(pid)
        else:
            print(f"  ⚠ 关联项目已失效，已剔除: {pid}")
    return ok


def build_summary_props():
    """构造「复盘记录数据库」一行所需的 properties（指标部分）。"""
    return {
        "周次": {"title": [{"text": {"content": week}}]},
        "复盘日期": {"date": {"start": today}},
        "近4周兑现率": {"number": r4},
        "近4周完成数": {"number": len(w4_ok)},
        "本周完成数": {"number": len(wk_ok)},
        "活跃待办数": {"number": n_active},
        "推进型占比": {"number": push_pct},
        "健康度": {"select": {"name": health_status(r4)}},
        "关联目标": {"rich_text": [{"text": {"content": GOAL_NAME}}]},
        "关联项目": {"relation": [{"id": pid} for pid in valid_project_ids(GOAL_PROJECTS)]},
    }


def commit_props():
    """从回顾纠偏页面抓取两个承诺小节的文本。"""
    return {
        "上周承诺回顾": {"rich_text": [{"text": {"content": trim(read_section(PAGE_ID, SEC_LAST))}}]},
        "下周承诺三件事": {"rich_text": [{"text": {"content": trim(read_section(PAGE_ID, SEC_NEXT))}}]},
    }


def auto_summary_prop():
    return {"自动复盘摘要": {"rich_text": [{"text": {"content": trim(content, 1500)}}]}}


def write_row_body(page_id, text):
    """把完整自动正文写进该行页面正文（不受 2000 字属性上限限制）。
    幂等：先删除本行已有的自动正文块，再重新写入。"""
    for b in page_children(page_id):
        if b.get("type") == "callout" and block_text(b).startswith("【Workbuddy 自动正文"):
            try:
                requests.delete(f"https://api.notion.com/v1/blocks/{b['id']}", headers=H, timeout=15)
            except Exception:
                pass
    children = _text_to_callouts(text, "📄", prefix=f"【Workbuddy 自动正文 · {week}】\n\n")
    r = requests.patch(f"https://api.notion.com/v1/blocks/{page_id}/children",
                       headers=H, json={"children": children}, timeout=20)
    print("行内自动正文:", r.status_code,
          r.json().get("message", "") if r.status_code != 200 else f"OK({len(children)}块)")


def _upsert(props):
    """按「周次」upsert，返回 (response, row_page_id)。"""
    existing = query(SUMMARY_DB, {"filter": {"property": "周次", "title": {"equals": week}}})
    if existing:
        pid = existing[0]["id"]
        r = requests.request("PATCH", f"https://api.notion.com/v1/pages/{pid}",
                             headers=H, json={"properties": props}, timeout=20)
        return r, pid
    body = {"parent": {"database_id": SUMMARY_DB}, "properties": props}
    r = requests.request("POST", "https://api.notion.com/v1/pages",
                         headers=H, json=body, timeout=20)
    return r, (r.json().get("id") if r.status_code == 200 else None)


def write_summary_row():
    """向「复盘记录数据库」写入/更新一行：指标 + 承诺 + 摘要 + 行内正文。"""
    props = build_summary_props()
    props.update(commit_props())
    props.update(auto_summary_prop())
    r, pid = _upsert(props)
    if r.status_code != 200:
        # 关联关系异常时，去掉「关联项目」重试一次，保证这一行不会因为关联问题整行丢失
        print("  ⚠ 首次写入失败，去掉「关联项目」重试:", str(r.json().get("message", ""))[:120])
        props.pop("关联项目", None)
        r, pid = _upsert(props)
    print(f"复盘记录数据库[{'更新' if pid else '新建'} {week}]:", r.status_code,
          r.json().get("message", "") if r.status_code != 200 else f"OK({pid})")
    if r.status_code == 200 and pid:
        write_row_body(pid, content)  # 完整正文写进页面，便于留存与回溯
    return r.status_code


def find_latest_row():
    """返回「复盘日期」最新的一行（用于周一回写承诺）。"""
    rows = query(SUMMARY_DB)
    if not rows:
        return None
    def d(r):
        v = r.get("properties", {}).get("复盘日期", {}).get("date")
        return (v or {}).get("start", "")
    return sorted(rows, key=d, reverse=True)[0]


def sync_commitments():
    """--sync：把页面上现在写的两个承诺小节，回写到最近一条复盘记录。
    只更新承诺字段，不覆盖周六生成的指标与摘要。"""
    row = find_latest_row()
    if not row:
        print("未找到任何复盘记录，跳过回写")
        return None
    pid = row["id"]
    wk = "".join(x.get("plain_text", "")
                 for x in row.get("properties", {}).get("周次", {}).get("title", []))
    props = commit_props()
    r = requests.request("PATCH", f"https://api.notion.com/v1/pages/{pid}",
                         headers=H, json={"properties": props}, timeout=20)
    last = props["上周承诺回顾"]["rich_text"][0]["text"]["content"]
    nxt = props["下周承诺三件事"]["rich_text"][0]["text"]["content"]
    print(f"承诺回写[{wk}]:", r.status_code,
          r.json().get("message", "") if r.status_code != 200 else "OK")
    print(f"  上周承诺回顾: {len(last)} 字 | 下周承诺三件事: {len(nxt)} 字")
    if not last and not nxt:
        print("  ⚠ 两个小节都没读到内容，请检查页面上是否已填写、或小节标题是否变动")
    return r.status_code


if __name__ == "__main__":
    print(f"[窗口口径] 周次:{week} | KR需关注:{len(kr_lines)} | 近4周:{len(w4_ok)}/{len(w4_tot)}"
          f"={r4}%(前4周{r4_prev}%) | 本周:{len(wk_ok)}/{len(wk_tot)} | "
          f"活跃待办:{n_active} | 推进型%:{push_pct} | 重点关注:{len(focus)} | "
          f"健康度:{health_status(r4)}")
    print("=" * 50)
    print(content)
    print("=" * 50)
    if SYNC:
        print("【承诺回写模式 --sync】")
        if WRITE:
            sync_commitments()
        else:
            print("【本地干跑，未写入 Notion】")
            print("将回写:", commit_props())
    elif WRITE:
        write_to_page(content)
        write_summary_row()
    else:
        print("【本地干跑，未写入 Notion。GitHub Actions 运行时会自动写入】")
        props = build_summary_props(); props.update(commit_props()); props.update(auto_summary_prop())
        print("复盘记录数据库待写入行:", week, "|", props)