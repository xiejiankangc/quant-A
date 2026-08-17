# quant-codex

A 股行情接入，支持双机各自克隆、各自搭建环境与构建。

## 项目结构

| 路径 | 说明 |
| --- | --- |
| `market_data/em.py` | 东方财富行情访问层：实时线 → 延迟线自动降级，并按「系统代理 → 直连 → curl」逐层回退 |
| `scripts/verify_market_data.py` | 双通道自检脚本：东财行情（失败回退腾讯逐笔）+ 同花顺 fuyao REST |
| `market_overview/` | 市场概览与板块变化追踪：M0 已提供抓取层、本地缓存与报告骨架 |
| `examples/portfolio.sample.json` | 组合输入格式样例（代码 + 权重，缺省基准上证指数、窗口 60 交易日） |
| `requirements.txt` | 直接依赖（精确版本） |
| `requirements.lock` | 完整依赖锁定（`pip freeze` 产物，可复现构建用） |
| `.python-version` | 目标 Python 版本 |
| `AGENTS.md` | 给两台机器上 Codex 的协作与环境边界约定 |

## 数据源

| 数据源 | 方式 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| akshare | Python（项目 `.venv`） | 无 | 东财/腾讯等公开接口；东财对部分 VPN 出口敏感，`market_data/em.py` 已做自适应 |
| 同花顺 fuyao | REST / MCP / CLI | `X-api-key` | 行情、财报、指数板块、基金、特色数据等，共用统一 API Key |

## 新机器一次性环境搭建（Windows）

两台机器都必须各执行一遍；`.venv` 属于机器级产物，不入库、不复制。

### 0. 前置

- Git for Windows：`winget install --id Git.Git -e`
- Python 3.12：`winget install --id Python.Python.3.12 -e`
- Node.js >= 22.12 与 npm（同花顺 CLI 需要）

### 1. 克隆并建立 Python 环境

```powershell
git clone https://github.com/xiejiankangc/quant-A.git
cd quant-A
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

需要与源机完全一致的依赖时，改用 `python -m pip install -r requirements.lock`。

### 2. 安装同花顺 CLI

```powershell
npm install -g @hithink-tech/hithink-finance-cli
hithink-finance version --format json
```

### 3. 配置统一 API Key

在 <https://fuyao.aicubes.cn/admin> 获取 API Key 后，写入用户环境变量与
凭据文件（文件只写一行 `HITHINK_FINANCE_API_KEY=...`）：

```powershell
$secureKey = Read-Host 'API Key' -AsSecureString
$key = [System.Net.NetworkCredential]::new('', $secureKey).Password
[Environment]::SetEnvironmentVariable('HITHINK_FINANCE_API_KEY', $key, 'User')
New-Item -ItemType Directory -Force "$env:APPDATA\hithink-finance" | Out-Null
Set-Content -Path "$env:APPDATA\hithink-finance\credentials.env" -Value "HITHINK_FINANCE_API_KEY=$key"
Remove-Variable key, secureKey
```

再把 Key 通过 stdin 同步给 CLI（不在命令行回显）：

```powershell
$key = [Environment]::GetEnvironmentVariable('HITHINK_FINANCE_API_KEY', 'User')
$key | hithink-finance auth login --api-key-stdin --format json
Remove-Variable key
```

验证：`hithink-finance auth status --format json` 返回 `configured: true`。

### 4. 同步 CLI 配套 Skills

```powershell
hithink-finance skills sync --format json
hithink-finance skills status --format json
```

随后核验当前 Agent 的技能目录（`~/.codex/skills` 与 `~/.agents/skills`）中
存在 `hithink-finance-shared`、`hithink-finance-symbol`、
`hithink-finance-market`、`hithink-finance-financials`、
`hithink-finance-index`、`hithink-finance-special-data`、
`hithink-finance-fund`、`hithink-finance-data`、`hithink-finance-research`、
`hithink-finance-valuation`，且每个目录都有 `SKILL.md`。缺失时从
`skills status` 输出中的 `canonical` 目录完整复制（含 `references/`）。
安装后新建或重启会话，让新技能被重新发现。

### 5. 接入 MCP（可选，Codex 对话场景）

在 `~/.codex/config.toml` 的 `[mcp_servers]` 区加入四个托管端点；配置只引用
环境变量，不含明文 Key：

```toml
[mcp_servers.hithink-finance-a-share]
url = "https://fuyao.aicubes.cn/mcp/a-share"
env_http_headers = { "X-api-key" = "HITHINK_FINANCE_API_KEY" }

[mcp_servers.hithink-finance-a-share-index]
url = "https://fuyao.aicubes.cn/mcp/a-share-index"
env_http_headers = { "X-api-key" = "HITHINK_FINANCE_API_KEY" }

[mcp_servers.hithink-finance-meta]
url = "https://fuyao.aicubes.cn/mcp/meta"
env_http_headers = { "X-api-key" = "HITHINK_FINANCE_API_KEY" }

[mcp_servers.hithink-finance-fund]
url = "https://fuyao.aicubes.cn/mcp/fund"
env_http_headers = { "X-api-key" = "HITHINK_FINANCE_API_KEY" }
```

修改后重启 Codex 应用，使其重新加载 MCP 并发现新技能。

### 6. 验证

```powershell
python scripts/verify_market_data.py 600519   # akshare + fuyao 双通道行情
hithink-finance symbol search --q 600519 --limit 1 --format json
hithink-finance doctor --format json
```

## 市场概览跟踪（M0 骨架）

先抓取再渲染报告；缓存与报告都落在不入库的 `data/` 目录：

```powershell
python -m market_overview fetch --portfolio examples\portfolio.sample.json --data-dir data\cache
python -m market_overview report --portfolio examples\portfolio.sample.json --data-dir data\cache --out data\reports\market_overview.md
```

- `fetch` 默认抓 60 交易日窗口的上证指数日线、东财行业/概念板块列表、涨跌幅
  最大的板块明细样例，以及组合内个股的后复权日线和市值/换手率补充信息；
- `report` 从缓存渲染 Markdown 报告，抓取失败的条目会如实列在「抓取状态」；
- 组合 JSON 格式见 `examples/portfolio.sample.json`，权重合计必须为 1；
- 东财板块历史走 `push2his.eastmoney.com`，部分 VPN 出口会风控该域名；失败
  不影响其余数据落盘，报告中会保留原因。

## 网络环境注意（机器级配置，不入库）

GitHub 直连可能被阻断；若 `git ls-remote` 超时，给 Git 配本机代理（端口按
本机实际代理填写）：

```powershell
git config --global http.proxy http://127.0.0.1:7892
git config --global https.proxy http://127.0.0.1:7892
```

东财 `push2.eastmoney.com` 对部分 VPN 出口敏感。`market_data/em.py` 会自动
降级到延迟线并尝试系统代理/直连/curl，无需手工干预；同花顺 fuyao 与腾讯源
不受影响。

## 双机协作

共享的是源码与文档；`.venv`、凭据、`~/.codex` 配置、Git 代理等属机器级状态，
不提交、不共享。分工与分支约定见 [AGENTS.md](AGENTS.md)。
