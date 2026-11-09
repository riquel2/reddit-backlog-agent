# Reddit 赛道用户需求与愿望清单生成器

输入几个 Subreddit 和一组抱怨句式，自动抓取近一年的讨论与高赞评论，用大模型聚类成**可排期的功能愿望清单**，输出 Markdown 报告 + Excel 表 + CSV。

提供两种用法：**浏览器界面**（双击 `start_ui.bat`，零命令行）和**命令行**（适合批量、定时、接自动化）。

面向独立开发者、小产品团队、做立项调研的人：在写第一行代码之前，先看看这个赛道的人到底在骂什么、愿意为什么付钱。

```
采集（PRAW）→ 清洗去噪 → AI 分批抽取 → 引文回查 → 跨批归并 → 加权打分 → 报告
```

---

## 一、它产出什么

一次运行生成 4 个文件（默认在 `reports/`）：

| 文件 | 用途 |
|---|---|
| `*_report.md` | 主报告：执行摘要、优先级矩阵、Top 10 深挖、用户原话墙、方法与局限 |
| `*_backlog.xlsx` | 5 个 Sheet：Overview / Summary / Backlog / Quotes / Raw Signals，可直接拿去排期 |
| `*_backlog.csv` | 给 BI、飞书多维表、Notion 数据库导入（UTF-8 BOM，Excel 打开不乱码） |
| `*_analysis.json` | 结构化快照，便于二次处理或对比历史批次 |

Backlog 表的核心字段：

| 字段 | 含义 |
|---|---|
| `feature_name` | 英文功能名，如 `Bulk CSV Import` |
| `pain_description` | 中文一句话说明具体痛点 |
| `demand_score` | 1-10 需求强度（语义分 × 0.65 + 证据分 × 0.35） |
| `Priority` | P0 / P1 / P2 / P3 |
| `verbatim_quotes` | 用户英文原话，已逐条回查原文，可点链接复核 |
| `intent` | 吐槽 / 求方案 / 明确要功能 / 愿意付费 / 准备换工具 |

---

## 二、目录结构

```text
reddit_backlog_agent/
├── app.py               # 可视化界面（Streamlit）：配置、进度、看板、下载
├── start_ui.bat         # Windows 双击启动界面（自动准备环境 + 端口顺延）
├── start_cli.bat        # Windows 命令行入口：start_cli.bat --demo
├── _env.bat             # 上面两个 bat 共用的环境探测/安装逻辑
├── config.py            # 配置：环境变量、CLI 参数、预置板块与关键词、校验
├── scraper.py           # PRAW 采集：过滤、评论提取、文本清洗、缓存续跑
├── analyzer.py          # AI 分析：分批抽取、JSON 容错、归并、打分、规则降级
├── reporter.py          # 报告：Markdown / Excel / CSV / JSON
├── main.py              # 命令行主控：串联流程、日志、退出码
├── requirements.txt     # 依赖（pip 用这个）
├── requirements.json    # 同一份依赖的机器可读版本
├── .env.example         # 配置模板
├── .streamlit/config.toml  # 界面主题与运行参数
├── tests/
│   ├── fixtures/sample_raw.json     # 15 篇合成样本，用于离线演示
│   ├── test_offline_pipeline.py     # 20 个离线测试（不访问网络）
│   └── test_ui_smoke.py             # 4 个界面冒烟测试（AppTest，不联网）
├── docs/
│   ├── samples/sample_report.md          # 样例报告（合成数据，用于展示产出格式）
│   ├── samples/sample_report.pdf         # 同一份的 PDF，可直接作为申请附件上传
│   └── reddit-data-access-request.md    # Data Access Request 逐栏英文答复（可直接粘贴）
├── data/                # 运行后生成：raw_data.json + cache/
└── reports/             # 运行后生成：报告产物 + run.log
```

---

## 三、可视化界面（推荐）

不想碰命令行就用界面。**Windows 直接双击 `start_ui.bat`**——它会自己确认环境、装好依赖、找空闲端口，
你只需要等窗口里打印出那句话然后点开浏览器。其他系统：

```bash
python -m streamlit run app.py
```

浏览器会自动打开 `http://localhost:8501`，界面分四页：

| 页面 | 内容 |
|---|---|
| ① 配置任务 | 选板块 / 信号词 / 时间范围，侧边栏填凭证，一键运行，实时显示采集与分析进度 |
| ② 分析看板 | 指标卡、执行摘要、优先级矩阵散点图、痛点类别分布、可筛选的愿望清单、用户原话 |
| ③ 报告与导出 | 四种格式一键下载、正文在线预览、历史运行记录 |
| ④ 使用说明 | OAuth 说明、凭证申请步骤、参数建议、常见问题 |

三点说明：

- 界面与命令行共用同一套模块（`scraper.py` / `analyzer.py` / `reporter.py`），数据目录与报告目录也相同，可以界面抓、命令行重跑分析，反之亦然。
- 没有凭证也能先看效果：运行方式选「内置示例数据」，用内置样本跑通全流程。
- 凭证填在左侧边栏，点「保存到 .env」会写入本机 `.env`（明文，已被 `.gitignore` 忽略）。

### 要不要提供 OAuth？

**不需要用户 OAuth 授权。** 本工具只读公开帖子，不发帖、不点赞、不涉及账号身份，所以没有浏览器跳转、没有授权码回调，也不用交出账号密码。

| 场景 | 需要什么 |
|---|---|
| 只读抓取（本项目用法） | 在 Reddit 后台创建 app，拿到 `client_id` + `client_secret` + 自定 `user_agent`；PRAW 在后台自动完成 application-only OAuth（client_credentials 流程） |
| 以你的账号发帖 / 评论 / 点赞 / 删帖 | 才需要 user-context OAuth：`username` + `password`，或 refresh token，再加对应 scope |

换句话说，你要做的是「注册一个应用」，不是「走一遍授权流程」。本项目刻意只做只读，避免账号风险。

---

## 四、快速开始

### 0. 环境要求与一键安装

- Python **3.10+**（PRAW 8 的硬性要求）
- 能访问 `reddit.com` 与 `api.reddit.com`
- 一个大模型 API Key（也可用本地 Ollama；或先用 `--demo` 看效果）

**Windows 用户**：不用手动装任何东西，直接双击 `start_ui.bat`。它按下面的顺序自己找路：

1. 项目里有 `.venv` 且依赖齐全 → 直接启动
2. 系统里有某个 Python 已装齐依赖 → 用那个启动
3. 都没有 → 自动挑一个 3.10+ 的解释器建 `.venv`，装好依赖再启动（仅首次，1–3 分钟）

这样设计是为了避开最常见的坑：**你机器上可能有多个 Python**，双击时用的那个往往不是装过依赖的那个。
脚本不依赖 PATH 的当前指向，也不会去污染你的系统 Python。端口 8501 被占用时会自动顺延到 8502/8503。

### 0.1 手动安装（macOS / Linux，或想自己管环境）

```bash
cd reddit_backlog_agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 1. 申请 Reddit API 凭证（免费，但新申请通道已关闭）

> ⚠️ **重要现状（2026-11-09）**：Reddit 已于 **2025-11-11 关闭自助创建 API 密钥**，
> **2026-10-31 起不再接受任何新的公开 Data API 访问申请**，2027-01 起开始回收未迁移的旧应用，
> 2027-03 全部公开 API 访问终止。现在点 `create app` 的典型表现是：**人机验证打勾了，
> 点按钮页面静默刷新，什么都没创建**——这不是你操作错了。
>
> 已存在的旧凭证（grandfathered）目前仍可用到 2027 年。**先打开
> <https://www.reddit.com/prefs/apps> 看页面上是否已经列着以前建的应用**，有就直接拿它的
> client_id / secret 用，不必新建。没有的话见「十二、合规与免责」里的替代方案。

> 下面这套流程是**旧凭证还能用过、或你已拿到审批**时的填法。

1. 登录 Reddit，打开 <https://www.reddit.com/prefs/apps>
2. 拉到页面底部，点 **are you a developer? create an app...**
3. 填写：
   - **name**：随便填，例如 `backlog-agent`
   - **type**：务必选 **script**（本工具是脚本，不是网页应用）
   - **description / about url**：可留空
   - **redirect uri**：必须填一个合法 URL，填 `http://localhost:8080` 即可
4. 点 **create app**，创建成功后：
   - `client_id` = 应用名正下方、**personal use script** 旁边那串字符（约 14 位）
   - `client_secret` = 标着 **secret** 的那串字符
5. `user_agent` 需要自己拼，Reddit 要求格式为
   `<平台>:<应用ID>:<版本> (by u/<你的用户名>)`，例如：

   ```
   windows:reddit-backlog-agent:v1.0 (by u/your_username)
   ```

> 只读采集公开数据**不需要**填 Reddit 账号密码。需要访问私有板块时才配置
> `REDDIT_USERNAME` / `REDDIT_PASSWORD` 或 `REDDIT_REFRESH_TOKEN`。

### 2. 安装依赖

Windows 双击 `start_ui.bat` 已经自动完成这一步，可以跳过。需要手动装时：

```bash
cd reddit_backlog_agent
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

> 后面的命令示例统一写 `python main.py`。**没激活虚拟环境时**，Windows 上请把 `python` 换成
> `start_cli.bat`（等价写法：`.venv\Scripts\python.exe main.py`），否则会用到一个没装依赖的系统 Python。

### 3. 配置

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

编辑 `.env`：

```ini
REDDIT_CLIENT_ID=你的client_id
REDDIT_CLIENT_SECRET=你的client_secret
REDDIT_USER_AGENT=windows:reddit-backlog-agent:v1.0 (by u/your_username)

LLM_API_KEY=sk-xxx
LLM_MODEL=gpt-4o-mini
# DeepSeek 用户：LLM_BASE_URL=https://api.deepseek.com/v1
```

### 4. 先跑一次离线演示（不需要任何密钥）

```bash
start_cli.bat --demo          # Windows 未激活虚拟环境
python main.py --demo         # 已激活虚拟环境 / macOS / Linux
```

会用 `tests/fixtures/sample_raw.json` 跑完整流程，在 `reports/` 生成样例报告。**建议先跑这个确认环境正常**，再动真格。

### 5. 正式运行

```bash
# 抓 SaaS + Notion 近一年 top 300 帖，每帖 8 条高赞评论
python main.py --subreddits SaaS,Notion --days 365 --limit 300

# 用关键词搜索模式（更容易命中"求替代品"的帖子）
python main.py --subreddits SaaS,productivity --mode search \
               --keywords "wish there was,alternative to,would pay for"

# 复用已抓数据，只重跑分析（换了模型/提示词时用）
python main.py --from-cache

# 不调大模型，用规则跑一遍看数据结构
python main.py --from-cache --no-llm

# 查看预置板块与关键词
python main.py --list-presets
```

### 6. 打开可视化界面

**双击 `start_ui.bat`**，等窗口里打出 `Open in browser: http://localhost:8501` 后浏览器会自动打开。

没有 Windows 或不想用脚本时：

```bash
.venv\Scripts\python.exe -m streamlit run app.py     # Windows
python -m streamlit run app.py                       # 已激活虚拟环境 / macOS / Linux
```

界面读的是同一份 `.env`，所以填好凭证后打开就能直接跑；详细用法见上一节「三、可视化界面」。

---

## 五、命令行参数

| 参数 | 说明 | 默认 |
|---|---|---|
| `--subreddits` | 目标板块，逗号分隔，可重复传入 | `SaaS,SideProject` |
| `--keywords` | 信号词，逗号分隔，可重复传入 | 预置前 6 个 |
| `--days` | 时间范围（天） | `365` |
| `--limit` | 每个板块最大帖子数（≤1000） | `500` |
| `--comments` | 每帖保留的高赞评论条数 | `8` |
| `--comment-min-score` | 评论最低赞数，过滤噪声 | `2` |
| `--mode` | `top` / `search` / `both` | `top` |
| `--analyze-limit` | 最多送多少篇给模型（控成本） | `300` |
| `--batch-size` | 单次送模型的帖子数 | `8` |
| `--model` / `--base-url` | 覆盖模型与接口地址 | 读 `.env` |
| `--no-llm` | 走规则降级模式 | 关 |
| `--demo` | 用内置样本离线演示 | 关 |
| `--from-cache` | 跳过采集，直接分析 `data/raw_data.json` | 关 |
| `--refresh` | 忽略缓存，强制重新请求 | 关 |
| `--data-dir` / `--output-dir` | 自定义数据/报告目录 | `./data` `./reports` |
| `--quiet` | 只输出警告与结果路径 | 关 |

**关于 `--mode`**：`top` 抓板块热帖（请求最少，推荐）；`search` 按关键词搜索（命中率高，但每个 关键词 × 板块 都是一次请求）；`both` 合并去重。关键词在 `top` 模式下不参与筛选，只用于给帖子打标签并参与需求加权。

---

## 六、接入不同大模型

工具走 OpenAI 兼容协议，换 `LLM_BASE_URL` + `LLM_MODEL` 即可。以各家官方文档为准：

| 服务 | `LLM_BASE_URL` | `LLM_MODEL` 示例 |
|---|---|---|
| OpenAI | 留空 | `gpt-4o-mini` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5:7b`（key 随便填） |

若模型不支持 `response_format={"type":"json_object"}`，程序会自动捕获 400 并降级为"普通输出 + 括号配对解析"，不需要改代码。

---

## 七、打分与优先级口径

```
demand_score = 0.65 × LLM语义强度 + 0.35 × 证据强度
证据强度 = min(1, 该需求的证据权重 / 全样本最大权重) × 10
证据权重 = Σ(1 + log(帖子赞数)×0.7 + log(评论数)×0.3 + 关键词命中×0.2) + 引文数×0.3
```

| 优先级 | 阈值 | 建议 |
|---|---|---|
| P0 | ≥ 8.0 | 立刻做，MVP 必须覆盖 |
| P1 | 6.5 – 8.0 | 下一个迭代 |
| P2 | 4.5 – 6.5 | 持续观察，等信号增强 |
| P3 | < 4.5 | 暂不投入 |

这套分数是**同一份报告内的相对排序**，不是市场规模预测。它回答"先做哪个"，不回答"能赚多少钱"。

---

## 八、实现上的几个关键决策

**为什么只取前 N 条评论？**
评论区是痛点最密集的地方，但展开全部评论会让 API 请求数爆炸。PRAW 的 `replace_more(limit=0)` 会直接丢弃 "load more comments" 占位节点、**不产生额外请求**，然后用本地 score 排序取 Top N。代价是深层子线程可能被遗漏，报告里已注明。

**为什么要回查引文？**
大模型很容易"顺手润色"一句听起来很像用户说的话。`analyzer._quote_is_grounded()` 会把每条引用规范化后回到原帖/评论里做包含匹配，找不到就丢弃，并在报告里统计丢弃数量。这样报告里每条原话都能点链接对上。

**为什么分批 + 归并两阶段？**
单次塞 300 篇会超上下文。所以先按 8 篇一批做"局部抽取"，再用一次"归并"调用把同义项合并（条目少于 25 条时跳过以省钱）。归并调用失败时用 `difflib` 做本地相似度兜底。

**断点续跑**
每篇帖子单独落盘在 `data/cache/{post_id}.json`，汇总文件用"先写临时文件再原子替换"。按 Ctrl+C 不会损坏数据，重跑自动跳过已抓过的帖子。`--refresh` 才强制重抓。

---

## 九、常见问题

| 现象 | 原因与处理 |
|---|---|
| `401 Unauthorized` | client_id / secret 填错，或把 secret 当成了 id。重新复制一次 |
| `403 Forbidden` | 三种可能：① 板块私有/被封禁；② `user_agent` 太敷衍被拦；③ 新应用未获数据访问批准（2026 年政策要求申请审批）。前两种换 UA 或换板块，第三种要到 Reddit 政策页的工单入口申请 |
| `429 Too Many Requests` | 程序会自动退避重试（PRAW 自身也会按 `X-Ratelimit-*` 头等待）。持续 429 请降低 `--limit` 或加 `--mode top` |
| 结果为空 / 条目很少 | 关键词太窄、`--comment-min-score` 太高、板块太冷。先试 `--mode both --limit 500` |
| 全是 "Improvement" 这种泛化名 | 你跑在 `--no-llm` 规则模式。配置 LLM_API_KEY 后去掉该参数 |
| Excel 打不开或乱码 | CSV 用 UTF-8 BOM，Excel 直接双击可读；`*_backlog.xlsx` 是标准 xlsx。若被杀毒软件锁住请先关闭文件再重跑 |
| Windows 控制台报编码错误 | `main.py` 已把 stdout 重置为 UTF-8；如仍报错，用 `chcp 65001` 或改用 Windows Terminal |
| 运行很慢 | 主要耗时在评论请求。把 `--comments` 降到 5、`--analyze-limit` 降到 150 可显著提速 |
| 点 `create app` 后页面静默刷新、应用没创建 | 自助创建已于 2025-11-11 关闭，新申请 2026-10-31 截止。换浏览器/网络/旧界面都无效。检查 `prefs/apps` 是否已有旧应用；没有则走替代方案 |
| `No module named streamlit` / `No module named praw` | 启动时用到的 Python 不是装依赖那个。项目里有多个解释器（如 `C:\Python314`、`py -3.13`、`.venv`）时最容易踩。解决：双击 `start_ui.bat` 由它挑解释器，或显式用 `.venv\Scripts\python.exe` |
| 双击 bat 一闪而过 | 多半是脚本里的中文被 cmd 按 GBK 解析崩了。本项目的 bat 已全部改为纯 ASCII；若你自行改过，请只写英文并保存为 ANSI/ASCII |
| 界面打不开 / 8501 端口被占用 | `start_ui.bat` 会自动顺延到 8502/8503；手动启动时加 `--server.port 8502` |
| 界面改了参数但结果没变 | Streamlit 每次交互都会重跑脚本，但结果只在点「开始分析」后更新；换参数后需要重新点一次 |

退出码：`0` 成功 / `1` 配置错误 / `2` 数据缺失 / `3` 分析失败 / `4` 未预期错误 / `130` 手动中断。

---

## 十、成本与耗时参考

- **采集**：300 帖 × 8 评论 ≈ 300–400 次 HTTP 请求，受 Reddit 速率限制约束，通常 3–8 分钟。
- **分析**：300 帖按每批 8 篇 ≈ 38 次模型调用 + 1 次归并 + 1 次摘要，约 20–40 万 tokens 量级（取决于帖子长度），用 `gpt-4o-mini` 级别模型成本很低。
- **省钱三招**：`--analyze-limit 150`、`--comments 5`、条目少时把 `merge_min_items` 调高（跳过归并调用）。

---

## 十一、测试

```bash
python -m unittest discover -s tests -v
```

20 个用例覆盖：文本清洗、置顶/NSFW 过滤、评论排序与去噪、JSON 容错解析、引文回查、本地归并、优先级阈值、启发式全链路产出、Excel 结构。

另有 4 个界面冒烟测试（`tests/test_ui_smoke.py`，基于 Streamlit `AppTest`）：首屏渲染无异常、无凭证时的报错文案、演示模式端到端跑通并跳转看板、看板筛选与各页面切换。全部离线，不需要密钥，也不写脏 `reports/`。

---

## 十二、合规与免责

> 依据 Reddit [Responsible Builder Policy](https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy)
> （2026-06-05 更新版）整理。政策会变，正式使用前请自己再读一遍原文。

### 现状时间线（截至 2026-11-09，来自 Reddit 官方公告与 r/redditdev）

| 时间 | 事件 |
|---|---|
| 2025-11-11 | Reddit 关闭**自助**创建 API 密钥，新密钥一律需审批 |
| 2026-08-05 | 宣布公开 Data API 逐步迁移到 Developer Platform（Devvit） |
| 2026-10-31 | **不再接受任何新的公开 Data API 访问申请** ← 已过期 |
| 2027-01-12 | 开始回收未注册/未迁移应用的 API 访问 |
| 2027-03 | 剩余公开 API 访问全部终止 |

**对本项目意味着**：新用户已无法通过「创建 script app」拿到可用的 Reddit Data API 凭证。
已存在的旧凭证在 2027 年前仍可用。三条替代路径：

1. **用旧凭证**：打开 `reddit.com/prefs/apps` 看是否已有历史应用，有就直接用。
2. **换数据源**：接第三方 Reddit 数据 API（付费服务），把 `scraper.py` 的取数层换掉即可——
   `analyzer.py` / `reporter.py` 完全不用动。也可以把外部数据按本项目的 JSON 结构写进
   `data/raw_data.json`，然后 `--from-cache` 走完整分析流程（见「十四、自己提供数据」）。
3. **Devvit**：如果目标是"在 Reddit 内运行的应用"，走 <https://developers.reddit.com>；
   但它是 Reddit 托管的沙箱环境，不能替代本项目的本地 Python 批处理。

**这个工具默认只符合「非商业、个人使用」的场景。** 几条硬约束：

| 政策要求 | 对本项目的含义 |
|---|---|
| **必须申请并获批后才能通过 API 访问 Reddit 数据** | 在 `prefs/apps` 建应用只是拿到凭证，不等于拿到数据许可。若调用被拒（401/403）或客户端被限流，需按政策里的工单入口申请 |
| **未获书面批准不得商业化** | 把报告拿去卖、做成订阅产品、放进付费服务里，都需要 Reddit 的明确书面同意。自己看、内部立项调研属于非商业用途 |
| **未获批准不得用 Reddit 数据训练模型** | 你在这里是「调用模型分析」，不是「拿数据训练模型」；但把抓下来的语料长期留存做训练集就越线了 |
| **用户删除内容后你必须删除**（建议 48 小时内例行清理） | `data/raw_data.json` 与 `data/cache/` 是本地持久化副本。**分析完请删掉 `data/` 目录**，不要长期囤积 |
| **禁止去匿名化 / 推断敏感特征** | 报告只展示帖子链接与正文，不展示用户名，也不做作者画像 |
| **100 QPM / 每 client id**（免费额度，10 分钟窗口均值） | 默认参数离上限很远；不要同时开多个实例并发抓取 |

其他：

- 仅访问**公开**内容，默认只读模式；Cookie / 密码等凭证不写入任何产物文件（`.env` 已在 `.gitignore` 中）。
- 请遵守各板块规则：不要高频轮询、不要用于骚扰。`user_agent` 必须真实描述你的应用，**不要伪造**。
- 报告中的 `demand_score` 是启发式指标，模型判断存在波动；**立项决策前请点开原始链接人工复核，并补充用户访谈**。
- 本项目不抓取、不存储邮箱等个人身份信息；报告中只展示帖子链接与正文，不展示用户名。
- 违反政策的后果包括撤销 token、封禁应用或账号。风险由使用者承担，本项目不提供合规背书。

---

## 十三、已知限制

- 只覆盖 Reddit，且以英语社区为主，中文/其他语种赛道样本会偏少。
- 不做情感时间序列（无法看"这个痛点是变强还是变弱"）。
- 不做竞品功能比对，`alternative to` 只作为信号词使用。
- 归并与打分都在单次运行内完成，没有跨批次的历史趋势对比（`analysis.json` 已保留快照，可自行 diff）。

---

## 十四、自己提供数据（Reddit API 不可用时）

新应用拿不到凭证时，不必改代码——只要把数据按下面的结构写进 `data/raw_data.json`，
然后跑 `--from-cache`，分析、打分、报告、Excel 全部照常工作：

```bash
start_cli.bat --from-cache            # 或在界面里选「复用本地缓存」
```

最小结构（字段缺失会被容错，但 `title` / `score` / `top_comments` 缺了会影响打分）：

```json
{
  "meta": {
    "subreddits": ["SaaS"],
    "keywords": ["wish there was"],
    "time_range_days": 365,
    "generated_at": "2026-11-09T00:00:00+00:00",
    "note": "可选：数据来源说明"
  },
  "posts": [
    {
      "id": "p01",
      "subreddit": "SaaS",
      "title": "帖子标题（必填，作为文本分析主体）",
      "selftext": "正文，可空字符串",
      "score": 842,
      "num_comments": 96,
      "created_utc": 1772000000,
      "permalink": "https://www.reddit.com/r/SaaS/comments/p01/",
      "author": "可选，报告不会展示",
      "matched_keywords": ["wish there was"],
      "top_comments": [
        { "id": "c0101", "body": "评论正文", "score": 320, "is_submitter": false }
      ]
    }
  ]
}
```

三条实用说明：

- `score` 和评论的 `score` 是**热度权重**的主要来源，不要全填 0，否则需求强度会拉不开差距。
- `top_comments` 建议按 `score` 从高到低放 5–10 条，评论往往比正文更有信息量。
- 分析结果的每条结论都带 `post_id` 与 `permalink`，**引文回查**机制会按 `title` / `selftext` / 评论正文
  逐条比对，编造的原话会被丢弃并计数——所以正文必须是真实文本，不能只填结论。
