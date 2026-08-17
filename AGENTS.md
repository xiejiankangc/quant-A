# AGENTS.md

供两台机器上的 Codex 使用。项目定位：A 股行情接入（akshare 东财 + 同花顺
fuyao），首个版本提供双通道自检能力。

## 项目结构

- `market_data/em.py`：东方财富行情访问层（实时线 → 延迟线自动降级，并按
  「系统代理 → 直连 → curl」逐层回退）。
- `scripts/verify_market_data.py`：双通道自检脚本（东财失败时回退腾讯逐笔源）。
- `requirements.txt` / `requirements.lock` / `.python-version`：Python 环境版本来源。

## 环境规则

- 新机器按 README 完成一次性搭建；`.venv` 每机各自重建，禁止复制或提交。
- 修改直接依赖后同步更新 `requirements.txt`，并用
  `python -m pip freeze > requirements.lock` 刷新锁文件。
- 金融取数优先走 hithink-finance 系列技能路由；Python 侧只有 `market_data`
  与自检脚本直接访问行情接口。
- 真实数据不可用时如实报告原因，不使用模拟或近似数据冒充。

## 安全

- API Key 只允许存在于机器级凭据源：用户环境变量
  `HITHINK_FINANCE_API_KEY`、`%APPDATA%\hithink-finance\credentials.env`
  或 CLI 系统凭据。绝不写入代码、命令参数、日志、Markdown、Issue 或 Git。
- `credentials.env` 已加入 `.gitignore`；提交前检查 `git status` 无敏感文件。

## 机器级 vs 仓库级

入库：源码、文档、依赖锁文件、构建/自检脚本、与机器无关的项目配置。

不入库：`.venv/`、`credentials.env`、`~/.codex/config.toml`（MCP 与本机路径）、
`~/.gitconfig`（代理与身份）、本机缓存与数据目录。

## 常用命令

```powershell
.\.venv\Scripts\Activate.ps1
python scripts/verify_market_data.py 600519
hithink-finance doctor --format json
hithink-finance symbol search --q 600519 --limit 1 --format json
```

## 双机协作约定

- 开工前先 `git pull --rebase`；不在同一分支并行修改，任务改动走 feature
  分支，验证后合并回 main。
- 本机网络代理、CLI 升级、MCP 配置等机器级变更不写入仓库；README 的「网络
  环境注意」只作参考模板。
- 首次提交推送完成后，另一台机器从 GitHub clone 并在本地重建环境，不再原地
  使用 SMB 共享目录构建。

## 换行与编码

- 仓库内统一 LF（`.gitattributes` 已设 `* text=auto`），Windows 检出时自动转
  CRLF，避免双机产生虚假 diff。
- 源码 UTF-8；控制台输出中文的编码问题由自检脚本内部处理。
