import os
import re
import sqlite3
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from PIL import Image, ImageOps

import cloud_store
import course_management as cm
import user_management as um

# ==================== 配置 ====================
PASSWORD = os.environ.get("SCORE_TRACKER_PASSWORD", "")
DAY_START, DAY_END = "08:00", "20:30"   # 课程表/空闲时间统计的工作时段
SUBJECTS = ["数学", "物理", "其他"]
REASONS = ["计算错误", "概念不清", "审题失误", "方法不会", "没时间", "粗心抄错", "其他"]
WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

BASE = Path(__file__).parent
st.set_page_config(page_title="教务工作台", page_icon="📒", layout="wide")
cloud_store.configure_from_streamlit()
DATA = cloud_store.get_data_dir(BASE / "data")
PHOTOS = DATA / "photos"
DB = DATA / "scores.db"
PHOTOS.mkdir(parents=True, exist_ok=True)
try:
    database_found = cloud_store.prepare_database(DB)
except Exception as exc:
    st.error(f"无法读取云端数据库：{exc}")
    st.stop()
if cloud_store.is_cloud() and not database_found:
    st.error("云端数据库尚未初始化。请先把本地 data/scores.db 上传到 Supabase Storage 的 database/scores.db。")
    st.stop()
st.markdown("""
<style>
#MainMenu, footer {visibility: hidden;}
.st-key-bottomnav {position: fixed; left: 0; right: 0; bottom: 0; z-index: 1000;
  background: #FFFFFF; border-top: 1px solid #E1E6EE;
  padding: 6px 8px calc(8px + env(safe-area-inset-bottom));}
[class*="st-key-me_quick"] [data-testid="stHorizontalBlock"],
[class*="st-key-me_month"] [data-testid="stHorizontalBlock"] {flex-wrap: nowrap !important; width: 100% !important;}
[class*="st-key-me_quick"] [data-testid="stColumn"],
[class*="st-key-me_month"] [data-testid="stColumn"] {min-width: 0 !important; flex: 1 1 0 !important; width: auto !important;}
[class*="st-key-me_quick"] .stElementContainer,
[class*="st-key-me_month"] .stElementContainer,
[class*="st-key-me_quick"] button,
[class*="st-key-me_month"] button {width: 100% !important; min-width: 0 !important;}
.st-key-bottomnav .stElementContainer,
.st-key-bottomnav [data-testid="stButtonGroup"],
.st-key-bottomnav [role="radiogroup"] {width: 100% !important;}
.st-key-bottomnav [role="radiogroup"] {display: flex !important; gap: 4px !important;}
.st-key-bottomnav button {flex: 1 1 0 !important; min-width: 0 !important;
  font-size: .8rem !important; padding: 7px 2px !important; justify-content: center !important;}
.block-container {padding-top: 2rem; max-width: 1100px;}
.stButton > button {border-radius: 10px; font-weight: 600; border: 1px solid #D5DCE8;}
[data-testid="stExpander"] {border-radius: 12px; border: 1px solid #E1E6EE; background: #FFF;}
[data-testid="stMetric"] {background:#FFF; border:1px solid #E1E6EE; border-radius:12px; padding:12px 16px;}
[data-testid="stImage"] img {border-radius: 10px; border: 1px solid #E1E6EE;}
table.wk {width:100%; border-collapse:collapse; table-layout:fixed; font-size:12px;}
table.wk th {padding:6px 2px; background:#EAEFF7; font-weight:600;}
table.wk th.today {background:#3B6EF5; color:#fff;}
table.wk td {height:19px; padding:0 4px; border:1px solid #fff; overflow:hidden; white-space:nowrap;}
table.wk td.tm {width:46px; text-align:right; color:#8A94A6; border:none; background:none;}
table.wk td.free {background:#E4F5EC;}
table.wk td.busy {background:#3B6EF5; color:#fff; font-weight:600;}
table.wk td.done {background:#7C93C9; color:#fff; font-weight:600;}
@media (max-width: 640px) {.stButton > button {width:100%;}}
</style>
""", unsafe_allow_html=True)


# 把「添加到主屏幕」需要的 meta / manifest 注入到页面 <head>（iPhone、安卓通用）
_PWA_SCRIPT = """
<script>
(function () {
  var top = window;
  for (var i = 0; i < 6; i++) {
    try {
      if (top.parent && top.parent !== top && top.parent.document) { top = top.parent; } else { break; }
    } catch (e) { break; }
  }
  var doc = top.document;
  var head = doc.head;
  if (!head) { return; }
  var origin = top.location.origin;
  function meta(name, content) {
    if (!head.querySelector('meta[name="' + name + '"]')) {
      var m = doc.createElement("meta");
      m.setAttribute("name", name);
      m.setAttribute("content", content);
      head.appendChild(m);
    }
  }
  meta("apple-mobile-web-app-capable", "yes");
  meta("mobile-web-app-capable", "yes");
  meta("apple-mobile-web-app-status-bar-style", "default");
  meta("apple-mobile-web-app-title", "\u6559\u52a1\u5de5\u4f5c\u53f0");
  meta("application-name", "\u6559\u52a1\u5de5\u4f5c\u53f0");
  meta("theme-color", "#3B6EF5");
  function link(rel, href, sizes) {
    var old = head.querySelector('link[rel="' + rel + '"]');
    if (old) { old.setAttribute("href", href); return; }
    var l = doc.createElement("link");
    l.setAttribute("rel", rel);
    l.setAttribute("href", href);
    if (sizes) { l.setAttribute("sizes", sizes); }
    head.appendChild(l);
  }
  var bases = [origin + "/~/+/app/static/", origin + "/app/static/"];
  var idx = 0;
  function probe() {
    if (idx >= bases.length) { return; }
    var base = bases[idx++];
    fetch(base + "manifest.json", { cache: "no-store" })
      .then(function (r) {
        var ct = (r.headers.get("content-type") || "").toLowerCase();
        if (r.ok && ct.indexOf("json") >= 0) {
          link("apple-touch-icon", base + "icon-180.png");
          link("icon", base + "icon-192.png", "192x192");
          link("manifest", base + "manifest.json");
        } else { probe(); }
      })
      .catch(function () { probe(); });
  }
  probe();
})();
</script>
"""

components.html(_PWA_SCRIPT, height=0)


# ==================== 数据库 ====================
def _clean(params):
    return [p.item() if hasattr(p, "item") else p for p in params]


def run(sql, params=()):
    conn = sqlite3.connect(DB)
    try:
        cur = conn.execute(sql, _clean(params))
        conn.commit()
        cloud_store.save_database(DB)
        return cur.lastrowid
    finally:
        conn.close()


def query(sql, params=()):
    conn = sqlite3.connect(DB)
    try:
        return pd.read_sql_query(sql, conn, params=_clean(params))
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS students(id INTEGER PRIMARY KEY, name TEXT NOT NULL, class_name TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS classes(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, grade TEXT DEFAULT '',
    subject TEXT DEFAULT '数学', total_hours REAL DEFAULT 0, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS class_members(class_id INTEGER, student_id INTEGER, UNIQUE(class_id, student_id));
CREATE TABLE IF NOT EXISTS schedule(id INTEGER PRIMARY KEY, class_id INTEGER, weekday INTEGER, start TEXT, end TEXT);
CREATE TABLE IF NOT EXISTS lessons(id INTEGER PRIMARY KEY, class_id INTEGER, lesson_date TEXT, start TEXT DEFAULT '',
    end TEXT DEFAULT '', hours REAL DEFAULT 0, content TEXT DEFAULT '', mastery TEXT DEFAULT '',
    homework TEXT DEFAULT '', note TEXT DEFAULT '', UNIQUE(class_id, lesson_date, start));
CREATE TABLE IF NOT EXISTS exams(id INTEGER PRIMARY KEY, name TEXT NOT NULL, exam_date TEXT, subject TEXT,
    full_score REAL DEFAULT 100);
CREATE TABLE IF NOT EXISTS scores(id INTEGER PRIMARY KEY, student_id INTEGER, exam_id INTEGER, score REAL,
    wrong_qs TEXT DEFAULT '', reasons TEXT DEFAULT '', knowledge TEXT DEFAULT '', paper TEXT DEFAULT '',
    sheet TEXT DEFAULT '', note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS plans(id INTEGER PRIMARY KEY, class_id INTEGER, plan_date TEXT, start TEXT, end TEXT,
    location TEXT DEFAULT '', status TEXT DEFAULT '待上课', note TEXT DEFAULT '', moved_from INTEGER);
"""
VIEW = """
DROP VIEW IF EXISTS v_lessons;
CREATE VIEW v_lessons AS
    SELECT l.*, c.name AS cname, c.subject, c.kind,
           ROW_NUMBER() OVER (PARTITION BY l.class_id ORDER BY l.lesson_date, l.start) AS seq
    FROM lessons l JOIN classes c ON c.id = l.class_id;
"""
ADD_COLS = {
    "students": {"grade": "TEXT DEFAULT ''", "contact": "TEXT DEFAULT ''",
                 "status": "TEXT DEFAULT '在读'", "note": "TEXT DEFAULT ''"},
    "exams": {"class_id": "INTEGER"},
    "classes": {"kind": "TEXT DEFAULT '班课'"},
    "lessons": {"plan_id": "INTEGER"},
}


def init_db():
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    for table, cols in ADD_COLS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    # 一次性迁移:旧版学生表里的"班级"文字 -> 班级表 + 班级名单
    if conn.execute("PRAGMA user_version").fetchone()[0] < 1:
        conn.execute("INSERT OR IGNORE INTO classes(name) SELECT DISTINCT class_name FROM students WHERE class_name!=''")
        conn.execute("""INSERT OR IGNORE INTO class_members(class_id, student_id)
                        SELECT c.id, s.id FROM students s JOIN classes c ON c.name = s.class_name
                        WHERE s.class_name != ''""")
        conn.execute("PRAGMA user_version = 1")
    # 迁移到"计划课程/实际授课分离":旧的每周固定时间 -> 从今天起未来8周的具体课程;课时改为按"节"计
    if conn.execute("PRAGMA user_version").fetchone()[0] < 2:
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        for cid, wd, s, e in conn.execute("SELECT class_id, weekday, start, end FROM schedule").fetchall():
            for w in range(8):
                d = monday + timedelta(weeks=w, days=wd)
                if d >= today:
                    conn.execute("INSERT INTO plans(class_id, plan_date, start, end) VALUES(?,?,?,?)", (cid, str(d), s, e))
        conn.execute("""UPDATE plans SET status='已上课' WHERE EXISTS (SELECT 1 FROM lessons l WHERE
                        l.class_id=plans.class_id AND l.lesson_date=plans.plan_date AND l.start=plans.start)""")
        conn.execute("UPDATE lessons SET plan_id=(SELECT p.id FROM plans p WHERE p.class_id=lessons.class_id AND p.plan_date=lessons.lesson_date AND p.start=lessons.start)")
        conn.execute("UPDATE lessons SET hours = 1")
        conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()
    cloud_store.save_database(DB)


# ==================== 工具函数 ====================
def mins(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def hm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def week_start(d):
    return d - timedelta(days=d.weekday())


def save_photos(files, tag):
    names = []
    for f in files or []:
        suffix = Path(getattr(f, "name", "upload")).suffix.lower()
        if suffix == ".pdf":
            name = f"{tag}_{uuid.uuid4().hex[:8]}.pdf"
            target = PHOTOS / name
            target.write_bytes(f.getbuffer())
        else:
            img = ImageOps.exif_transpose(Image.open(f)).convert("RGB")
            img.thumbnail((2000, 2000))
            name = f"{tag}_{uuid.uuid4().hex[:8]}.jpg"
            target = PHOTOS / name
            img.save(target, quality=80, optimize=True)
        cloud_store.upload_photo(target, name)
        names.append(name)
    return "|".join(names)


def show_photos(joined, caption):
    for n in [x for x in (joined or "").split("|") if x]:
        path = cloud_store.ensure_local_photo(n, PHOTOS)
        if not path.exists():
            continue
        if path.suffix.lower() == ".pdf":
            st.download_button(
                f"下载{caption}：{n}",
                data=path.read_bytes(),
                file_name=n,
                mime="application/pdf",
                key=f"pdf_{caption}_{n}",
                use_container_width=True,
            )
        else:
            st.image(str(path), caption=caption, use_container_width=True)


def labels(df, fmt):
    return {r["id"]: fmt(r) for _, r in df.iterrows()}


STATUS_ICON = {"待上课": "🟦", "已上课": "✅", "请假": "🟨", "取消": "⬛", "调课": "↪️"}
ACTIVE = ("待上课", "已上课")
PLAN_SQL = "SELECT p.*, c.name cname, c.subject, c.kind FROM plans p JOIN classes c ON c.id=p.class_id"


def class_stats():
    return query("""SELECT c.*, COALESCE(u.h,0) used, COALESCE(u.n,0) n, c.total_hours - COALESCE(u.h,0) remain
                    FROM classes c LEFT JOIN (SELECT class_id, SUM(hours) h, COUNT(*) n FROM lessons GROUP BY class_id) u
                    ON u.class_id = c.id ORDER BY c.name""")


def d2s(d):
    return datetime.strptime(d, "%Y-%m-%d").date()


def now_min():
    n = datetime.now()
    return n.hour * 60 + n.minute


def plans_between(d1, d2):
    return query(PLAN_SQL + " WHERE p.plan_date BETWEEN ? AND ? ORDER BY p.plan_date, p.start", (str(d1), str(d2)))


def plans_on(d):
    return plans_between(d, d)


def free_ranges(d, pl=None):
    """空闲 = 工作时段里没有「待上课/已上课」的部分(请假、取消、调课的课不占时间)"""
    pl = plans_on(d) if pl is None else pl
    busy = sorted((mins(r.start), mins(r.end)) for r in pl.itertuples() if r.status in ACTIVE)
    cur, out = mins(DAY_START), []
    for a, b in busy:
        if a > cur:
            out.append((cur, min(a, mins(DAY_END))))
        cur = max(cur, b)
    if cur < mins(DAY_END):
        out.append((cur, mins(DAY_END)))
    return [(a, b) for a, b in out if b - a >= 30]


def fmt_free(d, pl=None):
    return "、".join(f"{hm(a)}–{hm(b)}" for a, b in free_ranges(d, pl)) or "无"


def complete(cid, d, start, end, plan_id=None, content="", homework=""):
    """标记已上课:生成一条独立的实际授课记录(之后再改课程表不会影响它)"""
    run("""INSERT OR IGNORE INTO lessons(class_id, lesson_date, start, end, hours, plan_id, content, homework)
           VALUES(?,?,?,?,1,?,?,?)""", (int(cid), str(d), start, end, plan_id, content, homework))
    if plan_id is not None:
        run("UPDATE plans SET status='已上课' WHERE id=?", (int(plan_id),))


def set_status(pid, s):
    run("UPDATE plans SET status=? WHERE id=?", (s, int(pid)))


def plan_menu(p, key, box):
    with box.popover("更多"):
        k = f"{key}{p.id}"
        if p.status == "待上课":
            ct = st.text_input("本节内容(选填)", key=f"ct{k}")
            hw = st.text_input("作业(选填)", key=f"hw{k}")
            if st.button("✓ 已上课并保存内容", key=f"okc{k}"):
                complete(p.class_id, p.plan_date, p.start, p.end, p.id, ct, hw)
                st.rerun()
            x1, x2 = st.columns(2)
            if x1.button("请假", key=f"lv{k}"):
                set_status(p.id, "请假")
                st.rerun()
            if x2.button("取消", key=f"cn{k}"):
                set_status(p.id, "取消")
                st.rerun()
            st.markdown("**调课**(原来这节标记为「调课」,自动在新时间生成一节)")
            nd = st.date_input("改到", d2s(p.plan_date), key=f"nd{k}")
            y1, y2 = st.columns(2)
            ns = y1.time_input("开始", datetime.strptime(p.start, "%H:%M").time(), key=f"ns{k}")
            ne = y2.time_input("结束", datetime.strptime(p.end, "%H:%M").time(), key=f"ne{k}")
            if st.button("确认调课", key=f"mv{k}"):
                set_status(p.id, "调课")
                run("INSERT INTO plans(class_id, plan_date, start, end, location, moved_from) VALUES(?,?,?,?,?,?)",
                    (p.class_id, str(nd), ns.strftime("%H:%M"), ne.strftime("%H:%M"), p.location or "", p.id))
                st.rerun()
        elif p.status == "已上课":
            st.caption("误点了可以撤销(会同时删除这节课的教学记录)")
            if st.button("撤销已上课", key=f"un{k}"):
                run("DELETE FROM lessons WHERE plan_id=?", (int(p.id),))
                set_status(p.id, "待上课")
                st.rerun()
        elif p.status in ("请假", "取消"):
            if st.button("恢复为待上课", key=f"rs{k}"):
                set_status(p.id, "待上课")
                st.rerun()
        else:
            st.caption("这节已调到新的时间")


def plan_row(p, key):
    ended = p.status == "待上课" and p.plan_date == str(date.today()) and mins(p.end) <= now_min()
    c1, c2, c3 = st.columns([2, 5, 3])
    c1.markdown(f"**{p.start}–{p.end}**")
    tag = "👤一对一" if p.kind == "一对一" else "班课"
    c2.markdown(f"{STATUS_ICON[p.status]} **{p.cname}** · {tag} · {p.subject}"
                + (f" · 📍{p.location}" if p.location else "")
                + f"  \n<small>{p.status}{' · 已结束,记得点「已上课」' if ended else ''}</small>", unsafe_allow_html=True)
    if p.status == "待上课":
        b1, b2 = c3.columns([3, 2])
        if b1.button("✓ 已上课", key=f"ok{key}{p.id}", type="primary"):
            complete(p.class_id, p.plan_date, p.start, p.end, p.id)
            st.rerun()
        plan_menu(p, key, b2)
    else:
        plan_menu(p, key, c3)


def day_timeline(d, key):
    pl = plans_on(d)
    items = [(mins(r.start), 1, r) for r in pl.itertuples()] + [(a, 0, (a, b)) for a, b in free_ranges(d, pl)]
    for _, kind, x in sorted(items, key=lambda t: (t[0], t[1])):
        if kind == 0:
            st.markdown(f"<div style='padding:8px 12px;margin:4px 0;border-radius:10px;background:#E4F5EC;color:#2E7D5B'>"
                        f"⬜ <b>{hm(x[0])}–{hm(x[1])}</b>　空闲</div>", unsafe_allow_html=True)
        else:
            plan_row(x, key)


# ==================== 首页:我的课程表 ====================
def page_home():
    today = date.today()
    ws = week_start(today)
    st.title("我的课程表")
    st.caption(f"{today.year}年{today.month}月{today.day}日  {WD[today.weekday()]}")
    st.subheader("今天")
    day_timeline(today, "today")

    od = query(PLAN_SQL + " WHERE p.status='待上课' AND p.plan_date<? AND p.plan_date>=? ORDER BY p.plan_date, p.start",
               (str(today), str(today - timedelta(days=14))))
    if len(od):
        st.subheader("⚠️ 过去还没处理的课")
        for p in od.itertuples():
            st.caption(f"{p.plan_date[5:]} {WD[d2s(p.plan_date).weekday()]}")
            plan_row(p, "over")

    wp = plans_between(ws, ws + timedelta(6))
    week = [ws + timedelta(i) for i in range(7)]
    pend = int(query("SELECT COUNT(*) n FROM lessons WHERE content=''").n[0])
    month_n = int(query("SELECT COUNT(*) n FROM lessons WHERE substr(lesson_date,1,7)=?", (today.strftime("%Y-%m"),)).n[0])
    free_h = sum(b - a for d in week for a, b in free_ranges(d, wp[wp.plan_date == str(d)])) / 60
    st.divider()
    m = st.columns(4)
    m[0].metric("本月已授课(节)", month_n)
    m[1].metric("本周待上课(节)", int((wp.status == "待上课").sum()))
    m[2].metric("本周空闲(小时)", f"{free_h:.1f}")
    m[3].metric("待填教学记录", pend)
    if pend:
        st.button("去填教学记录 →", on_click=lambda: st.session_state.update(page="教学记录"))
    st.subheader("本周概览")
    st.dataframe(pd.DataFrame({
        "日期": [f"{WD[d.weekday()]} {d.month}/{d.day}" for d in week],
        "课程数": [int(wp[(wp.plan_date == str(d)) & wp.status.isin(ACTIVE)].shape[0]) for d in week],
        "空闲时段": [fmt_free(d, wp[wp.plan_date == str(d)]) for d in week],
    }), hide_index=True, use_container_width=True)


# ==================== 课程表(可直接编辑) ====================
def week_grid(ws):
    days = [ws + timedelta(i) for i in range(7)]
    wp = plans_between(ws, days[-1])
    cells = {}
    for r in wp[wp.status.isin(ACTIVE)].itertuples():
        i = (d2s(r.plan_date) - ws).days
        done = r.status == "已上课"
        first = mins(r.start) // 30 * 30
        for m in range(first, mins(r.end), 30):
            text = f"{'👤' if r.kind == '一对一' else ''}{r.cname} {r.start}{' ✓' if done else ''}" if m == first else "&nbsp;"
            cells[(i, m)] = ("done" if done else "busy", text)
    h = "<table class='wk'><tr><th style='width:46px'></th>"
    for i, d in enumerate(days):
        h += f"<th class='{'today' if d == date.today() else ''}'>{WD[i]}<br>{d.month}/{d.day}</th>"
    h += "</tr>"
    for m in range(mins(DAY_START), mins(DAY_END), 30):
        h += f"<tr><td class='tm'>{hm(m) if m % 60 == 0 else ''}</td>"
        for i in range(7):
            c = cells.get((i, m))
            h += f"<td class='{c[0]}'>{c[1]}</td>" if c else "<td class='free'></td>"
        h += "</tr>"
    return h + "</table>"


def plan_editor(d1, d2, key):
    """像表格一样直接改:改日期/时间/班级/地点、加行=新增、删行=删除。已上课的课不在这里改,保护历史。"""
    cl = query("SELECT id, name FROM classes ORDER BY name")
    if cl.empty:
        st.info("先到「班级」页创建班级或一对一学生")
        return
    ids = dict(zip(cl.name, cl.id))
    pl = query("""SELECT p.id, p.plan_date, p.start, p.end, c.name cname, p.location, p.status
                  FROM plans p JOIN classes c ON c.id=p.class_id WHERE p.plan_date BETWEEN ? AND ?
                  ORDER BY p.plan_date, p.start""", (str(d1), str(d2)))
    fixed = pl[pl.status == "已上课"]
    edit = pl[~pl.status.isin(["已上课", "调课"])]
    show = pd.DataFrame({"id": edit.id.values, "日期": [d2s(x) for x in edit.plan_date], "开始": edit.start.values,
                         "结束": edit.end.values, "班级/学生": edit.cname.values,
                         "地点": edit.location.fillna("").values, "状态": edit.status.values})
    ver = st.session_state.setdefault(f"ver_{key}", 0)
    ed = st.data_editor(show, key=f"pe_{key}_{d1}_{ver}", num_rows="dynamic", hide_index=True, use_container_width=True,
                        disabled=["状态"], column_config={
                            "id": None,
                            "日期": st.column_config.DateColumn(format="YYYY-MM-DD", required=True),
                            "开始": st.column_config.TextColumn(required=True, validate=r"^\d{1,2}:\d{2}$", help="如 14:00"),
                            "结束": st.column_config.TextColumn(required=True, validate=r"^\d{1,2}:\d{2}$", help="如 16:00"),
                            "班级/学生": st.column_config.SelectboxColumn(options=list(cl.name), required=True)})
    st.caption("直接改格子=调整课程;表格底部空行=新增;选中行按 Delete 键=删除。改完点保存。已上课的课受保护,要撤销请到日视图的「更多」。")
    if not st.button("💾 保存课程表修改", type="primary", key=f"sv_{key}_{d1}"):
        return
    rows, err = [], None
    for _, r in ed.iterrows():
        vals = [r["日期"], r["开始"], r["结束"], r["班级/学生"]]
        if all(pd.isna(v) for v in vals):
            continue
        if any(pd.isna(v) for v in vals):
            err = "有几行没填完整(日期、开始、结束、班级/学生都要填)"
            break
        try:
            s, e = mins(str(r["开始"])), mins(str(r["结束"]))
        except ValueError:
            err = "时间格式要像 14:00"
            break
        if not (0 <= s < e <= 24 * 60 - 1):
            err = "结束时间要晚于开始时间(且在当天内)"
            break
        rows.append(dict(id=None if pd.isna(r["id"]) else int(r["id"]), d=str(pd.to_datetime(r["日期"]).date()),
                         s=hm(s), e=hm(e), cid=int(ids[r["班级/学生"]]),
                         loc="" if pd.isna(r["地点"]) else str(r["地点"]),
                         active=r["状态"] not in ("请假", "取消")))
    if not err:
        occ = [(x["d"], x["s"], x["e"]) for x in rows if x["active"]] + [(r.plan_date, r.start, r.end) for r in fixed.itertuples()]
        last = {}
        for dd, s, e in sorted(occ):
            if dd in last and s < last[dd]:
                err = f"{dd} 有课程时间重叠(和 {s}–{e} 那节)"
                break
            last[dd] = max(last.get(dd, "00:00"), e)
    if err:
        st.error(err)
        return
    keep = {x["id"] for x in rows if x["id"] is not None}
    for pid in set(edit.id.astype(int)) - keep:
        run("DELETE FROM plans WHERE id=?", (pid,))
    for x in rows:
        if x["id"] is None:
            run("INSERT INTO plans(class_id, plan_date, start, end, location) VALUES(?,?,?,?,?)", (x["cid"], x["d"], x["s"], x["e"], x["loc"]))
        else:
            run("UPDATE plans SET class_id=?, plan_date=?, start=?, end=?, location=? WHERE id=?", (x["cid"], x["d"], x["s"], x["e"], x["loc"], x["id"]))
    st.session_state[f"ver_{key}"] = ver + 1
    st.rerun()


def copy_week(ws, n):
    src = query("SELECT * FROM plans WHERE plan_date BETWEEN ? AND ? AND status IN ('待上课','已上课')",
                (str(ws), str(ws + timedelta(6))))
    c = 0
    for k in range(1, n + 1):
        for r in src.itertuples():
            nd = str(d2s(r.plan_date) + timedelta(weeks=k))
            if query("SELECT 1 FROM plans WHERE class_id=? AND plan_date=? AND start=? AND status!='调课'",
                     (r.class_id, nd, r.start)).empty:
                run("INSERT INTO plans(class_id, plan_date, start, end, location) VALUES(?,?,?,?,?)",
                    (r.class_id, nd, r.start, r.end, r.location or ""))
                c += 1
    return c


def page_schedule():
    st.title("课程表")
    c1, c2 = st.columns([1, 1])
    view = c1.radio("视图", ["周视图", "日视图"], horizontal=True, label_visibility="collapsed")
    d = c2.date_input("日期", date.today(), label_visibility="collapsed")
    if view == "周视图":
        ws = week_start(d)
        we = ws + timedelta(6)
        st.caption(f"{ws.month}月{ws.day}日 – {we.month}月{we.day}日")
        st.markdown(week_grid(ws), unsafe_allow_html=True)
        st.caption("🟩 空闲　🟦 待上课　灰蓝 ✓ 已上课　👤 一对一　(请假/取消的课不占时间)")
        st.subheader("直接编辑本周课程")
        plan_editor(ws, we, "w")
        with st.expander("把这一周的课程复制到后面几周(提前排课表)"):
            n = st.number_input("复制到接下来几周", 1, 26, 4)
            if st.button("复制"):
                st.success(f"新增了 {copy_week(ws, int(n))} 节课")
    else:
        st.subheader(f"{d.month}月{d.day}日 {WD[d.weekday()]}")
        day_timeline(d, "day")
        st.subheader("编辑这一天")
        plan_editor(d, d, "d")


# ==================== 班级 / 一对一 ====================
KINDS = ["班课", "一对一"]


def class_month_summary(cid):
    p = query("SELECT substr(plan_date,1,7) m, status, COUNT(*) n FROM plans WHERE class_id=? GROUP BY 1,2", (int(cid),))
    l = query("SELECT substr(lesson_date,1,7) m, COUNT(*) n FROM lessons WHERE class_id=? GROUP BY 1", (int(cid),))
    out = []
    for m in sorted(set(p.m) | set(l.m), reverse=True):
        sp = dict(zip(p[p.m == m].status, p[p.m == m].n))
        out.append({"月份": m, "计划": int(sum(v for k, v in sp.items() if k != "调课")),
                    "实际完成": int(l[l.m == m].n.sum()), "请假": int(sp.get("请假", 0)),
                    "取消": int(sp.get("取消", 0)), "还没上": int(sp.get("待上课", 0))})
    return pd.DataFrame(out)


def page_classes():
    st.title("班级 / 一对一")
    stats = class_stats()
    with st.expander("➕ 新建班级或一对一学生", expanded=stats.empty):
        with st.form("new_class"):
            n = st.text_input("名称(如「初三数学1班」「王同学-数学」)")
            c1, c2, c3, c4 = st.columns(4)
            k = c1.selectbox("类型", KINDS)
            g = c2.text_input("年级")
            s = c3.selectbox("科目", SUBJECTS)
            t = c4.number_input("课时包总节数(仅一对一)", 0.0, 1000.0, 0.0, 1.0)
            if st.form_submit_button("创建") and n.strip():
                try:
                    run("INSERT INTO classes(name, grade, subject, total_hours, kind) VALUES(?,?,?,?,?)",
                        (n.strip(), g, s, t if k == "一对一" else 0, k))
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("名称已存在")
    if stats.empty:
        return
    lab = labels(stats, lambda r: f"{r['name']}({r['kind']})")
    cid = st.selectbox("选择", list(lab), format_func=lab.get)
    c = stats[stats.id == cid].iloc[0]
    last = query("SELECT MAX(lesson_date) d FROM lessons WHERE class_id=?", (int(cid),)).d[0]
    month_n = int(query("SELECT COUNT(*) n FROM lessons WHERE class_id=? AND substr(lesson_date,1,7)=?",
                        (int(cid), date.today().strftime("%Y-%m"))).n[0])
    m = st.columns(4)
    if c.kind == "一对一":
        m[0].metric("总课时", f"{c.total_hours:g}")
        m[1].metric("已上课", f"{c.used:g}")
        m[2].metric("剩余", f"{c.remain:g}")
        m[3].metric("最近一次", last or "—")
    else:
        m[0].metric("累计授课(节)", int(c.n))
        m[1].metric("本月授课(节)", month_n)
        m[2].metric("最近一次", last or "—")

    t1, t2 = st.tabs(["授课记录", "基本信息与名单"])
    with t1:
        ms = class_month_summary(cid)
        if ms.empty:
            st.info("还没有课。到「课程表」里排课,上完点「已上课」就会出现在这里")
        else:
            st.dataframe(ms, hide_index=True, use_container_width=True)
            sel = st.selectbox("查看月份", ["全部"] + ms["月份"].tolist())
            sql, ps = "SELECT * FROM v_lessons WHERE class_id=?", [int(cid)]
            if sel != "全部":
                sql += " AND substr(lesson_date,1,7)=?"
                ps.append(sel)
            ls = query(sql + " ORDER BY lesson_date, start", ps)
            for r in ls.itertuples():
                d = d2s(r.lesson_date)
                st.write(f"✓ **{d.month}月{d.day}日** {WD[d.weekday()]} · 第{r.seq}节 · {r.content or '(还没填内容)'}")
            st.success(f"{'全部' if sel == '全部' else sel} 实际完成:{len(ls)} 节")
    with t2:
        with st.form("edit_class"):
            n = st.text_input("名称", c["name"])
            c1, c2, c3, c4 = st.columns(4)
            k = c1.selectbox("类型", KINDS, index=KINDS.index(c.kind) if c.kind in KINDS else 0)
            g = c2.text_input("年级", c.grade or "")
            s = c3.selectbox("科目", SUBJECTS, index=SUBJECTS.index(c.subject) if c.subject in SUBJECTS else 0)
            t = c4.number_input("课时包总节数(仅一对一)", 0.0, 1000.0, float(c.total_hours), 1.0)
            if st.form_submit_button("保存基本信息"):
                run("UPDATE classes SET name=?, grade=?, subject=?, total_hours=?, kind=? WHERE id=?",
                    (n.strip(), g, s, t if k == "一对一" else 0, k, int(cid)))
                st.rerun()
        stu = query("SELECT id, name, grade, status FROM students ORDER BY status, name")
        cur = query("SELECT student_id FROM class_members WHERE class_id=?", (int(cid),)).student_id.tolist()
        slab = labels(stu, lambda r: f"{r['name']} {r['grade'] or ''}" + ("" if r["status"] == "在读" else "(停课)"))
        chosen = st.multiselect("学生名单", list(slab), default=cur, format_func=slab.get)
        if st.button("保存名单"):
            run("DELETE FROM class_members WHERE class_id=?", (int(cid),))
            for sid in chosen:
                run("INSERT OR IGNORE INTO class_members VALUES(?,?)", (int(cid), int(sid)))
            st.rerun()


# ==================== 授课统计 ====================
def page_stats():
    st.title("授课统计")
    cur = date.today().strftime("%Y-%m")
    months = sorted(set(query("SELECT DISTINCT substr(lesson_date,1,7) m FROM lessons").m.tolist() + [cur]), reverse=True)
    cls = query("SELECT id, name FROM classes ORDER BY name")
    stu = query("SELECT id, name FROM students ORDER BY name")
    f = st.columns(4)
    mo = f[0].selectbox("月份", ["全部"] + months, index=months.index(cur) + 1)
    fc = f[1].selectbox("班级/一对一", [0] + cls.id.tolist(), format_func=lambda i: "全部" if i == 0 else dict(zip(cls.id, cls.name))[i])
    fs = f[2].selectbox("学生", [0] + stu.id.tolist(), format_func=lambda i: "全部" if i == 0 else dict(zip(stu.id, stu.name))[i])
    fj = f[3].selectbox("科目", ["全部"] + SUBJECTS)
    sql, ps = "SELECT * FROM v_lessons WHERE 1=1", []
    if mo != "全部":
        sql += " AND substr(lesson_date,1,7)=?"; ps.append(mo)
    if fc:
        sql += " AND class_id=?"; ps.append(fc)
    if fs:
        sql += " AND class_id IN (SELECT class_id FROM class_members WHERE student_id=?)"; ps.append(fs)
    if fj != "全部":
        sql += " AND subject=?"; ps.append(fj)
    df = query(sql, ps)
    m = st.columns(3)
    m[0].metric("班课实际授课(节)", int((df.kind == "班课").sum()))
    m[1].metric("一对一实际授课(节)", int((df.kind == "一对一").sum()))
    m[2].metric("总授课(节)", len(df))
    if df.empty:
        st.info("这个条件下没有授课记录")
    else:
        by = df.groupby(["cname", "kind", "subject"]).size().reset_index(name="节数").sort_values("节数", ascending=False)
        st.dataframe(by.rename(columns={"cname": "班级/学生", "kind": "类型", "subject": "科目"}), hide_index=True, use_container_width=True)
        st.bar_chart(by.set_index("cname")["节数"])
    pk = class_stats()
    pk = pk[pk.kind == "一对一"]
    if len(pk):
        st.subheader("一对一课时包")
        st.dataframe(pk[["name", "total_hours", "used", "remain"]].rename(
            columns={"name": "学生", "total_hours": "总课时", "used": "已上课", "remain": "剩余"}),
            hide_index=True, use_container_width=True)


# ==================== 学生 ====================
def student_report(sid):
    df = query("""SELECT sc.*, e.name exam_name, e.exam_date, e.subject, e.full_score
                  FROM scores sc JOIN exams e ON sc.exam_id=e.id WHERE sc.student_id=? ORDER BY e.exam_date""", (int(sid),))
    if df.empty:
        st.info("这个学生还没有考试成绩")
        return
    sub = st.radio("科目", ["全部"] + sorted(df.subject.dropna().unique().tolist()), horizontal=True, key=f"rp{sid}")
    if sub != "全部":
        df = df[df.subject == sub]
    df["得分率%"] = (df.score / df.full_score * 100).round(1)
    df["较上次"] = df["得分率%"].diff().round(1)
    st.line_chart(df.set_index("exam_date")["得分率%"])
    st.dataframe(df[["exam_date", "exam_name", "subject", "score", "full_score", "得分率%", "较上次"]].rename(
        columns={"exam_date": "日期", "exam_name": "考试", "subject": "科目", "score": "得分", "full_score": "满分"}),
        hide_index=True, use_container_width=True)
    r = df.reasons.fillna("").str.split(",").explode().str.strip()
    r = r[r != ""]
    if not r.empty:
        st.caption("错因统计")
        st.bar_chart(r.value_counts())
    k = df.knowledge.fillna("").apply(lambda x: re.split(r"[,,、;;\s]+", x)).explode()
    k = k[k.notna() & (k != "")]
    if not k.empty:
        st.caption("高频薄弱知识点")
        st.dataframe(k.value_counts().rename("次数"), use_container_width=True)
    for _, row in df.sort_values("exam_date", ascending=False).iterrows():
        with st.expander(f"{row.exam_date} {row.exam_name} — {row.score:g}/{row.full_score:g}"):
            st.write(f"**错题**:{row.wrong_qs or '—'}　**错因**:{row.reasons or '—'}")
            st.write(f"**知识点**:{row.knowledge or '—'}")
            if row.note:
                st.write(f"**备注**:{row.note}")
            show_photos(row.paper, "试卷")
            show_photos(row.sheet, "答题卡")


def page_students():
    st.title("学生")
    cls = query("SELECT id, name FROM classes ORDER BY name")
    with st.expander("➕ 新增 / 批量导入学生"):
        with st.form("new_stu"):
            names = st.text_area("姓名(每行一个,可一次导入整班)", height=100)
            c1, c2 = st.columns(2)
            grade = c1.text_input("年级")
            contact = c2.text_input("联系方式(只导入一个学生时填)")
            join = st.multiselect("加入班级", cls.id.tolist(), format_func=dict(zip(cls.id, cls.name)).get)
            if st.form_submit_button("添加"):
                lst = [x.strip() for x in names.splitlines() if x.strip()]
                for n in lst:
                    sid = run("INSERT INTO students(name, grade, contact) VALUES(?,?,?)", (n, grade, contact))
                    for c in join:
                        run("INSERT OR IGNORE INTO class_members VALUES(?,?)", (int(c), sid))
                st.success(f"添加了 {len(lst)} 人")
    flt = st.radio("状态", ["在读", "停课", "全部"], horizontal=True)
    df = query("""SELECT s.id, s.name 姓名, s.grade 年级, s.status 状态, s.contact 联系方式,
                  (SELECT GROUP_CONCAT(c.name, '、') FROM class_members m JOIN classes c ON c.id=m.class_id
                   WHERE m.student_id=s.id) 班级 FROM students s ORDER BY s.name""")
    if flt != "全部":
        df = df[df.状态 == flt]
    st.dataframe(df.drop(columns="id"), hide_index=True, use_container_width=True)
    if df.empty:
        return
    sid = st.selectbox("查看学生详情", df.id.tolist(), format_func=dict(zip(df.id, df.姓名)).get)
    s = query("SELECT * FROM students WHERE id=?", (int(sid),)).iloc[0]
    t1, t2, t3 = st.tabs(["资料", "上课记录", "考试成绩"])
    with t1:
        with st.form("edit_stu"):
            c1, c2, c3 = st.columns(3)
            n = c1.text_input("姓名", s["name"])
            g = c2.text_input("年级", s.grade or "")
            stt = c3.selectbox("状态", ["在读", "停课"], index=0 if s.status != "停课" else 1)
            ct = st.text_input("联系方式", s.contact or "")
            nt = st.text_area("备注", s.note or "", height=80)
            if st.form_submit_button("保存"):
                run("UPDATE students SET name=?, grade=?, status=?, contact=?, note=? WHERE id=?",
                    (n.strip(), g, stt, ct, nt, int(sid)))
                st.rerun()
    with t2:
        lz = query("""SELECT lesson_date 日期, cname 班级, seq 第几节, hours 课时, content 内容, mastery 掌握情况, homework 作业
                      FROM v_lessons WHERE class_id IN (SELECT class_id FROM class_members WHERE student_id=?)
                      ORDER BY lesson_date DESC LIMIT 50""", (int(sid),))
        st.dataframe(lz, hide_index=True, use_container_width=True)
    with t3:
        student_report(sid)


# ==================== 教学记录 ====================
def page_lessons():
    st.title("教学记录")
    cls = query("SELECT id, name FROM classes ORDER BY name")
    stu = query("SELECT id, name FROM students ORDER BY name")
    with st.expander("➕ 补记一节课(临时加课/补课)"):
        if cls.empty:
            st.info("先创建班级")
        else:
            with st.form("extra"):
                c1, c2, c3, c4 = st.columns(4)
                cid = c1.selectbox("班级", cls.id.tolist(), format_func=dict(zip(cls.id, cls.name)).get)
                d = c2.date_input("日期", date.today())
                t1 = c3.time_input("开始", time(19, 0))
                t2 = c4.time_input("结束", time(20, 30))
                if st.form_submit_button("添加"):
                    s, e = t1.strftime("%H:%M"), t2.strftime("%H:%M")
                    if query("SELECT id FROM lessons WHERE class_id=? AND lesson_date=? AND start=?", (int(cid), str(d), s)).empty:
                        complete(cid, d, s, e)
                        st.rerun()
                    else:
                        st.error("这节课已经有记录了")
    f1, f2, f3 = st.columns(3)
    fc = f1.selectbox("班级", [0] + cls.id.tolist(), format_func=lambda i: "全部班级" if i == 0 else dict(zip(cls.id, cls.name))[i])
    fs = f2.selectbox("学生", [0] + stu.id.tolist(), format_func=lambda i: "全部学生" if i == 0 else dict(zip(stu.id, stu.name))[i])
    only = f3.checkbox("只看未填写的")
    sql, ps = "SELECT * FROM v_lessons WHERE 1=1", []
    if fc:
        sql += " AND class_id=?"; ps.append(fc)
    if fs:
        sql += " AND class_id IN (SELECT class_id FROM class_members WHERE student_id=?)"; ps.append(fs)
    if only:
        sql += " AND content=''"
    df = query(sql + " ORDER BY lesson_date DESC, start DESC LIMIT 100", ps)
    if df.empty:
        st.info("没有记录")
    for r in df.itertuples():
        mark = "" if r.content else " · ⚠️ 未填"
        with st.expander(f"{r.lesson_date} {r.start}  {r.cname} · 第{r.seq}节{mark}"):
            with st.form(f"lf{r.id}"):
                content = st.text_area("本节课内容", r.content, height=80)
                mastery = st.text_area("学习情况/掌握情况", r.mastery, height=80)
                homework = st.text_area("作业", r.homework, height=60)
                note = st.text_input("备注", r.note)
                hrs = st.number_input("实际课时", 0.0, 10.0, float(r.hours), 0.5)
                b1, b2 = st.columns(2)
                if b1.form_submit_button("保存", type="primary"):
                    run("UPDATE lessons SET content=?, mastery=?, homework=?, note=?, hours=? WHERE id=?",
                        (content, mastery, homework, note, hrs, int(r.id)))
                    st.rerun()
                if b2.form_submit_button("删除这节课"):
                    run("DELETE FROM lessons WHERE id=?", (int(r.id),))
                    st.rerun()


# ==================== 考试成绩 ====================
def pick_exam(key):
    ex = query("""SELECT e.*, COALESCE(c.name,'') cname FROM exams e LEFT JOIN classes c ON c.id=e.class_id
                  ORDER BY e.exam_date DESC, e.id DESC""")
    if ex.empty:
        st.info("先在「考试管理」里添加考试")
        return None
    lab = labels(ex, lambda r: f"{r['exam_date']} {r['name']} {r['cname']}({r['subject']})")
    eid = st.selectbox("考试", list(lab), format_func=lab.get, key=key)
    return ex[ex.id == eid].iloc[0]


def exam_students(ex):
    if pd.notna(ex["class_id"]):
        return query("""SELECT s.* FROM students s JOIN class_members m ON m.student_id=s.id
                        WHERE m.class_id=? AND s.status='在读' ORDER BY s.name""", (int(ex["class_id"]),))
    return query("SELECT * FROM students WHERE status='在读' ORDER BY name")


def tab_entry():
    ex = pick_exam("in_exam")
    if ex is None:
        return
    eid, full = int(ex["id"]), float(ex["full_score"])
    stu = exam_students(ex)
    done = set(query("SELECT student_id FROM scores WHERE exam_id=?", (eid,)).student_id)
    st.subheader("快速录分")
    todo = stu[~stu.id.isin(done)]
    if todo.empty:
        st.success("这场考试的学生都录过分了")
    else:
        df = pd.DataFrame({"姓名": todo["name"].values, "得分": [float("nan")] * len(todo)}, index=todo.id.values)
        ed = st.data_editor(df, disabled=["姓名"], hide_index=True, use_container_width=True, key=f"q{eid}",
                            column_config={"得分": st.column_config.NumberColumn(min_value=0.0, max_value=full, step=0.5)})
        if st.button("保存这些成绩", type="primary"):
            n = 0
            for sid, row in ed.iterrows():
                if pd.notna(row["得分"]):
                    run("INSERT INTO scores(student_id, exam_id, score) VALUES(?,?,?)", (int(sid), eid, float(row["得分"])))
                    n += 1
            st.success(f"保存了 {n} 人")
            st.rerun()
    with st.expander("单个学生详细录入(错题/错因/知识点/试卷和答题卡照片)"):
        if stu.empty:
            st.info("这场考试没有可选学生,先到「班级」里加名单")
            return
        sid = st.selectbox("学生", stu.id.tolist(), format_func=dict(zip(stu.id, stu["name"])).get)
        old = query("SELECT * FROM scores WHERE student_id=? AND exam_id=?", (int(sid), eid))
        o = old.iloc[0] if len(old) else None
        score = st.number_input(f"得分(满分 {full:g})", 0.0, full, float(o.score) if o is not None else 0.0, 0.5, key=f"sc{sid}{eid}")
        wrong = st.text_input("错题题号(如 5,12,18)", o.wrong_qs if o is not None else "", key=f"w{sid}{eid}")
        reasons = st.multiselect("错因", REASONS, [x for x in (o.reasons.split(",") if o is not None and o.reasons else []) if x in REASONS], key=f"r{sid}{eid}")
        knowledge = st.text_input("涉及知识点", o.knowledge if o is not None else "", key=f"k{sid}{eid}")
        paper = st.file_uploader("试卷照片/PDF(可多张)", type=["jpg", "jpeg", "png", "pdf"], accept_multiple_files=True, key=f"p{sid}{eid}")
        sheet = st.file_uploader("答题卡照片/PDF(可多张)", type=["jpg", "jpeg", "png", "pdf"], accept_multiple_files=True, key=f"s{sid}{eid}")
        note = st.text_input("备注", o.note if o is not None else "", key=f"n{sid}{eid}")
        if st.button("保存这个学生的详细记录"):
            tag = f"e{eid}_s{sid}"
            p, s = save_photos(paper, tag + "_paper"), save_photos(sheet, tag + "_sheet")
            if o is None:
                run("""INSERT INTO scores(student_id, exam_id, score, wrong_qs, reasons, knowledge, paper, sheet, note)
                       VALUES(?,?,?,?,?,?,?,?,?)""", (int(sid), eid, score, wrong, ",".join(reasons), knowledge, p, s, note))
            else:
                run("""UPDATE scores SET score=?, wrong_qs=?, reasons=?, knowledge=?, note=?,
                       paper=CASE WHEN ?='' THEN paper ELSE ? END, sheet=CASE WHEN ?='' THEN sheet ELSE ? END
                       WHERE id=?""", (score, wrong, ",".join(reasons), knowledge, note, p, p, s, s, int(o.id)))
            st.success("已保存")


def tab_analysis():
    ex = pick_exam("an_exam")
    if ex is None:
        return
    eid, full = int(ex["id"]), float(ex["full_score"])
    df = query("SELECT s.id sid, s.name 姓名, sc.score 得分 FROM scores sc JOIN students s ON s.id=sc.student_id WHERE sc.exam_id=?", (eid,))
    if df.empty:
        st.info("这场考试还没有成绩")
        return
    df["得分率"] = (df["得分"] / full * 100).round(1)
    df["排名"] = df["得分"].rank(ascending=False, method="min").astype(int)
    prev = query("""SELECT sc.student_id sid, sc.score / e.full_score * 100 prate FROM scores sc
                    JOIN exams e ON e.id=sc.exam_id
                    WHERE e.subject=? AND (e.exam_date<? OR (e.exam_date=? AND e.id<?))
                    ORDER BY e.exam_date, e.id""", (ex["subject"], ex["exam_date"], ex["exam_date"], eid)).drop_duplicates("sid", keep="last")
    df = df.merge(prev, on="sid", how="left")
    df["较上次(得分率)"] = (df["得分率"] - df["prate"]).round(1)
    delta = df["得分率"].mean() - df["prate"].mean() if df["prate"].notna().any() else None
    m = st.columns(4)
    m[0].metric("平均分", f"{df['得分'].mean():.1f}", f"{delta:+.1f}% 较上次" if delta is not None and pd.notna(delta) else None)
    m[1].metric("最高分", f"{df['得分'].max():g}")
    m[2].metric("最低分", f"{df['得分'].min():g}")
    m[3].metric("参考人数", len(df))
    st.dataframe(df.sort_values("排名")[["排名", "姓名", "得分", "得分率", "较上次(得分率)"]],
                 hide_index=True, use_container_width=True)
    st.bar_chart(df.set_index("姓名")["得分"])


def tab_student():
    stu = query("SELECT id, name FROM students ORDER BY name")
    if stu.empty:
        st.info("还没有学生")
        return
    sid = st.selectbox("学生", stu.id.tolist(), format_func=dict(zip(stu.id, stu["name"])).get, key="rep_stu")
    student_report(sid)


def tab_manage():
    cls = query("SELECT id, name FROM classes ORDER BY name")
    with st.form("new_exam"):
        c1, c2, c3, c4 = st.columns(4)
        n = c1.text_input("考试名称")
        d = c2.date_input("日期", date.today())
        s = c3.selectbox("科目", SUBJECTS)
        f = c4.number_input("满分", 1.0, 300.0, 100.0, 5.0)
        c = st.selectbox("所属班级(选了才能统计班级排名和「待录成绩」)", [0] + cls.id.tolist(),
                         format_func=lambda i: "不限班级" if i == 0 else dict(zip(cls.id, cls.name))[i])
        if st.form_submit_button("添加考试") and n.strip():
            run("INSERT INTO exams(name, exam_date, subject, full_score, class_id) VALUES(?,?,?,?,?)",
                (n.strip(), str(d), s, f, c or None))
            st.rerun()
    st.dataframe(query("""SELECT e.exam_date 日期, e.name 考试, e.subject 科目, e.full_score 满分, COALESCE(c.name,'不限') 班级
                          FROM exams e LEFT JOIN classes c ON c.id=e.class_id ORDER BY e.exam_date DESC"""),
                 hide_index=True, use_container_width=True)


def page_exams():
    st.title("考试成绩")
    t1, t2, t3, t4 = st.tabs(["录入成绩", "考试分析", "学生成绩", "考试管理"])
    with t1:
        tab_entry()
    with t2:
        tab_analysis()
    with t3:
        tab_student()
    with t4:
        tab_manage()


# ==================== 登录 & 导航 ====================
init_db()
cm.configure(DB)
cm.init_course_db(DB)
um.configure(DB)
um.init_user_db(DB, default_teacher_password=PASSWORD)

user = um.current_user()
if user is None:
    um.render_login()
    st.stop()

um.render_sidebar_identity()
if int(user.get("must_change_password") or 0) == 1:
    um.page_account(force_change=True)
    st.stop()

if user["role"] == um.ROLE_TEACHER:
    PAGES = {
        "首页": cm.page_home,
        "课程表": cm.page_schedule,
        "教学对象": cm.page_targets,
        "学生": page_students,
        "授课统计": cm.page_stats,
        "考试成绩": page_exams,
        "账号管理": um.page_user_admin,
        "账号": um.page_account,
    }
else:
    PAGES = {
        "我的课程": um.page_student_courses,
        "我的成绩": um.page_student_scores,
        "账号": um.page_account,
    }

def page_me() -> None:
    """手机版「我的」：身份、快捷入口、待办、课时预警、本月速览、退出登录。"""
    me = um.current_user() or {}
    st.title("我的")
    st.caption(f"{um.ROLE_LABELS.get(me.get('role'), '')}：{me.get('username') or ''}")

    st.markdown("#### ⚡ 快捷入口")
    with st.container(key="me_quick"):
        for pair in (["学生", "授课统计"], ["账号管理", "账号资料"]):
            cols = st.columns(2)
            for col, label in zip(cols, pair):
                target = "账号" if label == "账号资料" else label
                if col.button(label, use_container_width=True, key=f"me_{target}"):
                    st.session_state["mobile_extra"] = target
                    st.rerun()

    today = date.today()
    st.markdown("#### 🔔 待办提醒")
    overdue = int(query(
        "SELECT COUNT(*) n FROM plans WHERE status='待上课' AND plan_date<? AND plan_date>=?",
        (str(today), str(today - timedelta(days=30))),
    ).n[0])
    if overdue:
        st.button(
            f"⚠️ 过去 30 天有 {overdue} 节课还没确认「已上课」→ 去处理",
            use_container_width=True, key="me_overdue",
            on_click=lambda: st.session_state.update(mobile_extra=None, page="课程表"),
        )
    else:
        st.success("没有待确认的课程")

    st.markdown("#### ⏳ 课时预警")
    try:
        one = cm.one_to_one_summary()
    except Exception:
        one = pd.DataFrame()
    if one.empty:
        st.caption("还没有一对一学生")
    else:
        low = one[one["remain"].astype(float) <= 3]
        if low.empty:
            st.caption("暂无课时不足的学生")
        else:
            for r in low.itertuples():
                st.markdown(f"- **{r.name}**：只剩 {float(r.remain):g} 课时")

    st.markdown("#### 📊 本月速览")
    month = today.strftime("%Y-%m")
    total = int(query("SELECT COUNT(*) n FROM plans WHERE substr(plan_date,1,7)=? AND status='已上课'", (month,)).n[0])
    one_n = int(query(
        "SELECT COUNT(*) n FROM plans WHERE substr(plan_date,1,7)=? AND status='已上课' AND target_type='一对一'",
        (month,),
    ).n[0])
    with st.container(key="me_month"):
        mm = st.columns(2)
        mm[0].metric("本月已授课", total)
        mm[1].metric("其中一对一", one_n)

    st.divider()
    if st.button("退出登录", use_container_width=True, key="me_logout"):
        um.logout()


if user["role"] == um.ROLE_TEACHER:
    MOBILE_TABS = {"首页": "首页", "课表": "课程表", "教学": "教学对象", "成绩": "成绩", "我的": "我的"}
    MOBILE_EXTRA = ["学生", "授课统计", "账号管理", "账号"]
else:
    MOBILE_TABS = {"我的课程": "我的课程", "成绩": "我的成绩", "账号": "账号"}
    MOBILE_EXTRA = []

if cm.is_mobile():
    extra = st.session_state.get("mobile_extra")
    if extra in MOBILE_EXTRA:
        if st.button("← 返回", key="mobile_back"):
            st.session_state.pop("mobile_extra", None)
            st.rerun()
        st.divider()
        PAGES[extra]()
    else:
        labels = list(MOBILE_TABS)
        with st.container(key="bottomnav"):
            tab = st.segmented_control(
                "导航", labels, default=labels[0], key="mobile_tab",
                label_visibility="collapsed",
            )
        target = MOBILE_TABS.get(tab or labels[0], labels[0])
        if target == "我的":
            page_me()
        elif target == "成绩":
            page_exams()
        elif target == "我的成绩":
            um.page_student_scores()
        else:
            PAGES[target]()
else:
    page = st.sidebar.radio("导航", list(PAGES), key="page")
    PAGES[page]()
