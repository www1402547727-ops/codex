# 项目约定（给 AI 助手看）

## 这是什么
「教务工作台」——一位独立老师自己用的教务系统（约 30 个学生）。
技术栈：Streamlit(Python) + SQLite + Supabase（数据库与图片云存储）+ 自建账号系统。

- 线上地址：https://7ealebswmykj7itawfr7kh.streamlit.app/
- 部署方式：push 到 main 分支，Streamlit Cloud 约 2 分钟自动部署
- 仓库：git@github.com:www1402547727-ops/codex.git

## 铁律（务必遵守）
1. **不要把 `data/scores.db`、`.streamlit/secrets.toml`、任何密钥提交到 git**（仓库可能是公开的）
2. **不要删表、删字段**（尤其 `exams.class_id`、`scores`）；数据库迁移只能「加字段 + 回填 + 可重复执行」
3. 老师的密码、Supabase 的 `service_role` key **不要明文出现在对话/回复里**
4. **界面文案不要出现 "AI"、"GPT"、"智能粘贴" 字样**（用户明确要求，改成「快速填写」这类中性说法）
5. 手机端导航统一用**侧边栏**（不要做底部导航条）
6. **不新增第三方依赖**：requirements.txt 只有 streamlit / pandas / pillow / requests
7. 改完必须：`python -m py_compile` 通过 → 再把项目和数据**复制到临时目录**跑一遍页面，不要拿正式数据库做测试

## 文件分工
| 文件 | 负责 |
|---|---|
| `app.py` | 成绩模块、学生页、登录与导航、快速填写 |
| `course_management.py` | 课程表、班级/一对一、授课统计、按地点结算 |
| `user_management.py` | 账号、登录、学生端页面 |
| `cloud_store.py` | Supabase 同步（数据库快照、图片上传下载） |
| `ui_theme.py` | 全局配色与手机端 CSS |

## 提交习惯
- 一次只做一件事，中文 commit message 写清改了什么
- push 后等约 2 分钟，打开线上地址确认
- 用户不懂代码：回复用中文、说人话，改完告诉他「刷新页面后怎么看」
