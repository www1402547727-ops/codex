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
import ui_theme
import user_management as um

# ==================== 配置 ====================
PASSWORD = os.environ.get("SCORE_TRACKER_PASSWORD", "")
DAY_START, DAY_END = "08:00", "20:30"   # 课程表/空闲时间统计的工作时段
SUBJECTS = ["数学", "物理", "其他"]
REASONS = ["计算错误", "概念不清", "审题失误", "方法不会", "没时间", "粗心抄错", "其他", "自定义"]
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
ui_theme.inject()


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
  meta("theme-color", "#1F5C4D");
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
CREATE TABLE IF NOT EXISTS exam_categories(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, sort INTEGER DEFAULT 0);
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
                 "status": "TEXT DEFAULT '在读'", "note": "TEXT DEFAULT ''",
                 "school": "TEXT DEFAULT ''"},
    "exams": {"class_id": "INTEGER", "category": "TEXT DEFAULT ''",
              "school": "TEXT DEFAULT ''", "grade": "TEXT DEFAULT ''",
              "paper": "TEXT DEFAULT ''"},
    "classes": {"kind": "TEXT DEFAULT '班课'"},
    "lessons": {"plan_id": "INTEGER"},
    "scores": {"status": "TEXT DEFAULT ''"},
}


def _merge_duplicate_students(conn) -> list:
    """把"同一个人被建成多条记录"合并成一条：搬运班级/成绩/账号/一对一关联到保留的那条。"""
    groups = conn.execute(
        """SELECT TRIM(name) nm, GROUP_CONCAT(id) ids FROM students
           GROUP BY TRIM(name) HAVING COUNT(*) > 1"""
    ).fetchall()
    merged = []
    for nm, ids_text in groups:
        ids = sorted(int(x) for x in str(ids_text).split(","))
        keep = ids[0]
        # 优先保留已被"账号"或"一对一"引用的那条，避免断链
        for cand in ids:
            if conn.execute("SELECT 1 FROM users WHERE student_id=?", (cand,)).fetchone() \
               or conn.execute("SELECT 1 FROM one_to_one_students WHERE student_id=?", (cand,)).fetchone():
                keep = cand
                break
        drop = [i for i in ids if i != keep]
        for d in drop:
            conn.execute("UPDATE OR IGNORE class_members SET student_id=? WHERE student_id=?", (keep, d))
            conn.execute("DELETE FROM class_members WHERE student_id=?", (d,))
            conn.execute("UPDATE scores SET student_id=? WHERE student_id=?", (keep, d))
            conn.execute("UPDATE users SET student_id=? WHERE student_id=?", (keep, d))
            conn.execute("UPDATE one_to_one_students SET student_id=? WHERE student_id=?", (keep, d))
            conn.execute("DELETE FROM students WHERE id=?", (d,))
        merged.append((nm, keep, drop))
    return merged


def init_db():
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    for table, cols in ADD_COLS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, typ in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    # 同一场考试 + 同一个学生只保留一条成绩（历史重复先合并，再建唯一索引）
    conn.execute("""DELETE FROM scores WHERE id NOT IN (
                        SELECT id FROM (
                            SELECT id, ROW_NUMBER() OVER (
                                       PARTITION BY exam_id, student_id
                                       ORDER BY (score IS NOT NULL) DESC,
                                                (COALESCE(wrong_qs,'')||COALESCE(knowledge,'')||COALESCE(paper,'')) <> '' DESC,
                                                id DESC) rn
                            FROM scores WHERE exam_id IS NOT NULL AND student_id IS NOT NULL) WHERE rn = 1)""")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_scores_exam_student ON scores(exam_id, student_id)")
    # 老成绩没有 status 字段：有分数=已录入，没分数=未录入（空不等于 0 分）
    conn.execute("UPDATE scores SET status='已录入' WHERE COALESCE(status,'')='' AND score IS NOT NULL")
    conn.execute("UPDATE scores SET status='未录入' WHERE COALESCE(status,'')=''")
    # 考试分类：首次使用时给几个常用分类
    if conn.execute("SELECT COUNT(*) FROM exam_categories").fetchone()[0] == 0:
        for i, nm in enumerate(["月考", "期中", "期末", "模拟考", "随堂测", "其他"]):
            conn.execute("INSERT OR IGNORE INTO exam_categories(name, sort) VALUES(?,?)", (nm, i))
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
    # 一次性修复：同一个人被建成了多条学生记录 -> 合并（只跑一次）
    conn.execute("""CREATE TABLE IF NOT EXISTS app_migrations(
                        name TEXT PRIMARY KEY, applied_at TEXT DEFAULT '', detail TEXT DEFAULT '')""")
    if not conn.execute("SELECT 1 FROM app_migrations WHERE name='merge_dup_students_v1'").fetchone():
        merged = _merge_duplicate_students(conn)
        detail = "; ".join(f"{nm}: 保留#{keep} 合并{drop}" for nm, keep, drop in merged)
        conn.execute("INSERT INTO app_migrations(name, applied_at, detail) VALUES(?,?,?)",
                     ("merge_dup_students_v1", datetime.now().isoformat(timespec="seconds"), detail))
    # 一次性迁移：以前每个学生各传一份试卷 -> 把该场考试里出现最多的那份提升为"考试级共用试卷"
    if not conn.execute("SELECT 1 FROM app_migrations WHERE name='exam_paper_share_v1'").fetchone():
        rows = conn.execute(
            """SELECT exam_id, paper, COUNT(*) n FROM scores
               WHERE COALESCE(paper,'')<>'' AND exam_id IS NOT NULL
               GROUP BY exam_id, paper ORDER BY exam_id, n DESC, paper"""
        ).fetchall()
        chosen = {}
        for exam_id, paper, n in rows:
            if exam_id not in chosen:
                chosen[exam_id] = paper
        promoted = 0
        for exam_id, paper in chosen.items():
            cur = conn.execute("SELECT COALESCE(paper,'') FROM exams WHERE id=?", (exam_id,)).fetchone()
            if cur is not None and not str(cur[0] or "").strip():
                conn.execute("UPDATE exams SET paper=? WHERE id=?", (paper, exam_id))
                promoted += 1
        conn.execute("INSERT INTO app_migrations(name, applied_at, detail) VALUES(?,?,?)",
                     ("exam_paper_share_v1", datetime.now().isoformat(timespec="seconds"),
                      f"把 {promoted} 场考试的学生试卷提升为共用试卷"))
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
            sch_in = c1.text_input("学校")
            grade = c2.text_input("年级")
            contact = st.text_input("联系方式(只导入一个学生时填)")
            join = st.multiselect("加入班级", cls.id.tolist(), format_func=dict(zip(cls.id, cls.name)).get)
            if st.form_submit_button("添加"):
                lst = [x.strip() for x in names.splitlines() if x.strip()]
                added, reused = 0, 0
                for n in lst:
                    hit = query("SELECT id FROM students WHERE TRIM(name)=? ORDER BY id LIMIT 1", (n,))
                    if len(hit):
                        sid = int(hit["id"].iloc[0])
                        reused += 1
                    else:
                        sid = run("INSERT INTO students(name, grade, contact, school) VALUES(?,?,?,?)",
                                  (n, grade, contact, sch_in.strip()))
                        added += 1
                    for c in join:
                        run("INSERT OR IGNORE INTO class_members VALUES(?,?)", (int(c), sid))
                msg = f"新增 {added} 人"
                if reused:
                    msg += f"；{reused} 人本来就有，直接复用并加入所选班级(不会重复建人)"
                st.success(msg)
    with st.expander("🔗 把已有学生加入 / 移出班级"):
        if cls.empty:
            st.info("先去「教学对象 → 班级」建一个班级")
        else:
            all_stu = query("SELECT id, name FROM students ORDER BY name")
            cid2 = st.selectbox("选择班级", cls.id.tolist(),
                                format_func=dict(zip(cls.id, cls.name)).get, key="roster_class")
            now_members = query("SELECT student_id FROM class_members WHERE class_id=?",
                                (int(cid2),)).student_id.tolist()
            valid_ids = set(all_stu.id.tolist())
            picked = st.multiselect(
                "这个班里有哪些学生", all_stu.id.tolist(),
                default=[x for x in now_members if x in valid_ids],
                format_func=dict(zip(all_stu.id, all_stu.name)).get,
                key=f"roster_members_{cid2}",
            )
            if st.button("保存班级名单", type="primary", key=f"roster_save_{cid2}"):
                run("DELETE FROM class_members WHERE class_id=?", (int(cid2),))
                for sid in picked:
                    run("INSERT OR IGNORE INTO class_members VALUES(?,?)", (int(cid2), int(sid)))
                st.success("名单已保存")
                st.rerun()
    flt = st.radio("状态", ["在读", "停课", "全部"], horizontal=True)
    df = query("""SELECT s.id, s.name 姓名, s.school 学校, s.grade 年级, s.status 状态, s.contact 联系方式,
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
            sch = st.text_input("学校", (s["school"] or "") if "school" in s.index else "")
            ct = st.text_input("联系方式", s.contact or "")
            nt = st.text_area("备注", s.note or "", height=80)
            if st.form_submit_button("保存"):
                run("UPDATE students SET name=?, grade=?, status=?, contact=?, note=?, school=? WHERE id=?",
                    (n.strip(), g, stt, ct, nt, sch.strip(), int(sid)))
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
    subs = sorted({str(x) for x in ex["subject"].fillna("") if str(x).strip()})
    cats = sorted({str(x) for x in ex["category"].fillna("") if str(x).strip()})
    schools = sorted({str(x) for x in ex.get("school").fillna("") if str(x).strip()}) if "school" in ex.columns else []
    grades = sorted({str(x) for x in ex.get("grade").fillna("") if str(x).strip()}) if "grade" in ex.columns else []
    f1, f2 = st.columns(2)
    sub = f1.selectbox("科目", ["全部"] + subs, key=f"{key}_subj")
    cat = f2.selectbox("分类", ["全部"] + cats, key=f"{key}_cat")
    f3, f4 = st.columns(2)
    sch = f3.selectbox("学校", ["全部"] + schools, key=f"{key}_school")
    grd = f4.selectbox("年级", ["全部"] + grades, key=f"{key}_grade")
    view = ex
    if sub != "全部":
        view = view[view["subject"].fillna("") == sub]
    if cat != "全部":
        view = view[view["category"].fillna("") == cat]
    if sch != "全部":
        view = view[view["school"].fillna("") == sch]
    if grd != "全部":
        view = view[view["grade"].fillna("") == grd]
    if view.empty:
        st.info("这个筛选条件下没有考试，换个条件试试。")
        return None

    def _exam_label(r):
        loc = " ".join(str(x) for x in (r.get("school", ""), r.get("grade", "")) if str(x).strip())
        return f"{r['exam_date']} {r['name']}" + (f" · {loc}" if loc else "") + f"（{r['subject']}）"

    lab = labels(view, _exam_label)
    eid = st.selectbox("考试", list(lab), format_func=lab.get, key=key)
    return view[view.id == eid].iloc[0]


def row_val(row, key, default=""):
    """从 pandas Series 里安全取值（列不存在或为 NaN 时返回 default）。"""
    try:
        v = row[key]
    except Exception:
        return default
    if v is None:
        return default
    try:
        if pd.isna(v):
            return default
    except Exception:
        pass
    return v


def score_state(row):
    """一条成绩记录的状态：缺考 / 已录入 / 未录入。"""
    if row is None:
        return "未录入"
    stt = str(row_val(row, "status", "") or "").strip()
    if stt == "缺考":
        return "缺考"
    if stt == "已录入":
        return "已录入"
    return "已录入" if pd.notna(row_val(row, "score", None)) else "未录入"


def file_count(joined) -> int:
    """已保存的附件数量（用 | 分隔）。"""
    return len([x for x in str(joined or "").split("|") if x.strip()])


def teach_map(student_ids):
    """返回 {student_id: {"kind": 教学类型, "class": 班级名}}，一对一学生也不会有班级。"""
    ids = [int(x) for x in student_ids]
    info = {i: {"classes": [], "o2o": False} for i in ids}
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    rows = query(f"""SELECT m.student_id sid, c.name cname FROM class_members m
                     JOIN classes c ON c.id=m.class_id WHERE m.student_id IN ({q})""", tuple(ids))
    for _, r in rows.iterrows():
        info[int(r["sid"])]["classes"].append(str(r["cname"]))
    try:
        rows2 = query(f"""SELECT student_id sid FROM one_to_one_students
                          WHERE student_id IN ({q})""", tuple(ids))
        for x in rows2["sid"].tolist():
            if int(x) in info:
                info[int(x)]["o2o"] = True
    except Exception:
        pass
    out = {}
    for sid, d in info.items():
        kinds = []
        if d["classes"]:
            kinds.append("班课")
        if d["o2o"]:
            kinds.append("一对一")
        out[sid] = {"kind": "·".join(kinds) if kinds else "—",
                    "class": "、".join(d["classes"]) if d["classes"] else "—"}
    return out


def exam_students(ex):
    """考试名单：优先按「学校 + 年级」取全部在读学生（含一对一）；
    老考试没填学校/年级时，回退到原来的班级名单。"""
    school = str(row_val(ex, "school", "") or "").strip()
    grade = str(row_val(ex, "grade", "") or "").strip()
    if school or grade:
        sql = "SELECT * FROM students WHERE status='在读'"
        params = []
        if school:
            sql += " AND TRIM(COALESCE(school,''))=?"
            params.append(school)
        if grade:
            sql += " AND TRIM(COALESCE(grade,''))=?"
            params.append(grade)
        sql += " ORDER BY name"
        return query(sql, tuple(params))
    cid = row_val(ex, "class_id", None)
    if cid is not None and pd.notna(cid):
        return query("""SELECT s.* FROM students s JOIN class_members m ON m.student_id=s.id
                        WHERE m.class_id=? AND s.status='在读' ORDER BY s.name""", (int(cid),))
    return query("SELECT * FROM students WHERE status='在读' ORDER BY name")


def save_entry_changes(eid, changes, old_map):
    """按「未录入 / 已录入 / 缺考」三态保存成绩。

    changes: [{"sid": 学生id, "status": 状态, "score": 分数或None}]
    返回 (已录入人数, 缺考人数, 清空人数, 跳过人数)。
    同一场考试同一学生只有一条记录（scores 上有唯一索引），所以不会重复扣分。
    """
    saved = absent = cleared = skipped = 0
    for ch in changes:
        sid = int(ch["sid"])
        stt = str(ch.get("status") or "未录入").strip()
        sc = ch.get("score")
        has_score = sc is not None and pd.notna(sc)
        o = old_map.get(sid)
        exist = o is not None
        if stt == "缺考":
            if not exist or score_state(o) != "缺考":
                run("""INSERT INTO scores(student_id, exam_id, score, status) VALUES(?,?,NULL,'缺考')
                       ON CONFLICT(exam_id, student_id) DO UPDATE SET score=NULL, status='缺考'""", (sid, eid))
                absent += 1
        elif stt == "已录入" or has_score:
            if not has_score:
                skipped += 1
                continue
            new_val = float(sc)
            same = (exist and score_state(o) == "已录入"
                    and pd.notna(row_val(o, "score", None))
                    and abs(float(row_val(o, "score", 0)) - new_val) < 1e-9)
            if not same:
                run("""INSERT INTO scores(student_id, exam_id, score, status) VALUES(?,?,?,'已录入')
                       ON CONFLICT(exam_id, student_id) DO UPDATE SET score=excluded.score, status='已录入'""",
                    (sid, eid, new_val))
                saved += 1
        else:
            if exist:
                run("DELETE FROM scores WHERE student_id=? AND exam_id=?", (sid, eid))
                cleared += 1
    return saved, absent, cleared, skipped


FILL_TEMPLATE = """请对照答题卡，按下面的格式回我四行，不要多余的话：
错题：（填题号，多个用逗号分隔）
错因：（只能从这些里选：计算错误、概念不清、审题失误、方法不会、没时间、粗心抄错、其他；确实不在这几个里的，照你自己的话写）
知识点：（填知识点，多个用顿号分隔）
分析：（针对这张卷子写一段详细分析，可以写多行）"""

# 错因的常见说法 -> 标准标签（换种说法写也能归位）
REASON_HINTS = {
    "计算错误": ["计算", "算错", "运算", "算理", "口算"],
    "概念不清": ["概念", "定义", "性质", "基础不牢", "理解不透", "知识点不清"],
    "审题失误": ["审题", "读题", "看错题", "题意", "漏看条件", "看错条件"],
    "方法不会": ["方法", "不会", "思路", "步骤", "无从下手", "没思路", "解题思路"],
    "没时间": ["没时间", "时间不够", "来不及", "没做完", "时间紧"],
    "粗心抄错": ["粗心", "抄错", "笔误", "马虎", "看错数字", "看错符号", "符号错误"],
    "其他": ["其他", "其它", "综合"],
}

_FILL_FIELDS = [
    ("wrong", r"(错题题号|错题号|错题|题号|错的题)"),
    ("reasons", r"(错因|错误原因|失分原因|原因)"),
    ("knowledge", r"(涉及知识点|薄弱知识点|知识点|考点)"),
    ("score", r"(得分|分数|成绩)"),
]


def _extract_note(raw) -> str:
    """把"分析/点评/备注"标签后面那一整段（可跨多行）原样取出来。"""
    lines = [str(x).rstrip() for x in str(raw or "").splitlines()]
    start = None
    for i, line in enumerate(lines):
        head = line.strip().split(":")[0].split("：")[0][:10]
        if re.search(r"(试卷分析|整体分析|分析|点评|备注|总结|讲评)", head):
            start = i
            break
    if start is None:
        return ""
    m = re.match(r"^\s*[^:：]{0,12}[:：]\s*(.*)$", lines[start])
    buf = [m.group(1) if m else lines[start]]
    for line in lines[start + 1:]:
        head = line.strip().split(":")[0].split("：")[0].strip()
        if re.fullmatch(r"(错题题号|错题号|错题|题号|错因|错误原因|失分原因|知识点|考点|得分|分数|成绩)", head):
            break
        buf.append(line)
    return "\n".join([x.strip() for x in buf if x.strip()]).strip()


def _norm_text(t) -> str:
    """把全角标点、markdown 符号统一掉，方便机器识别。"""
    t = str(t or "")
    for a, b in (("：", ":"), ("，", ","), ("、", ","), ("；", ";"), ("．", "."),
                 ("（", "("), ("）", ")"), ("～", "-"), ("—", "-"), ("　", " ")):
        t = t.replace(a, b)
    for ch in ("*", "#", "|", "`", ">", "[", "]", "**"):
        t = t.replace(ch, " ")
    return t


def _nums(text) -> str:
    """抽题号：支持 5,12 / 5-8 / 第5题 / 12、18 等写法。"""
    out, seen = [], set()
    for part in re.findall(r"\d+\s*(?:[-~至]\s*\d+)?", str(text or "")):
        if re.search(r"[-~至]", part):
            a, b = re.split(r"[-~至]", part)
            try:
                a, b = int(a), int(b)
            except Exception:
                continue
            if 0 < a <= b <= 200:
                out.extend(range(a, b + 1))
        else:
            try:
                out.append(int(part))
            except Exception:
                pass
    res = []
    for n in out:
        if 0 < n <= 200 and n not in seen:
            seen.add(n)
            res.append(n)
    return ",".join(str(n) for n in res)


def parse_fill_text(text) -> dict:
    """把一整段文字解析成 错题/错因/知识点/得分。"""
    res = {"wrong": "", "reasons": [], "custom": "", "knowledge": "", "score": None, "note": ""}
    res["note"] = _extract_note(text)
    t = _norm_text(text)
    lines = [l.strip(" -\t") for l in t.splitlines() if l.strip()]
    fields, used = {}, set()
    for idx, line in enumerate(lines):
        m = re.match(r"^([^:]{1,16}):\s*(.+)$", line)
        if not m:
            m = re.match(r"^(错题题号|错题号|错题|题号|错的题|错因|错误原因|失分原因|原因|涉及知识点|薄弱知识点|知识点|考点|得分|分数|成绩)\s+(.+)$", line)
        if not m:
            continue
        key_raw, val = m.group(1).strip(), m.group(2).strip()
        for key, pat in _FILL_FIELDS:
            if re.search(pat, key_raw):
                if key not in fields:
                    fields[key] = val
                    used.add(idx)
                break
    # 兜底：没写"错题"标签时，找包含 2 个以上数字、或带"题"字的那一行
    if "wrong" not in fields:
        for idx, line in enumerate(lines):
            if idx in used:
                continue
            n = re.findall(r"\d+", line)
            if len(n) >= 2 or ("题" in line and n):
                fields["wrong"] = line
                used.add(idx)
                break
    res["wrong"] = _nums(fields.get("wrong", ""))
    res["knowledge"] = str(fields.get("knowledge", "")).strip().replace(",", "、")
    sc = re.search(r"\d+(?:\.\d+)?", str(fields.get("score", "")))
    if sc:
        try:
            res["score"] = float(sc.group(0))
        except Exception:
            res["score"] = None
    # 错因：先归类，认不出来的原样放进"自定义错因"
    raw_reasons = str(fields.get("reasons", "")).strip()
    if not raw_reasons:
        for line in lines:
            if any(h in line for hints in REASON_HINTS.values() for h in hints):
                raw_reasons = line
                break
    matched, custom = [], []
    for chunk in [c.strip() for c in re.split(r"[,;/]|以及|和|及", raw_reasons) if c.strip()]:
        best, best_len = None, 0
        for name, hints in REASON_HINTS.items():
            for h in hints:
                if h in chunk and len(h) > best_len:
                    best, best_len = name, len(h)
        if best:
            if best not in matched:
                matched.append(best)
        elif chunk not in custom:
            custom.append(chunk)
    res["reasons"] = [r for r in REASONS if r in matched]
    drop = {"无", "没有", "暂无", "无错因", "无明显错因", "-", "none", "None", "N/A", "n/a"}
    res["custom"] = ",".join([c for c in custom if c not in drop])
    return res


def quick_fill_box(sid: int, eid: int) -> None:
    """单个学生录分时的快速填写：把整段文字贴进来，自动填到下面。"""
    st.markdown("##### 📋 快速填写（把文字整段粘贴进来，自动填到下面）")
    st.caption("第一步：复制下面这段格式说明。")
    st.code(FILL_TEMPLATE, language=None)
    st.caption("第二步：把内容整段粘贴到这里，点「识别并填充」。")
    blob = st.text_area("粘贴内容", key=f"fill{sid}{eid}", height=110,
                        placeholder="错题：5,12,18\n错因：计算错误,方法不会\n知识点：二次函数、判别式")
    if st.button("🔎 识别并填充到下面", key=f"fillbtn{sid}{eid}"):
        r = parse_fill_text(blob)
        got = []
        if r["wrong"]:
            st.session_state[f"w{sid}{eid}"] = r["wrong"]
            got.append(f"错题 {r['wrong']}")
        if r["reasons"] or r["custom"]:
            st.session_state[f"r{sid}{eid}"] = r["reasons"]
            st.session_state[f"c{sid}{eid}"] = r["custom"]
            got.append("错因 " + ",".join(list(r["reasons"]) + ([r["custom"]] if r["custom"] else [])))
        if r["knowledge"]:
            st.session_state[f"k{sid}{eid}"] = r["knowledge"]
            got.append("知识点 " + r["knowledge"])
        if r["score"] is not None:
            st.session_state[f"sc{sid}{eid}"] = float(r["score"])
            got.append(f"得分 {r['score']:g}")
        if r.get("note"):
            st.session_state[f"nt{sid}{eid}"] = r["note"]
            got.append("试卷分析")
        if got:
            st.success("已识别并填到下面：" + "；".join(got) + "（还能手动改）")
        else:
            st.warning("没识别出内容，看看粘贴的格式，或直接在下面手填。")
    st.divider()


def tab_entry():
    ex = pick_exam("in_exam")
    if ex is None:
        return
    _flash = st.session_state.pop("entry_msg", None)
    if _flash:
        st.success(_flash)
    eid, full = int(ex["id"]), float(ex["full_score"])
    stu = exam_students(ex)
    scope_school = str(row_val(ex, "school", "") or "未填")
    scope_grade = str(row_val(ex, "grade", "") or "未填")
    if stu.empty:
        st.warning("这场考试没有匹配到学生。")
        st.caption(f"考试范围：学校「{scope_school}」年级「{scope_grade}」。"
                   f"请到「学生」页把学生的学校和年级改成一致（一对一学生也会一起进来）。")
        return
    info = teach_map(stu["id"].tolist())
    olds = query("SELECT * FROM scores WHERE exam_id=?", (eid,))
    old_map = {int(r["student_id"]): r for _, r in olds.iterrows()}
    rows = []
    for _, s in stu.iterrows():
        sid = int(s["id"])
        o = old_map.get(sid)
        val = None
        if o is not None and pd.notna(row_val(o, "score", None)):
            try:
                val = float(o["score"])
            except Exception:
                val = None
        rows.append({"学生": s["name"], "教学类型": info.get(sid, {}).get("kind", "—"),
                     "班级": info.get(sid, {}).get("class", "—"), "成绩": val,
                     "状态": score_state(o),
                     "答题卡": ("已传" if row_val(o, "sheet", "") and str(row_val(o, "sheet", "")).strip() else "—"),
                     "_sid": sid})
    df = pd.DataFrame(rows).set_index("_sid")
    st.subheader("录入成绩")
    st.caption(f"名单范围：学校「{scope_school}」· 年级「{scope_grade}」· 共 {len(df)} 人。"
               "未录入 = 还没录（不是 0 分）；已录入 = 填了分数；缺考 = 确认没参加。")
    ed = st.data_editor(
        df, hide_index=True, use_container_width=True, key=f"entry{eid}",
        disabled=["学生", "教学类型", "班级", "答题卡"],
        column_config={
            "成绩": st.column_config.NumberColumn(min_value=0.0, max_value=full, step=0.5, help=f"满分 {full:g}"),
            "状态": st.column_config.SelectboxColumn(options=["未录入", "已录入", "缺考"]),
        },
    )
    st.caption("这个按钮只保存分数和状态；试卷分析、错题、知识点在下面的「单个学生详细录入」里单独保存。")
    if st.button("💾 保存成绩（仅分数/状态）", type="primary", key=f"save{eid}"):
        changes = [{"sid": int(sid), "status": str(r["状态"] or "未录入").strip(),
                    "score": (None if pd.isna(r["成绩"]) else float(r["成绩"]))}
                   for sid, r in ed.iterrows()]
        saved, absent, cleared, skipped = save_entry_changes(eid, changes, old_map)
        msg = f"已录入 {saved} 人，缺考 {absent} 人，清空 {cleared} 人"
        if skipped:
            msg += f"；{skipped} 人状态是「已录入」但没填分数，已跳过"
        st.session_state["entry_msg"] = msg + "（只保存了分数和状态；试卷分析请在下面单个学生里保存）"
        st.rerun()

    st.divider()
    with st.expander("单个学生详细录入（错题 / 错因 / 知识点 / 试卷和答题卡照片）"):
        sid = st.selectbox("学生", stu.id.tolist(), format_func=dict(zip(stu.id, stu["name"])).get,
                           key=f"detail{eid}")
        old = query("SELECT * FROM scores WHERE student_id=? AND exam_id=?", (int(sid), eid))
        o = old.iloc[0] if len(old) else None
        quick_fill_box(int(sid), eid)
        cur_score = float(row_val(o, "score", 0.0)) if (o is not None and pd.notna(row_val(o, "score", None))) else 0.0
        score = st.number_input(f"得分(满分 {full:g})", 0.0, full, cur_score, 0.5, key=f"sc{sid}{eid}")
        wrong = st.text_input("错题题号(如 5,12,18)", row_val(o, "wrong_qs", "") if o is not None else "", key=f"w{sid}{eid}")
        old_rs = [x.strip() for x in str(row_val(o, "reasons", "")).split(",") if x.strip()] if o is not None else []
        sel_rs = [x for x in old_rs if x in REASONS]
        custom_def = ",".join([x for x in old_rs if x not in REASONS])
        rkey, ckey = f"r{sid}{eid}", f"c{sid}{eid}"
        if rkey in st.session_state:
            reasons = st.multiselect("错因", REASONS, key=rkey)
        else:
            reasons = st.multiselect("错因", REASONS, sel_rs, key=rkey)
        if ckey in st.session_state:
            custom = st.text_input("自定义错因(可选，多个用逗号分隔)", key=ckey)
        else:
            custom = st.text_input("自定义错因(可选，多个用逗号分隔)", custom_def, key=ckey)
        knowledge = st.text_input("涉及知识点", row_val(o, "knowledge", "") if o is not None else "", key=f"k{sid}{eid}")
        st.markdown("**这场考试的试卷（所有人共用）**")
        exam_paper = str(row_val(ex, "paper", "") or "").strip()
        if exam_paper:
            show_photos(exam_paper, "考试试卷")
        else:
            st.caption("这场考试还没传共用试卷 —— 到「考试成绩 → 考试管理」里传一次就行。")
        paper = st.file_uploader("这个学生单独的试卷(一般不用传)", type=["jpg", "jpeg", "png", "pdf"], accept_multiple_files=True, key=f"p{sid}{eid}")
        cur_stu_paper = str(row_val(o, "paper", "") or "").strip()
        if cur_stu_paper:
            st.caption(f"✅ 这个学生单独传过试卷：{file_count(cur_stu_paper)} 个文件")
            show_photos(cur_stu_paper, "该学生单独的试卷")
        sheet = st.file_uploader("答题卡照片/PDF(可多张)", type=["jpg", "jpeg", "png", "pdf"], accept_multiple_files=True, key=f"s{sid}{eid}")
        cur_sheet = str(row_val(o, "sheet", "") or "").strip()
        if cur_sheet:
            st.caption(f"✅ 已保存答题卡 {file_count(cur_sheet)} 个文件（下面就是已上传的，可点开看/下载）")
            show_photos(cur_sheet, "已上传的答题卡")
        else:
            st.caption("这个学生还没有答题卡。选好文件后，记得点下面的保存按钮。")
        note = st.text_area("试卷分析 / 备注（学生也能看到，可整段粘贴）",
                            row_val(o, "note", "") if o is not None else "", height=150,
                            key=f"nt{sid}{eid}")
        st.caption("⚠️ 改完（尤其是试卷分析）要点下面的按钮保存。上面那个「保存成绩」只保存分数和状态。")
        if st.button("💾 保存这个学生（分数 / 错题 / 分析）", type="primary", key=f"dsc{sid}{eid}"):
            tag = f"e{eid}_s{sid}"
            p, s = save_photos(paper, tag + "_paper"), save_photos(sheet, tag + "_sheet")
            rs = list(reasons) + [x.strip() for x in str(custom or "").split(",") if x.strip()]
            reasons_joined = ",".join(rs)
            if o is None:
                run("""INSERT INTO scores(student_id, exam_id, score, status, wrong_qs, reasons, knowledge, paper, sheet, note)
                       VALUES(?,?,?,'已录入',?,?,?,?,?,?)""",
                    (int(sid), eid, score, wrong, reasons_joined, knowledge, p, s, note))
            else:
                run("""UPDATE scores SET score=?, status='已录入', wrong_qs=?, reasons=?, knowledge=?, note=?,
                       paper=CASE WHEN ?='' THEN paper ELSE ? END, sheet=CASE WHEN ?='' THEN sheet ELSE ? END
                       WHERE id=?""",
                    (score, wrong, reasons_joined, knowledge, note, p, p, s, s, int(o["id"])))
            _stu_name = str(stu[stu["id"] == int(sid)]["name"].iloc[0])
            _got = ["分数", "错题", "分析"]
            if p:
                _got.append("试卷")
            if s:
                _got.append("答题卡")
            st.session_state["entry_msg"] = f"已保存 ✅「{_stu_name}」的" + "、".join(_got) + "都存好了；学生登录后刷新就能看到。"
            st.rerun()


def tab_analysis():
    ex = pick_exam("an_exam")
    if ex is None:
        return
    eid, full = int(ex["id"]), float(ex["full_score"])
    raw = query("""SELECT s.id sid, s.name 姓名, sc.score 得分, COALESCE(sc.status,'') status
                   FROM scores sc JOIN students s ON s.id=sc.student_id WHERE sc.exam_id=?""", (eid,))
    if raw.empty:
        st.info("这场考试还没有成绩")
        return
    absent_n = int((raw["status"] == "缺考").sum())
    df = raw[(raw["status"] != "缺考") & raw["得分"].notna()].copy()
    if df.empty:
        st.info("这场考试还没有录入任何分数（缺考不计入统计）。")
        return
    info = teach_map(df["sid"].tolist())
    df["教学类型"] = [info.get(int(x), {}).get("kind", "—") for x in df["sid"]]
    df["班级"] = [info.get(int(x), {}).get("class", "—") for x in df["sid"]]
    df["得分率"] = (df["得分"] / full * 100).round(1)
    df["排名"] = df["得分"].rank(ascending=False, method="min").astype(int)
    prev = query("""SELECT sc.student_id sid, sc.score / e.full_score * 100 prate FROM scores sc
                    JOIN exams e ON e.id=sc.exam_id
                    WHERE e.subject=? AND sc.score IS NOT NULL AND (e.exam_date<? OR (e.exam_date=? AND e.id<?))
                    ORDER BY e.exam_date, e.id""", (ex["subject"], ex["exam_date"], ex["exam_date"], eid)).drop_duplicates("sid", keep="last")
    df = df.merge(prev, on="sid", how="left")
    df["较上次(得分率)"] = (df["得分率"] - df["prate"]).round(1)
    delta = df["得分率"].mean() - df["prate"].mean() if df["prate"].notna().any() else None
    m = st.columns(4)
    m[0].metric("平均分", f"{df['得分'].mean():.1f}", f"{delta:+.1f}% 较上次" if delta is not None and pd.notna(delta) else None)
    m[1].metric("最高分", f"{df['得分'].max():g}")
    m[2].metric("最低分", f"{df['得分'].min():g}")
    m[3].metric("参考人数", len(df))
    if absent_n:
        st.caption(f"另有 {absent_n} 人标记为缺考（不计入平均分）。")
    st.dataframe(df.sort_values("排名")[["排名", "姓名", "教学类型", "班级", "得分", "得分率", "较上次(得分率)"]],
                 hide_index=True, use_container_width=True)
    st.bar_chart(df.set_index("姓名")["得分"])


def tab_student():
    stu = query("""SELECT s.id, s.name, COALESCE(s.school,'') school, COALESCE(s.grade,'') grade
                   FROM students s ORDER BY s.name""")
    if stu.empty:
        st.info("还没有学生")
        return
    info = teach_map(stu["id"].tolist())
    stu = stu.copy()
    stu["教学类型"] = [info.get(int(x), {}).get("kind", "—") for x in stu["id"]]
    schools = sorted({str(x) for x in stu["school"] if str(x).strip()})
    grades = sorted({str(x) for x in stu["grade"] if str(x).strip()})
    c1, c2, c3 = st.columns(3)
    sch = c1.selectbox("学校", ["全部"] + schools, key="rep_school")
    grd = c2.selectbox("年级", ["全部"] + grades, key="rep_grade")
    kind = c3.selectbox("教学类型", ["全部", "班课", "一对一"], key="rep_kind")
    view = stu
    if sch != "全部":
        view = view[view["school"] == sch]
    if grd != "全部":
        view = view[view["grade"] == grd]
    if kind != "全部":
        view = view[view["教学类型"].str.contains(kind, regex=False)]
    if view.empty:
        st.info("这个筛选条件下没有学生")
        return
    sid = st.selectbox("学生", view.id.tolist(), format_func=dict(zip(view.id, view["name"])).get, key="rep_stu")
    student_report(sid)


def tab_manage():
    cls = query("SELECT id, name FROM classes ORDER BY name")
    cls_map = dict(zip(cls.id, cls.name)) if not cls.empty else {}
    cls_ids = cls.id.tolist() if not cls.empty else []

    msg = st.session_state.pop("exam_msg", None)
    if msg:
        st.success(msg)

    cat_names = query("SELECT name FROM exam_categories ORDER BY sort, id")["name"].tolist()

    # ---------- 分类管理 ----------
    with st.expander("🏷 考试分类（期中 / 期末 / 月考 … 可以自己加）"):
        c1, c2 = st.columns([3, 1])
        new_cat = c1.text_input("新增分类", placeholder="例如：单元测", key="new_cat_name", label_visibility="collapsed")
        if c2.button("添加分类", key="add_cat_btn"):
            nm = (new_cat or "").strip()
            if not nm:
                st.warning("先填分类名称")
            elif nm in cat_names:
                st.warning("这个分类已经存在")
            else:
                nxt = int(query("SELECT COALESCE(MAX(sort),0)+1 n FROM exam_categories").n[0])
                run("INSERT INTO exam_categories(name, sort) VALUES(?,?)", (nm, nxt))
                st.rerun()
        if cat_names:
            st.caption("现在有：" + "、".join(cat_names))
            d1, d2 = st.columns([3, 1])
            del_cat = d1.selectbox("要删除的分类", cat_names, key="del_cat_name", label_visibility="collapsed")
            if d2.button("删除分类", key="del_cat_btn"):
                used = int(query("SELECT COUNT(*) n FROM exams WHERE COALESCE(category,'')=?", (del_cat,)).n[0])
                if used:
                    st.error(f"还有 {used} 场考试属于「{del_cat}」，请先把它们改成别的分类")
                else:
                    run("DELETE FROM exam_categories WHERE name=?", (del_cat,))
                    st.rerun()

    # ---------- 新增考试 ----------
    with st.form("new_exam"):
        c1, c2, c3 = st.columns(3)
        n = c1.text_input("考试名称")
        d = c2.date_input("日期", date.today())
        s = c3.selectbox("科目", SUBJECTS)
        c6, c7 = st.columns(2)
        school = c6.text_input("学校", placeholder="例如：XX中学")
        grade = c7.text_input("年级", placeholder="例如：初三")
        c4, c5 = st.columns(2)
        cat = c4.selectbox("分类", [""] + cat_names, format_func=lambda x: x or "未分类")
        f = c5.number_input("满分", 1.0, 300.0, 100.0, 5.0)
        c = st.selectbox("所属班级(可不选)", [0] + cls_ids,
                         format_func=lambda i: "不限班级" if i == 0 else cls_map.get(i, str(i)))
        paper_new = st.file_uploader("试卷照片/PDF（这场考试共用，传一次就行，可留空）",
                                     type=["jpg", "jpeg", "png", "pdf"], accept_multiple_files=True)
        st.caption("填了学校和年级后，录分名单=该学校该年级的全部学生（班课 + 一对一都会进来）。")
        if st.form_submit_button("添加考试", type="primary") and n.strip():
            new_eid = run("""INSERT INTO exams(name, exam_date, subject, full_score, class_id, category, school, grade)
                             VALUES(?,?,?,?,?,?,?,?)""",
                          (n.strip(), str(d), s, f, c or None, cat, school.strip(), grade.strip()))
            if paper_new:
                names = save_photos(paper_new, f"e{int(new_eid)}_exampaper")
                run("UPDATE exams SET paper=? WHERE id=?", (names, int(new_eid)))
            st.session_state["exam_msg"] = f"已添加考试「{n.strip()}」"
            st.rerun()

    # ---------- 列表 + 筛选 ----------
    all_ex = query("""SELECT e.id, e.exam_date, e.name, e.subject, COALESCE(e.category,'') category,
                             e.full_score, COALESCE(e.school,'') school, COALESCE(e.grade,'') grade,
                             CASE WHEN COALESCE(e.paper,'')<>'' THEN '有' ELSE '—' END AS 试卷,
                             COALESCE(c.name,'') cname
                      FROM exams e LEFT JOIN classes c ON c.id=e.class_id
                      ORDER BY e.exam_date DESC, e.id DESC""")
    if all_ex.empty:
        st.info("还没有考试记录，先在上面添加。")
        return
    f1, f2 = st.columns(2)
    subs = ["全部"] + sorted({str(x) for x in all_ex["subject"].fillna("") if str(x).strip()})
    catf = ["全部"] + sorted({str(x) for x in all_ex["category"].fillna("") if str(x).strip()})
    sub_f = f1.selectbox("按科目看", subs, key="mng_sub")
    cat_f = f2.selectbox("按分类看", catf, key="mng_cat")
    view = all_ex
    if sub_f != "全部":
        view = view[view["subject"].fillna("") == sub_f]
    if cat_f != "全部":
        view = view[view["category"].fillna("") == cat_f]
    show = view.copy()
    show["分类"] = show["category"].replace("", "未分类")
    st.dataframe(
        show[["exam_date", "name", "school", "grade", "subject", "分类", "full_score", "试卷", "cname"]].rename(
            columns={"exam_date": "日期", "name": "考试", "school": "学校", "grade": "年级",
                     "subject": "科目", "full_score": "满分", "cname": "班级"}),
        hide_index=True, use_container_width=True,
    )
    if view.empty:
        st.info("这个筛选条件下没有考试，换个科目或分类试试。")
        return

    # ---------- 修改 / 删除 ----------
    st.subheader("修改或删除考试")
    lab = labels(view, lambda r: f"{r['exam_date']} {r['name']} ({r['subject']} · {r['category'] or '未分类'})")
    pick = st.selectbox("选择考试", list(lab), format_func=lab.get, key="edit_exam_pick")
    ex = query("SELECT * FROM exams WHERE id=?", (int(pick),)).iloc[0]
    try:
        cur_date = pd.to_datetime(ex["exam_date"]).date()
    except Exception:
        cur_date = date.today()
    cls_opts = [0] + cls_ids
    cur_cls = int(ex["class_id"]) if pd.notna(ex["class_id"]) else 0
    cur_subj = str(ex["subject"] or "")
    subj_opts = SUBJECTS if cur_subj in SUBJECTS else SUBJECTS + [cur_subj]
    cur_paper = str(row_val(ex, "paper", "") or "").strip()
    st.markdown("**这场考试的试卷（所有学生共用，传一次就行）**")
    if cur_paper:
        show_photos(cur_paper, "共用试卷")
    else:
        st.caption("还没上传共用试卷。上传后，这个学校/年级的学生在自己账号里就能看到这张卷子。")
    with st.form(f"edit_exam_{pick}"):
        c1, c2, c3 = st.columns(3)
        n2 = c1.text_input("考试名称", str(ex["name"]))
        d2 = c2.date_input("日期", cur_date)
        s2 = c3.selectbox("科目", subj_opts, index=subj_opts.index(cur_subj))
        c6, c7 = st.columns(2)
        school2 = c6.text_input("学校", str(ex["school"] or "") if "school" in ex.index else "")
        grade2 = c7.text_input("年级", str(ex["grade"] or "") if "grade" in ex.index else "")
        c4, c5 = st.columns(2)
        cat_opts = [""] + cat_names
        cur_cat = str(ex["category"] or "")
        cat2 = c4.selectbox("分类", cat_opts, index=cat_opts.index(cur_cat) if cur_cat in cat_opts else 0,
                            format_func=lambda x: x or "未分类")
        f2v = c5.number_input("满分", 1.0, 300.0, float(ex["full_score"] or 100), 5.0)
        c2v = st.selectbox("所属班级", cls_opts, index=cls_opts.index(cur_cls) if cur_cls in cls_opts else 0,
                           format_func=lambda i: "不限班级" if i == 0 else cls_map.get(i, str(i)))
        paper_add = st.file_uploader("追加试卷照片/PDF（可多张）", type=["jpg", "jpeg", "png", "pdf"],
                                     accept_multiple_files=True, key=f"exam_paper_{pick}")
        b1, b4, b2 = st.columns(3)
        if b1.form_submit_button("💾 保存修改", type="primary"):
            run("""UPDATE exams SET name=?, exam_date=?, subject=?, full_score=?, class_id=?, category=?, school=?, grade=?
                   WHERE id=?""",
                (n2.strip(), str(d2), s2, f2v, c2v or None, cat2, school2.strip(), grade2.strip(), int(pick)))
            if paper_add:
                added = save_photos(paper_add, f"e{int(pick)}_exampaper")
                run("UPDATE exams SET paper=? WHERE id=?", ((cur_paper + "|" + added).strip("|"), int(pick)))
            st.session_state["exam_msg"] = f"考试「{n2.strip()}」已保存"
            st.rerun()
        if b4.form_submit_button("🗑 清空共用试卷"):
            run("UPDATE exams SET paper='' WHERE id=?", (int(pick),))
            st.session_state["exam_msg"] = f"已清空「{ex['name']}」的共用试卷"
            st.rerun()
        if b2.form_submit_button("🗑 删除这场考试"):
            cnt = int(query("SELECT COUNT(*) n FROM scores WHERE exam_id=?", (int(pick),)).n[0])
            if cnt:
                st.error(f"这场考试已经有 {cnt} 条成绩，请先清掉成绩再删考试")
            else:
                run("DELETE FROM exams WHERE id=?", (int(pick),))
                st.session_state["exam_msg"] = f"考试「{ex['name']}」已删除"
                st.rerun()


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

    st.caption("所有页面都在侧边栏「导航」里：手机点左上角「>」即可展开。")

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
            on_click=lambda: st.session_state.update(page="课程表"),
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
    NAV = dict(PAGES)
    if cm.is_mobile():
        NAV["我的"] = page_me          # 待办提醒 / 课时预警 / 本月速览
else:
    NAV = dict(PAGES)

page = st.sidebar.radio("导航", list(NAV), key="page")
NAV[page]()
