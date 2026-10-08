"""教务工作台的统一界面样式。

所有颜色、圆角、间距都集中在这一个文件里,改风格只改这里。
app.py 启动时调用一次 inject(),其他页面不用再各自写 <style>。

配色思路:黑板绿(主色/已完成) + 粉笔黄(待处理) + 墨色(今天)。
绿 = 已完成/可点的主操作,黄 = 还需要你处理,灰 = 请假/取消。
字体只用系统字体(国内访问不到 Google Fonts)。
"""
from __future__ import annotations

import streamlit as st

CSS = """
:root {
  --ink: #1C2723;
  --muted: #5E6B66;
  --faint: #8D9893;
  --line: #DDE3DF;
  --hair: #EBEFEC;
  --paper: #F4F6F5;
  --card: #FFFFFF;
  --green: #1F5C4D;
  --green-dark: #174A3E;
  --green-soft: #E3EEE9;
  --chalk: #E0A82E;
  --chalk-soft: #FBF1D6;
  --chalk-ink: #6E4F08;
  --grey: #9AA39F;
  --grey-soft: #EDF0EE;
  --radius: 12px;
}

/* ---------- 全局 ---------- */
.stApp {
  background: var(--paper);
  color: var(--ink);
  font-family: "PingFang SC", "HarmonyOS Sans SC", "Microsoft YaHei UI", "Microsoft YaHei",
               "Noto Sans CJK SC", "Noto Sans SC", system-ui, -apple-system, "Segoe UI", sans-serif;
  font-variant-numeric: tabular-nums;
}
.stApp input, .stApp textarea, .stApp button { font-family: inherit; }
#MainMenu, footer, [data-testid="stDecoration"] { display: none !important; }
header[data-testid="stHeader"] { background: transparent; }
.block-container { padding-top: 2.2rem; max-width: 1180px; }

h1, [data-testid="stHeading"] h1 { font-size: 1.7rem !important; font-weight: 700 !important; letter-spacing: 0 !important; line-height: 1.25 !important; padding: .2rem 0 .1rem !important; }
h2, [data-testid="stHeading"] h2 { font-size: 1.25rem !important; font-weight: 700 !important; letter-spacing: 0 !important; }
h3, h4, [data-testid="stHeading"] h3, [data-testid="stHeading"] h4 { font-size: 1.05rem !important; font-weight: 650 !important; letter-spacing: 0 !important; }
[data-testid="stCaptionContainer"] { color: var(--muted); }
hr { border-color: var(--line) !important; margin: 1.1rem 0 !important; }

/* ---------- 按钮 ---------- */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
  border-radius: 10px; font-weight: 600; min-height: 2.5rem;
  border: 1px solid var(--line); background: var(--card); color: var(--ink);
  transition: border-color .12s, background-color .12s, color .12s;
}
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {
  border-color: var(--green); color: var(--green); background: var(--card);
}
.stButton > button:focus-visible, .stFormSubmitButton > button:focus-visible { outline: 3px solid rgba(31,92,77,.35); outline-offset: 1px; }
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"], .stFormSubmitButton > button[kind="primaryFormSubmit"] {
  background: var(--green); border-color: var(--green); color: #fff;
}
.stButton > button[kind="primary"]:hover, .stFormSubmitButton > button[kind="primary"]:hover, .stFormSubmitButton > button[kind="primaryFormSubmit"]:hover {
  background: var(--green-dark); border-color: var(--green-dark); color: #fff;
}

/* ---------- 容器 / 输入 / 提示 ---------- */
div[data-testid="stVerticalBlockBorderWrapper"] { border-radius: var(--radius) !important; border-color: var(--line) !important; background: var(--card); }
[data-testid="stExpander"] { border-radius: var(--radius); border: 1px solid var(--line); background: var(--card); }
[data-testid="stExpander"] details { border: none; }
[data-testid="stMetric"] { background: var(--card); border: 1px solid var(--line); border-radius: var(--radius); padding: 12px 16px; }
[data-testid="stMetricLabel"] { color: var(--muted); }
[data-testid="stMetricValue"] { font-weight: 700; }
[data-testid="stAlert"] { border-radius: 10px; }
[data-testid="stImage"] img { border-radius: 10px; border: 1px solid var(--line); }
[data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div { border-radius: 10px !important; }
button[role="tab"] { font-weight: 600; }
div[role="dialog"] { border-radius: 16px; }
[data-testid="stSidebar"] { background: var(--card); border-right: 1px solid var(--line); }
[data-testid="stSidebar"] [role="radiogroup"] label { padding: .45rem .7rem; border-radius: 10px; width: 100%; }
[data-testid="stSidebar"] [role="radiogroup"] label:has(input:checked) { background: var(--green-soft); color: var(--green); font-weight: 650; }

/* ---------- 课程条目(整个界面的视觉重点) ----------
   一节课 = 一张卡:左侧色条表示状态,第一行是「粗体开始时间 – 结束  班级 · 科目」,
   下面一排小标签。点第一行编辑,右侧绿色按钮记录上完课。 */
[class*="st-key-card_"] {
  background: var(--card); border: 1px solid var(--line); border-left-width: 5px;
  border-radius: var(--radius); padding: .5rem .8rem .65rem; gap: .1rem !important;
}
[class*="st-key-card_pending_"] { border-left-color: var(--chalk); }
[class*="st-key-card_done_"] { border-left-color: var(--green); background: #F9FBFA; }
[class*="st-key-card_off_"] { border-left-color: var(--grey); background: var(--grey-soft); }
[class*="st-key-card_off_"] button p { color: var(--muted); }
[class*="st-key-card_off_"] .chip { background: #E1E5E3; }

[class*="st-key-open_"] button {
  justify-content: flex-start !important; text-align: left !important; width: 100%;
  background: transparent !important; border: none !important; box-shadow: none !important;
  padding: .3rem .1rem !important; min-height: 2.3rem; font-weight: 500; color: var(--ink);
}
[class*="st-key-open_"] button:hover { background: rgba(31,92,77,.06) !important; color: var(--green); }
[class*="st-key-open_"] button p { text-align: left; margin: 0; font-size: 1.02rem; line-height: 1.35; }

/* 长标题要能折行:Streamlit 按钮默认 nowrap+hidden 会把「经二路数学班课 · 数学」切掉。
   允许换行后标题在窄屏上换成两行(时间一行、班级科目一行),右侧「已上课」按钮仍保持同一行。 */
[class*="st-key-open_"] button,
[class*="st-key-open_"] button * {
  white-space: normal !important;
  overflow: visible !important;
  text-overflow: clip !important;
  word-break: break-word !important;
}
[class*="st-key-open_"] button { height: auto !important; align-items: flex-start !important; }
[class*="st-key-open_"] button strong { font-weight: 750; font-size: 1.12em; }

.chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 0 .15rem; }
.chip { font-size: .78rem; line-height: 1; padding: 4px 9px; border-radius: 999px; background: var(--grey-soft); color: var(--muted); }
.chip.pending { background: var(--chalk-soft); color: var(--chalk-ink); }
.chip.done { background: var(--green-soft); color: var(--green); }
.chip.off { background: #E3E7E5; color: var(--muted); }

/* 学生端课程卡(纯 HTML) */
.srow { display: flex; gap: 14px; align-items: center; padding: .15rem 0; }
.srow .stime { min-width: 3.6rem; text-align: center; line-height: 1.15; }
.srow .stime b { display: block; font-size: 1.35rem; font-weight: 750; }
.srow .stime span { font-size: .8rem; color: var(--muted); }
.srow .sname { font-weight: 650; font-size: 1.02rem; margin-bottom: 6px; }
.sdate { font-size: .82rem; color: var(--muted); margin: 14px 0 6px; font-weight: 600; }

/* 周视图每天一列(电脑) */
.dayhead { text-align: center; padding: 7px 4px; border-radius: 10px; background: var(--card); border: 1px solid var(--line); font-weight: 650; color: var(--muted); line-height: 1.3; }
.dayhead small { display: block; font-size: .82rem; font-weight: 500; }
.dayhead.today { background: var(--ink); border-color: var(--ink); color: #fff; }
[class*="st-key-week_"] button {
  justify-content: flex-start !important; text-align: left !important; white-space: pre-line !important;
  line-height: 1.35; min-height: 4.2rem; background: var(--card);
  border-left: 4px solid var(--chalk) !important; font-weight: 500;
}
[class*="st-key-week_"] button p { text-align: left; font-size: .86rem; margin: 0; }

/* 电脑版「本周按天」那 7 列很窄(每个约 97px),同样要允许折行,否则显示成「13:30 经...」 */
[class*="st-key-week_"] button,
[class*="st-key-week_"] button * {
  white-space: pre-line !important;
  overflow: visible !important;
  text-overflow: clip !important;
  word-break: break-word !important;
}
[class*="st-key-week_"] button { height: auto !important; align-items: flex-start !important; }
[class*="st-key-week_"] button p { line-height: 1.3 !important; }

/* ---------- 周课表网格 ---------- */
table.wk2 { width: 100%; border-collapse: separate; border-spacing: 0; table-layout: fixed; font-size: 12px;
  background: var(--card); border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; }
table.wk2 th { padding: 8px 2px; background: var(--card); border-bottom: 1px solid var(--line); font-weight: 600; color: var(--muted); text-align: center; line-height: 1.3; }
table.wk2 th.today { background: var(--ink); color: #fff; }
table.wk2 td { height: 22px; padding: 1px 5px; border-top: 1px solid var(--hair); border-left: 1px solid var(--hair);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; vertical-align: middle; }
table.wk2 td.tm { width: 48px; border: none; text-align: right; color: var(--faint); padding-right: 6px; background: transparent; }
table.wk2 td.free { background: var(--card); }
table.wk2 td.tdy { background: #F7F9F8; }
table.wk2 td.busy { background: var(--chalk-soft); color: var(--chalk-ink); font-weight: 600; border-left: 3px solid var(--chalk); }
table.wk2 td.done { background: var(--green-soft); color: var(--green); font-weight: 600; border-left: 3px solid var(--green); }

/* ---------- 月历 ---------- */
table.month2 { width: 100%; border-collapse: separate; border-spacing: 4px; table-layout: fixed; font-size: 12px; }
table.month2 th { padding: 7px 2px; background: transparent; color: var(--muted); font-weight: 600; text-align: center; }
table.month2 td { height: 104px; padding: 6px 7px; vertical-align: top; background: var(--card); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
table.month2 td.today { border: 2px solid var(--ink); }
table.month2 td.outside { background: transparent; border-color: transparent; color: #C4CBC7; }
table.month2 .daynum { font-weight: 750; margin-bottom: 3px; }
table.month2 td.today .daynum { color: var(--ink); }
table.month2 .mini { display: block; color: var(--chalk-ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
table.month2 .mini.done { color: var(--green); }
.tiny-note { color: var(--faint); font-size: .78rem; }

/* ---------- 手机底部导航 + 「我的」页 ---------- */
.st-key-bottomnav { position: fixed; left: 0; right: 0; bottom: 0; z-index: 1000;
  background: var(--card); border-top: 1px solid var(--line);
  padding: 6px 8px calc(8px + env(safe-area-inset-bottom)); }
.st-key-bottomnav .stElementContainer, .st-key-bottomnav [data-testid="stButtonGroup"], .st-key-bottomnav [role="radiogroup"] { width: 100% !important; }
.st-key-bottomnav [role="radiogroup"], .st-key-bottomnav [data-testid="stButtonGroup"] { display: flex !important; gap: 4px !important; }
.st-key-bottomnav button { flex: 1 1 0 !important; min-width: 0 !important; font-size: .9rem !important;
  min-height: 3rem !important; padding: 10px 2px !important; justify-content: center !important;
  border: none !important; background: transparent !important; color: var(--muted) !important; border-radius: 10px !important; }
.st-key-bottomnav button p { font-size: .9rem !important; line-height: 1.2 !important; }
.st-key-bottomnav button[kind$="Active"], .st-key-bottomnav button[aria-checked="true"], .st-key-bottomnav button[aria-pressed="true"] {
  background: var(--green-soft) !important; color: var(--green) !important; font-weight: 700 !important; }
[class*="me_quick"] [data-testid="stHorizontalBlock"], [class*="me_month"] [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; width: 100% !important; }
[class*="me_quick"] [data-testid="stColumn"], [class*="me_month"] [data-testid="stColumn"] { min-width: 0 !important; flex: 1 1 0 !important; width: auto !important; }
[class*="me_quick"] .stElementContainer, [class*="me_month"] .stElementContainer, [class*="me_quick"] button, [class*="me_month"] button { width: 100% !important; min-width: 0 !important; }

/* 手机每日分组标题 + 今日统计 */
.mday { font-weight: 700; font-size: 1rem; margin: 16px 0 8px; padding: 8px 12px; border-radius: 10px; background: var(--card); border: 1px solid var(--line); color: var(--ink); }
.mday.today { background: var(--ink); border-color: var(--ink); color: #fff; }
.mempty { color: var(--faint); font-size: .85rem; margin: 0 0 6px 12px; }
.mstats { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin: 6px 0 12px; }
.mstat { background: var(--card); border: 1px solid var(--line); border-radius: var(--radius); padding: 10px 12px; }
.mstat span { display: block; color: var(--muted); font-size: .8rem; }
.mstat b { font-size: 1.4rem; line-height: 1.25; font-weight: 750; }

/* ---------- 登录页 ---------- */
.login-title { font-size: 1.6rem; font-weight: 750; margin: 0 0 .2rem; }
.login-sub { color: var(--muted); margin: 0 0 1.2rem; }

/* ---------- 手机 ---------- */
@media (max-width: 760px) {
  .block-container { padding: .6rem .8rem 7.5rem !important; }
  /* 手机底部导航放大：更好点 */
  .st-key-bottomnav { padding: 8px 8px calc(10px + env(safe-area-inset-bottom)); }
  .st-key-bottomnav button { min-height: 3.4rem !important; font-size: .95rem !important; }
  .st-key-bottomnav button p { font-size: .95rem !important; }
  [data-testid="stMainBlockContainer"] { padding-top: .4rem !important; }
  h1, [data-testid="stHeading"] h1 { font-size: 1.45rem !important; }
  h2, [data-testid="stHeading"] h2 { font-size: 1.15rem !important; }
  .stButton > button, .stFormSubmitButton > button { min-height: 2.8rem; font-size: .96rem; }
  [data-testid="stMetric"] { padding: 8px 10px; }
  [data-testid="stMetricValue"] { font-size: 1.3rem; }
  div[data-testid="stHorizontalBlock"] { gap: .45rem !important; }
  /* 课程卡里「标题 | 已上课」始终排成一行,不让 Streamlit 上下堆叠 */
  [class*="st-key-card_"] { padding: .35rem .6rem .55rem; }
  [class*="st-key-card_"] [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; align-items: center; }
  [class*="st-key-card_"] [data-testid="stColumn"] { min-width: 0 !important; flex: 1 1 0 !important; width: auto !important; }
  [class*="st-key-card_"] [data-testid="stColumn"]:last-child:not(:first-child) { flex: 0 0 6.2rem !important; width: 6.2rem !important; }
  [class*="st-key-open_"] button p { font-size: .98rem; }
  table.wk2.m { font-size: 10px; }
  table.wk2.m th { padding: 4px 1px; font-size: 10px; line-height: 1.15; }
  table.wk2.m th .d { font-size: 9px; font-weight: 500; opacity: .85; }
  table.wk2.m th .n { font-size: 9.5px; font-weight: 750; }
  table.wk2.m td { height: 20px; padding: 0 1px; font-size: 10.5px; text-align: center; }
  table.wk2.m td.tm { width: 40px; font-size: 9px; text-align: right; }
  table.month2.m { border-spacing: 3px; }
  table.month2.m td { height: 50px; padding: 3px 2px; text-align: center; }
  table.month2.m .daynum { margin-bottom: 1px; font-size: .82rem; }
  table.month2.m .mnum { color: var(--chalk-ink); font-weight: 750; font-size: .72rem; }
  table.month2.m .mdone { color: var(--green); font-size: .68rem; }
}

@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
"""


def inject() -> None:
    """把样式注入当前页面。每次脚本运行调用一次即可。"""
    st.markdown(f"<style>{CSS}</style>", unsafe_allow_html=True)
