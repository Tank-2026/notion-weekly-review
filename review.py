import os, re, requests, time
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


# ===== 时间基准（统一用北京时间） =====
CN_OFFSET = timedelta(hours=8)
now = datetime.utcnow() + CN_OFFSET
today = now.strftime("%Y-%m-%d")
y, w, _ = now.isocalendar()
week = f"{y}-W{w:02d}"
MONTH_EN = ["January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December"]
month_label = f"{now.year}-{MONTH_EN[now.month - 1]}"     # 月度记录命名：2026-October

# ===== 今天该跑什么（单一定时 + 脚本判断，从根上避免周/月复盘并发） =====
# MANUAL=true（workflow_dispatch 手动触发）→ 强制跑周复盘，方便随时补跑/调试
MANUAL = os.environ.get("MANUAL") == "true"
RUN_WEEKLY = True if MANUAL else (now.weekday() == 5)      # 周六
RUN_MONTHLY = False if MANUAL else (now.day == 1)          # 每月 1 号
if not (RUN_WEEKLY or RUN_MONTHLY):
    print(f"[跳过] 北京时间 {now:%Y-%m-%d %H:%M} —— 既非周六、也非 1 号，今天没有复盘任务")
    raise SystemExit(0)
print(f"[模式] 北京 {now:%Y-%m-%d %H:%M} | 周复盘:{'跑' if RUN_WEEKLY else '—'}"
      f" | 月度复盘:{'跑' if RUN_MONTHLY else '—'}")

# ===== 1. KR（状态指标：信心指数 + 与上周的变化） =====
# 只列「需要关注」的（信心 ≤ CONF_FOCUS 或未评），避免 13 条摊平、稀释焦点。
# 汇报用「信心指数(上周)」里存的上次值做对比；汇报结束后再把当前值刷新进去（见 refresh_kr_snapshot）。
CONF_FOCUS = 5
krs = query(KR_DB)
n_kr_active = 0
kr_all = []            # (name, status, conf, conf_prev)
kr_snapshot = []       # (page_id, conf) —— 待刷新的 KR


def _num(v):
    if v is None:
        return "—"
    f = float(v)
    return str(int(f)) if f == int(f) else str(round(f, 2))


for _r in krs:
    _p = _r.get("properties", {})
    _st = g(_p, "状态") or ""
    if _st not in ("未开始", "进行中"):
        continue
    n_kr_active += 1
    _conf = g(_p, "信心指数")
    _prev = g(_p, "信心指数(上周)")
    kr_all.append((g(_p, "关键成果") or "", _st, _conf, _prev))
    kr_snapshot.append((_r["id"], _conf))

kr_focus = [k for k in kr_all if k[2] is None or k[2] <= CONF_FOCUS]
kr_focus.sort(key=lambda k: (0 if k[2] is None else 1, k[2] if k[2] is not None else 0))

kr_lines = []
for nm, st, conf, prev in kr_focus:
    if conf is None:
        mark = "⚠️ 未评信心"
    elif prev is None:
        mark = f"信心 {_num(conf)}/10（首次记录）"
    else:
        d = round(conf - prev, 2)
        arrow = "↑" if d > 0 else "↓" if d < 0 else "→"
        mark = f"信心 {_num(conf)}/10（上周 {_num(prev)}，{arrow}{_num(abs(d))}）"
    kr_lines.append(f"• {nm}\n　　{st} · {mark}")
_kr_more = len(kr_all) - len(kr_focus)
if _kr_more > 0:
    kr_lines.append(f"…（另有 {_kr_more} 条信心 >{CONF_FOCUS}，月度复盘时看）")
kr_text = "\n".join(kr_lines) or "（无进行中/未开始的 KR）"

# 下次复盘日 = 下一个周六
_days_ahead = (5 - now.weekday()) % 7 or 7
NEXT_REVIEW = (now + timedelta(days=_days_ahead)).strftime("%Y-%m-%d")

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
        if (g(p, "复盘周期") or "") == week:
            continue
        raw = g(p, "本周三件") or ""
        names = [re.sub(r"^[①②③④⑤⑥⑦⑧⑨⑩]+\s*", "", ln.strip()) for ln in raw.split("\n") if ln.strip()]
        names = [re.sub(r"^\d+[\.、]\s*", "", n) for n in names]
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


# ===== 3.5 卡点：谁连续几周没动（零新增字段，从历史复盘记录推断） =====
def _status_map(txt):
    """把「上周三件推进情况」解析成 {任务名: 状态描述}。"""
    out = {}
    for ln in (txt or "").split("\n"):
        ln = ln.strip()
        if not ln or "→" not in ln:
            continue
        name, _, st = ln.partition("→")
        name = re.sub(r"^[①②③④⑤⑥⑦⑧⑨⑩]+\s*", "", name)
        name = re.sub(r"^\d+[\.、]\s*", "", name).strip()
        if name:
            out[name] = st.strip()
    return out


def stalled_tasks(min_weeks=2):
    """数「连续几周都标未动」的任务（不需要任何新字段）。

    数据源：「复盘记录数据库」历史行的「上周三件推进情况」——
    每行记录的是"上一周的三件，在那一周结束时到哪一步"。
    从最新一条往前连乘：某周出现"动了"或该任务没进三件 → 链条中断。
    """
    def _d(r):
        v = r.get("properties", {}).get("复盘日期", {}).get("date")
        return (v or {}).get("start", "")

    streaks = {}
    for r in sorted(query(SUMMARY_DB), key=_d, reverse=True):
        st = _status_map(g(r.get("properties", {}), "上周三件推进情况"))
        if not st:
            continue
        touched = set()
        for nm, desc in st.items():
            if "未动" in desc:
                streaks[nm] = streaks.get(nm, 0) + 1
                touched.add(nm)
        for nm in list(streaks):
            if nm not in touched:
                del streaks[nm]        # 动了 / 没进三件 → 链条中断
    return sorted([(n, c) for n, c in streaks.items() if c >= min_weeks],
                  key=lambda x: -x[1])


_stalled = stalled_tasks(2)
if _stalled:
    stall_text = "\n".join(f"• 《{n}》 —— 连续 {c} 周未推进" for n, c in _stalled[:8])
else:
    stall_text = "（暂无 —— 该统计从 2026-W41 开始积累，需几周后才有内容）"


# ===== 3.6 「本期聚焦」：读同步块里上次写的那句（周六读到的即上周写的） =====
def read_focus_from_page():
    for b in page_children(WEEKLY_PAGE):
        if b.get("type") != "synced_block":
            continue
        seen, buf = False, []
        for k in page_children(b["id"]):
            s = block_text(k).strip()
            if k.get("type", "").startswith("heading") and "聚焦" in s:
                seen = True
                continue
            if seen and s:
                buf.append(s)
        if seen:
            txt = " ".join(buf).strip()
            if txt.startswith("（一句话") or txt.startswith("(一句话"):
                return ""            # 还是占位符 → 视为没填
            return txt
    return ""


FOCUS_LAST = read_focus_from_page()

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

# ===== 6. 未来 4 周（P2 标记） =====
# P1/P2 字段设定源自《OKR工作法》周报：
#   P1 = 接下来一周的三件重要任务   P2 = 未来四周应该办的重要任务
# 与「2026年完成率120%」页面的视图保持同源（该视图 quick_filters：推进型 + P1/P2=P2 + 状态待办/进行中）


def _due(t):
    return g(t.get("properties", {}), "截止时间") or ""


future = sorted([t for t in active
                 if g(t.get("properties", {}), "P1/P2") == "P2"
                 and g(t.get("properties", {}), "任务性质") == "推进型"],
                key=lambda t: (_due(t) or "9999"))
if future:
    p2_text = "\n".join(f"• {tname(t.get('properties', {}))[:36]}" for t in future[:10])
    if len(future) > 10:
        p2_text += f"\n…（另有 {len(future) - 10} 件未列）"
else:
    p2_text = "（暂无 P2 标记的任务 —— 在任务库里把「未来 4 周要办」的推进型任务标为 P2）"

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

🕸 卡点（连续未推进 · 从历史复盘记录推断）
{stall_text}

──────────────────

② 🚀 本周推进三件
【候选 · 每个已带"下一步动作"】（从中圈 3 个，在任务库里标为 P1）
{cand_text}

【本周已选】（P1 任务）
{this_three_text}{missing_note}

──────────────────

③ 📊 状态指标（只列需关注的 KR · 含与上周对比）
{kr_text}

──────────────────

④ 📅 未来 4 周（P2 标记 · 未来四周要办的重要任务）
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
    """把自动区写入页面。

    结构约定：[说明 callout] [自动区（若干 callout）] [divider] [手写区…]
    清理规则：删掉「说明 callout」之后、第一个 divider 之前的**所有**块。
    这样无论自动区被切成几块、之前是否残留过重复块，都能一次清干净；手写区永不受影响。
    """
    page = page or WEEKLY_PAGE
    blocks = page_children(page)
    anchor, anchor_i = None, None
    for i, b in enumerate(blocks):
        if b.get("type") != "callout":
            continue
        s = block_text(b)
        if "Workbuddy" in s and not s.startswith(CALL_MARK):
            anchor, anchor_i = b["id"], i     # 说明 callout（永不删）
            break
    to_delete = []
    if anchor_i is not None:
        for b in blocks[anchor_i + 1:]:
            if b.get("type") == "divider":
                break                          # divider 之后是手写区，停
            to_delete.append(b["id"])
    for bid in to_delete:
        try:
            requests.delete(f"https://api.notion.com/v1/blocks/{bid}", headers=H, timeout=15)
        except Exception:
            pass
    if to_delete:
        time.sleep(0.5)                        # 等删除生效，避免与插入竞态产生重复块
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

    last_status = "\n".join(f"{_c(i)} {nm} → {locate(nm)}" for i, nm in enumerate(last_names)) if last_names else ""
    this_line = "\n".join(f"{_c(i)} {tname(t.get('properties', {}))}" for i, t in enumerate(this_three)) if this_three else ""
    return {
        "复盘周期": {"title": [{"text": {"content": week}}]},
        "复盘日期": {"date": {"start": today}},
        "近4周兑现率": {"number": r4 / 100},
        "近4周完成数": {"number": len(w4_ok)},
        "本周完成数": {"number": len(wk_ok)},
        "活跃待办数": {"number": n_active},
        "推进型占比": {"number": push_pct},
        "关联目标": {"rich_text": [{"text": {"content": GOAL_NAME}}]},
        "关联项目": {"relation": [{"id": pid} for pid in valid_project_ids(GOAL_PROJECTS)]},
        "上周三件推进情况": {"rich_text": [{"text": {"content": trim(last_status)}}]},
        # 没填时写空数组（清空），避免空 content 触发写入失败
        "上周聚焦": {"rich_text": ([{"text": {"content": trim(FOCUS_LAST)}}] if FOCUS_LAST else [])},
        "本周三件": {"rich_text": [{"text": {"content": trim(this_line)}}]},
    }


def write_row_body(page_id, text):
    """把完整正文写进该行页面内（幂等：先清空该行页面所有块，再写入）。

    行页面是本脚本独占的，全清再写可避免历史残留造成重复。
    """
    blocks = page_children(page_id)
    for b in blocks:
        try:
            requests.delete(f"https://api.notion.com/v1/blocks/{b['id']}", headers=H, timeout=15)
        except Exception:
            pass
    if blocks:
        time.sleep(0.5)
    children = _text_to_callouts(text, "📄", prefix=f"【Workbuddy 自动正文 · {week}】\n\n")
    r = requests.patch(f"https://api.notion.com/v1/blocks/{page_id}/children",
                       headers=H, json={"children": children}, timeout=20)
    print("行内自动正文:", r.status_code,
          r.json().get("message", "") if r.status_code != 200 else f"OK({len(children)}块)")


def _upsert(props):
    """按「复盘周期」upsert，返回 (response, row_page_id, was_update)。"""
    existing = query(SUMMARY_DB, {"filter": {"property": "复盘周期", "title": {"equals": week}}})
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


def refresh_kr_snapshot():
    """把「信心指数」当前值存进「信心指数(上周)」，并把「下次检查日期」刷新为下次复盘日。

    两条纪律（否则「上周值」会被自己覆盖，对比永远为 0）：
      1. 必须在**报告写完之后**调用 —— 报告要用「上周」里的旧值做对比；
      2. 只在**本周第一次运行**时刷新 —— 一周内重复运行会跳过。
    刷新后，本次的"当前信心"就成为下周的"上周信心"。
    """
    if not kr_snapshot:
        return
    ok = 0
    for rid, conf in kr_snapshot:
        props = {"下次检查日期": {"date": {"start": NEXT_REVIEW}}}
        if conf is not None:
            props["信心指数(上周)"] = {"number": conf}
        try:
            r = requests.patch(f"https://api.notion.com/v1/pages/{rid}", headers=H,
                               json={"properties": props}, timeout=20)
            if r.status_code == 200:
                ok += 1
        except Exception:
            pass
    print(f"KR 信心快照刷新: {ok}/{len(kr_snapshot)} | 下次检查日期 → {NEXT_REVIEW}")


if __name__ == "__main__":
    if RUN_WEEKLY:
        print(f"[周复盘] 周期:{week} | KR进行中:{n_kr_active} | 近4周:{len(w4_ok)}/{len(w4_tot)}={r4}%"
              f"(前4周{r4_prev}%) | 本周完成:{len(wk_ok)} | 活跃:{n_active} | 停放:{n_park} | "
              f"候选:{len(cand)} | P1三件:{len(this_three)} | 已填下一步动作:{n_action_filled} | "
              f"上周三件识别:{len(last_names)}")
        print("=" * 50)
        print(content)
        print("=" * 50)
        if WRITE:
            # 本周是否已有记录 —— 决定要不要刷新 KR 快照（见下）
            _week_row_exists = bool(query(SUMMARY_DB, {
                "filter": {"property": "复盘周期", "title": {"equals": week}}}))
            write_to_page(content)
            write_summary_row()
            if _week_row_exists:
                print(f"KR 信心快照：本周（{week}）已刷新过，跳过 —— 避免把本周值误当上周值")
            else:
                refresh_kr_snapshot()
        else:
            print("【本地干跑，未写入 Notion。GitHub Actions 运行时会自动写入】")
            print("复盘记录数据库待写入行:", week)

    if RUN_MONTHLY:
        # ===== 月度复盘（模板待设计，A1 已搁置）=====
        # 命名规范已定：记录名 = month_label（如 2026-October），与周复盘 2026-W43 同库共存。
        # 模板确定后在此接入：读「回顾纠偏」页面 + 写「复盘记录数据库」月度行。
        print(f"[月度复盘] 周期:{month_label} —— 模板尚未实现，本次不写入任何内容")
