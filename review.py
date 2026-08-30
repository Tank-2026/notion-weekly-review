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
week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
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

# 2. 任务统计（只排除明确标了「个人/其他」的任务；未分类的历史任务默认视为工作，
#    避免 2353 条历史任务因未填「领域」而全部归零。要更严格可改成 == "工作"）
def is_work(p):
    dom = g(p, "领域")
    return dom in (None, "", "工作")
tasks = query(TASK_DB)
work = [t for t in tasks if is_work(t.get("properties", {}))]
done = [t for t in work if g(t.get("properties", {}), "状态") == "完成"]
total_done = len(done)
push = sum(1 for t in done if g(t.get("properties", {}), "项目管理数据库"))
maintain = total_done - push
push_pct = round(push * 100 / total_done) if total_done else 0

# 真延期：工作 + 推进型 + 有截止日期 + 截止<今天 + 未完成
overdue = [t for t in work if (lambda p: g(p, "任务性质") == "推进型"
                               and g(p, "状态") != "完成"
                               and g(p, "截止时间")
                               and g(p, "截止时间") < today)(t.get("properties", {}))]
n_overdue = len(overdue)

# 本周到期未完成（工作领域，截止日期在近 7 天内）
due_week = [t for t in work if (lambda p: g(p, "截止时间")
                                and week_ago <= g(p, "截止时间") <= today
                                and g(p, "状态") != "完成")(t.get("properties", {}))]
n_due_week = len(due_week)

# 按「重要」优先级排序，列出具体任务（避免只给数字看不到是哪些）
overdue_sorted = sorted(overdue, key=lambda t: prank(t.get("properties", {})))[:8]
due_sorted = sorted(due_week, key=lambda t: prank(t.get("properties", {})))[:8]
# 本周重点关注：未完成(非完成/废弃)且填了「重要」的工作任务，按优先级取前 12
focus = [t for t in work if g(t.get("properties", {}), "状态") not in ("完成", "废弃")
         and g(t.get("properties", {}), "重要")]
focus_sorted = sorted(focus, key=lambda t: prank(t.get("properties", {})))[:12]
overdue_text = "\n".join(fmt_task(t) for t in overdue_sorted) or "（无）"
due_text = "\n".join(fmt_task(t) for t in due_sorted) or "（无）"
focus_text = "\n".join(fmt_task(t) for t in focus_sorted) or "（暂无标注「重要」的任务）"

content = f"""【Workbuddy 自动生成 · 周度复盘 - {now.strftime('%Y-%m-%d')}】

📊 任务结构（累计 · 工作领域；个人/其他已排除，未分类默认计为工作）
- 累计完成（工作）：{total_done} 条
- 推进型（挂项目）：{push} 条（{push_pct}%）
- 维护型：{maintain} 条（{100 - push_pct}%）
{"⚠️ 推进型<30%：本周偏简单重复，下周需把 KR 任务拉上来" if push_pct < 30 else "✓ 推进型占比健康"}

🚨 真延期（工作·推进型·逾期未完成）：{n_overdue} 条  ← 本周重点清理对象
{overdue_text}

📅 本周到期未完成（工作）：{n_due_week} 条
{due_text}

⭐ 本周重点关注（按重要度排序 · 工作）
{focus_text}

🎯 需关注 KR（未开始/进行中）
""" + "\n".join(kr_lines) + """

📝 你的动作
1. 回顾上周「下周三件事」完成情况
2. 处理真延期任务：要么拆解到可启动，要么改合理截止日
3. 更新各 KR 信心指数
4. 确定下周三件事（必须关联 KR）
"""


def write_to_page(text):
    # Notion 单块富文本上限 2000 字，按行切分为多个 callout，保证整份复盘不丢内容
    children = _text_to_callouts(text, "🤖")
    r = requests.patch(f"https://api.notion.com/v1/blocks/{PAGE_ID}/children",
                       headers=H, json={"children": children}, timeout=20)
    print("写入回顾纠偏页面:", r.status_code,
          r.json().get("message", "") if r.status_code != 200 else f"OK({len(children)}块)")


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


def health_status(push_pct):
    """推进型占比 → 健康度 select。"""
    if push_pct < 30:
        return "风险"
    if push_pct < 50:
        return "警告"
    return "健康"


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
        "累计完成工作": {"number": total_done},
        "推进型占比": {"number": push_pct},
        "真延期数": {"number": n_overdue},
        "本周到期数": {"number": n_due_week},
        "健康度": {"select": {"name": health_status(push_pct)}},
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
    print(f"[工作领域] 周次:{week} | KR需关注:{len(kr_lines)} | 累计完成:{total_done} | "
          f"推进型%:{push_pct} | 真延期:{n_overdue} | 本周到期:{n_due_week} | "
          f"重点关注:{len(focus)} | 健康度:{health_status(push_pct)}")
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
