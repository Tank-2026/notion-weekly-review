import os, sys, re, requests, time
from datetime import datetime, timedelta

# ===== 从 GitHub Secrets 读取（本地测试用默认值） =====
TOKEN = os.environ.get("NOTION_TOKEN", "ntn_U10348912549fc7XCmbmK2T8i6z41vEW2QsDYYcJG285tm")
H = {"Authorization": f"Bearer {TOKEN}", "Notion-Version": "2022-06-28", "Content-Type": "application/json"}
# 周复盘页面（新建，每周六写入；手写区不会被覆盖）
WEEKLY_PAGE = os.environ.get("NOTION_WEEKLY_PAGE", "3f603daf-2ff6-818b-bd98-ded8571a48ec")
# 回顾纠偏页面（已转为月度复盘，保留兼容）
PAGE_ID = os.environ.get("NOTION_REVIEW_PAGE", "7445e6d5-15ca-4691-b34b-ca2db921909f")
KR_DB = os.environ.get("NOTION_KR_DB", "e9056e7c-2110-4b09-badb-da80f2ec42ee")
TASK_DB = os.environ.get("NOTION_TASK_DB", "31fa03dc-ff4a-47b1-8d27-ca3a749cb648")
SUMMARY_DB = os.environ.get("NOTION_SUMMARY_DB", "3a603daf-2ff6-81dc-ad40-e4e9ab85f938")
GOAL_PROJECTS = [
    "28903daf-2ff6-8081-92e8-de2e64a36b29",  # 熟悉茁乐瑞普多
    "3c883c49-f267-4ab8-82e5-6fdcb6ff6ed4",  # 中青年覆盖
    "2b203daf-2ff6-809b-9ac1-e2a8689a72f6",  # 客情跟进
]
GOAL_NAME = "2026年完成率120%"
# 设 WRITE_TO_NOTION=1 才真正写入 Notion；本地测试默认只打印
WRITE = os.environ.get("WRITE_TO_NOTION") == "1"
SYNC = "--sync" in sys.argv

CALL_MARK = "【Workbuddy 自动生成 · 周复盘 - "


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
    t = b.get("type")
    for k in ("paragraph", "heading_1", "heading_2", "heading_3", "callout", "toggle",
              "bulleted_list_item", "numbered_list_item", "to_do", "quote"):
        if t == k:
            return "".join(x.get("plain_text", "") for x in b.get(k, {}).get("rich_text", []))
    return ""


def trim(s, limit=1900):
    s = (s or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


def tname(p):
    return g(p, "任务名称") or "(无标题)"


def fmt_task(t):
    """任务名 + 关键执行信息（不含"重要"，该字段已弃用）。"""
    p = t.get("properties", {})
    nm = tname(p)
    due = g(p, "截止时间")
    nat = g(p, "任务性质")
    est = g(p, "预估时间")
    bits = []
    if nat:
        bits.append(nat)
    if est:
        bits.append(f"{int(est)}min")
    if due:
        bits.append(f"截止{due}")
    return f"• {nm}" + (f"　（{' · '.join(bits)}）" if bits else "")


# ===== 时间基准 =====
now = datetime.utcnow()
today = now.strftime("%Y-%m-%d")
y, w, _ = now.isocalendar()
week = f"{y}-W{w:02d}"

# ===== 1. KR（状态指标：名称 + 信心指数） =====
krs = query(KR_DB)
kr_lines = []
n_kr_active = 0
for r in krs:
    p = r.get("properties", {})
    nm = g(p, "关键成果") or ""
    st = g(p, "状态") or ""
    conf = g(p, "信心指数")
    if st in ("未开始", "进行中"):
        n_kr_active += 1
        conf_s = f"信心 {conf}/10" if conf is not None else "信心 —"
        kr_lines.append(f"• {nm}\n　　{st} · {conf_s}")
kr_text = "\n".join(kr_lines) or "（无进行中/未开始的 KR）"

# ===== 2. 任务统计（滚动窗口口径，保留原逻辑） =====
def is_work(p):
    dom = g(p, "领域")
    return dom in (None, "", "工作")


DONE_ST, DROP_ST = "完成", "废弃"
ACTIVE_ST = ("待办", "日程", "进行中", "等待")
PARK_ST = ("将来也许", "未整理")

tasks = query(TASK_DB)
work = [t for t in tasks if is_work(t.get("properties", {}))]


def window(a, b):
    """截止时间落在 [a,b] 的任务 → (全部, 已完成, 未完成)。「废弃」与停放区不计入分母。"""
    tot, ok, miss = [], [], []
    for t in work:
        p = t.get("properties", {})
        due = g(p, "截止时间")
        if not (due and a <= due <= b):
            continue
        st = g(p, "状态")
        if st in (DROP_ST,) or st in PARK_ST:
            continue
        tot.append(t)
        (ok if st == DONE_ST else miss).append(t)
    return tot, ok, miss


_mon = now - timedelta(days=now.weekday())
this_a = _mon.strftime("%Y-%m-%d")
this_b = (_mon + timedelta(days=6)).strftime("%Y-%m-%d")
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

push = sum(1 for t in w4_ok if g(t.get("properties", {}), "项目管理数据库"))
push_pct = round(push * 100 / len(w4_ok)) if w4_ok else 0
push_week = sum(1 for t in wk_ok if g(t.get("properties", {}), "项目管理数据库"))

active = [t for t in work if g(t.get("properties", {}), "状态") in ACTIVE_ST]
park = [t for t in work if g(t.get("properties", {}), "状态") in PARK_ST]
n_active = len(active)
n_park = len(park)

# ===== 3. 上周三件 → 现在的推进位置（自动扫，不做评价） =====
def _all_task_names():
    m = {}
    for t in work:
        m[tname(t.get("properties", {}))] = t.get("properties", {})
    return m


TASK_MAP = _all_task_names()


def locate(name):
    """按任务名找当前状态，返回位置描述（只记位置，不判对错）。"""
    p = TASK_MAP.get(name)
    if not p:
        return "（未匹配到任务）"
    st = g(p, "状态") or "—"
    if st == DONE_ST:
        return "已完成 ✅"
    if st in PARK_ST:
        return f"停放（{st}）"
    if st == DROP_ST:
        return "已废弃"
    return "推进中" if st in ("进行中", "日程") else f"未动（{st}）"


def last_week_three():
    """读最近一条非本周复盘的「本周三件」，逐条定位当前位置。"""
    rows = query(SUMMARY_DB)
    if not rows:
        return [], ""
    def d(r):
        v = r.get("properties", {}).get("复盘日期", {}).get("date")
        return (v or {}).get("start", "")
    for r in sorted(rows, key=d, reverse=True):
        p = r.get("properties", {})
        if (g(p, "周次") or "") == week:
            continue
        raw = g(p, "本周三件") or ""
        names = [re.sub(r"^[①②③④⑤⑥\d]+[\.、]?\s*", "", ln.strip()) for ln in raw.split("\n") if ln.strip()]
        names = [n for n in names if n]
        return names, raw
    return [], ""


last_names, _ = last_week_three()
if last_names:
    circ = "①②③④⑤⑥⑦⑧⑨"
    last_three_text = "\n".join(
        f"{circ[i] if i < len(circ) else '·'} {nm} → {locate(nm)}" for i, nm in enumerate(last_names)
    )
else:
    last_three_text = "（首次运行，暂无上周三件记录）"

week_done_names = [tname(t.get("properties", {}))[:28] for t in wk_ok][:8]
week_done_text = "、".join(week_done_names) if week_done_names else "（本周暂无完成）"

# ===== 4. 候选（带「下一步动作」，供本人自选 3 件） =====
def cand_rank(t):
    p = t.get("properties", {})
    p12 = g(p, "P1/P2")
    pr = 0 if p12 == "P1" else (1 if p12 == "P2" else 3)
    nat = 0 if g(p, "任务性质") == "推进型" else 1
    return (pr, nat, g(p, "创建日期") or "9999")


cand_pool = [t for t in active if g(t.get("properties", {}), "任务性质") == "推进型"]
cand = sorted(cand_pool, key=cand_rank)[:6]

cand_lines = []
marks = "ABCDEF"
for i, t in enumerate(cand):
    p = t.get("properties", {})
    nm = tname(p)
    nxt = g(p, "下一步动作")
    est = g(p, "预估时间")
    scene = g(p, "任务场景")
    meta = []
    if est:
        meta.append(f"{int(est)}min")
    if scene and scene != "任意":
        meta.append(scene)
    meta_s = f"　[{(' · '.join(meta))}]" if meta else ""
    if nxt:
        cand_lines.append(f"{marks[i]}【{nm}】{meta_s}\n　　▸ {nxt}")
    else:
        cand_lines.append(f"{marks[i]}【{nm}】{meta_s}\n　　▸ ⚠️ 还没写「下一步动作」——请补一句，否则这一件无法启动")
cand_text = "\n".join(cand_lines) or "（暂无推进型候选）"

# ===== 5. 本周已选三件（P1）+ 防吃灰检查 =====
this_three = [t for t in active if g(t.get("properties", {}), "P1/P2") == "P1"]
three_lines = []
missing_action = []
for t in this_three:
    p = t.get("properties", {})
    nm = tname(p)
    nxt = g(p, "下一步动作")
    is_push = g(p, "任务性质") == "推进型"
    if nxt:
        three_lines.append(f"• {nm}\n　　▸ {nxt}")
    elif is_push:
        three_lines.append(f"• {nm}\n　　▸ ⚠️ 缺「下一步动作」—— 补一句")
        missing_action.append(nm)
    else:
        # 维护型/琐碎事不必写动作，它们靠截止时间管理
        three_lines.append(f"• {nm}　（{g(p, '任务性质') or '非推进型'}，靠截止时间管，不必写动作）")
if not three_lines:
    this_three_text = "（还没有标记 P1 的任务 —— 从上面候选里圈 3 个，在任务库标为 P1）"
elif len(this_three) > 3:
    this_three_text = f"⚠️ 当前有 {len(this_three)} 个 P1，建议收敛到 3 个：\n" + "\n".join(three_lines)
else:
    this_three_text = "\n".join(three_lines)

n_action_filled = sum(1 for t in active if g(t.get("properties", {}), "下一步动作"))
if missing_action:
    missing_note = (f"\n⚠️ 防吃灰检查：本周三件里有 {len(missing_action)} 件没写「下一步动作」"
                    f"（{('、'.join(missing_action))[:60]}）—— 补上才能启动")
elif this_three:
    missing_note = "\n✓ 本周三件都写了「下一步动作」"
else:
    missing_note = ""

# ===== 6. 未来 4 周（P2） =====
p2 = [t for t in active if g(t.get("properties", {}), "P1/P2") == "P2"]
p2_text = "\n".join("• " + tname(t.get("properties", {}))[:40] for t in p2[:12]) or "（暂无 P2 任务）"

# ===== 7. 我的处境 =====
park_names = [tname(t.get("properties", {}))[:22] for t in park][:5]
park_text = "、".join(park_names) if park_names else "（无）"

health_note = ("✓ 兑现率健康" if r4 >= 85 else "⚠️ 兑现率偏低：先看清单，是估多了还是卡住了" if r4 >= 70
               else "🚨 兑现率告急：下周只承诺 2 件")
trend = ("↑" if delta > 0 else "↓" if delta < 0 else "—") + f" {abs(delta)}pt"

# ===== 8. 正文（六板块） =====
content = f"""{CALL_MARK}{now.strftime('%Y-%m-%d')}】

① ✅ 上周我推进了什么
【上周三件 · 现在在哪一步】（只记位置，不做评价）
{last_three_text}

本周完成 {len(wk_ok)} 件（其中推进型 {push_week} 件）
{week_done_text}

──────────────────

② 🚀 本周推进三件
【候选 · 每个已带"下一步动作"】（从中圈 3 个，在任务库里标为 P1）
{cand_text}

【本周已选】（P1 任务）
{this_three_text}{missing_note}

──────────────────

③ 📊 状态指标（KR 信心指数）
{kr_text}

──────────────────

④ 📅 未来 4 周（P2 任务）
{p2_text}

──────────────────

⑤ 📦 我的处境
活跃欠账 {n_active} 件 · 将来也许 {n_park} 件 · KR 进行中 {n_kr_active} 项
将来也许 Top5（看一眼就行，本周不要求动）：{park_text}

──────────────────

📈 记分牌（参考，不是重点）
近 4 周兑现率 {r4}%（前 4 周 {r4_prev}%，{trend}） · 推进型占比 {push_pct}% · {health_note}
"""


def write_to_page(text, page=None):
    """把自动区写入页面：先清理旧自动区，再插到锚点（说明 callout）之后，手写区不受影响。"""
    page = page or WEEKLY_PAGE
    anchor = None
    for b in page_children(page):
        if b.get("type") == "callout":
            s = block_text(b)
            if s.startswith(CALL_MARK):
                try:
                    requests.delete(f"https://api.notion.com/v1/blocks/{b['id']}", headers=H, timeout=15)
                except Exception:
                    pass
            elif anchor is None and "Workbuddy" in s:
                anchor = b["id"]
    children = _text_to_callouts(text, "🤖")
    payload = {"children": children}
    if anchor:
        payload["after"] = anchor
    r = requests.patch(f"https://api.notion.com/v1/blocks/{page}/children",
                       headers=H, json=payload, timeout=20)
    print("写入周复盘页面:", r.status_code,
          r.json().get("message", "") if r.status_code != 200 else f"OK({len(children)}块)")
    return r.status_code


def _text_to_callouts(text, icon, prefix=""):
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


# ===== 9. 复盘记录数据库（三件追踪） =====
_PROJ_CACHE = {}


def valid_project_ids(ids):
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
    return ok


def build_summary_props():
    """「复盘记录数据库」一行：指标 + 三件追踪。"""
    circ = "①②③④⑤⑥⑦⑧⑨⑩"

    def _c(i):
        return circ[i] if i < len(circ) else f"{i+1}."

    last_line = "\n".join(f"{_c(i)} {nm}" for i, nm in enumerate(last_names)) if last_names else ""
    last_status = "\n".join(f"{_c(i)} {nm} → {locate(nm)}" for i, nm in enumerate(last_names)) if last_names else ""
    this_line = "\n".join(f"{_c(i)} {tname(t.get('properties', {}))}" for i, t in enumerate(this_three)) if this_three else ""
    return {
        "周次": {"title": [{"text": {"content": week}}]},
        "复盘日期": {"date": {"start": today}},
        "近4周兑现率": {"number": r4 / 100},
        "近4周完成数": {"number": len(w4_ok)},
        "本周完成数": {"number": len(wk_ok)},
        "活跃待办数": {"number": n_active},
        "推进型占比": {"number": push_pct},
        "关联目标": {"rich_text": [{"text": {"content": GOAL_NAME}}]},
        "关联项目": {"relation": [{"id": pid} for pid in valid_project_ids(GOAL_PROJECTS)]},
        "上周三件": {"rich_text": [{"text": {"content": trim(last_line)}}]},
        "上周三件推进情况": {"rich_text": [{"text": {"content": trim(last_status)}}]},
        "本周三件": {"rich_text": [{"text": {"content": trim(this_line)}}]},
    }


def write_row_body(page_id, text):
    """把完整正文写进该行页面内（幂等：先删旧自动正文）。"""
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
    """按「周次」upsert，返回 (response, row_page_id, was_update)。"""
    existing = query(SUMMARY_DB, {"filter": {"property": "周次", "title": {"equals": week}}})
    if existing:
        pid = existing[0]["id"]
        r = requests.request("PATCH", f"https://api.notion.com/v1/pages/{pid}",
                             headers=H, json={"properties": props}, timeout=20)
        return r, pid, True
    body = {"parent": {"database_id": SUMMARY_DB}, "properties": props}
    r = requests.request("POST", "https://api.notion.com/v1/pages",
                         headers=H, json=body, timeout=20)
    return r, (r.json().get("id") if r.status_code == 200 else None), False


def write_summary_row():
    props = build_summary_props()
    r, pid, was_update = _upsert(props)
    if r.status_code != 200:
        print("  ⚠ 首次写入失败，去掉「关联项目」重试:", str(r.json().get("message", ""))[:120])
        props.pop("关联项目", None)
        r, pid, was_update = _upsert(props)
    action = "更新" if was_update else "新建"
    print(f"复盘记录数据库[{action} {week}]:", r.status_code,
          r.json().get("message", "") if r.status_code != 200 else f"OK({pid})")
    if r.status_code == 200 and pid:
        write_row_body(pid, content)
    return r.status_code


if __name__ == "__main__":
    print(f"[周复盘] 周次:{week} | KR进行中:{n_kr_active} | 近4周:{len(w4_ok)}/{len(w4_tot)}={r4}%"
          f"(前4周{r4_prev}%) | 本周完成:{len(wk_ok)} | 活跃:{n_active} | 停放:{n_park} | "
          f"候选:{len(cand)} | P1三件:{len(this_three)} | 已填下一步动作:{n_action_filled} | "
          f"上周三件识别:{len(last_names)}")
    print("=" * 50)
    print(content)
    print("=" * 50)
    if WRITE:
        write_to_page(content)
        write_summary_row()
    else:
        print("【本地干跑，未写入 Notion。GitHub Actions 运行时会自动写入】")
        print("复盘记录数据库待写入行:", week)
