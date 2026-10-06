# 教务工作台云端版

这是本地版教务工作台的独立云端测试版本。

## 云端结构

- Streamlit Community Cloud：运行网页
- Supabase Storage：保存 SQLite 数据库和照片/PDF
- GitHub：保存代码

## Streamlit Secrets

在 Streamlit Cloud 的 App settings -> Secrets 中填写：

```toml
SUPABASE_URL = "https://kohudbxgiafdhkjztnyp.supabase.co"
SUPABASE_SERVICE_ROLE_KEY = "请在这里填写 Supabase service_role key"
SUPABASE_BUCKET = "student-files"
SCORE_TRACKER_PASSWORD = "老师账号首次初始化密码"
```

不要提交 `.streamlit/secrets.toml`。

## 首次上传数据库

在本地开启 VPN 后，在项目目录运行：

```bash
python tools/upload_initial_database.py
```

按要求输入 Supabase URL、service role key 和 bucket。脚本会：

- 上传 `data/scores.db` 到 `database/scores.db`
- 上传 `data/photos` 中的文件到 `photos/`

## 运行方式

云端应用启动时会：

1. 从 Supabase 下载 `database/scores.db`
2. 使用本地 SQLite 逻辑运行
3. 每次修改后把最新数据库同步回 Supabase
4. 每天保留一份 `backups/YYYY-MM-DD/scores.db`
5. 照片和 PDF 上传到 Supabase Storage

## 注意

这个方案适合 30 人以内、并发很低时的免费云端测试。  
如果以后并发写入增加，建议迁移到正式 PostgreSQL 或独立云服务器。