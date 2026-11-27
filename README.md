# 期刊投稿格式采集

这个 Python 命令行程序按单刊或按 Excel 清单采集公开的投稿指南、投稿格式和相关附件。输出包含下载原件、网页快照和来源清单。需求见 [期刊投稿格式文件采集需求.md](docs/期刊投稿格式文件采集需求.md)。

## 项目目录

- `data/` 存放采集输入，默认清单为 `data/期刊链接.xlsx`。
- `docs/` 存放需求和实施计划。
- `periodical_collector/` 存放采集程序。
- `tests/` 存放测试。
- `output/` 存放采集结果，首次运行时创建。

## 安装

在 Mac 的终端或者 VS Code 集成终端运行以下命令。建议使用 Python 3.11 或更新版本。

```bash
cd ~/periodical_collection
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## 采集单个期刊

程序默认从项目的 `data/期刊链接.xlsx` 查询期刊名称和官网，并记录 Excel 行号。

```bash
python -m periodical_collector single --journal '财经法学'
```

如果有另一个已核实的官方入口，可以一并提供。《财经法学》的示例同时使用 Excel 里的知网投审平台和校方投稿须知。

```bash
python -m periodical_collector single --journal '财经法学' \
  --entry-url 'https://cjfx.cufe.edu.cn/tgxz.htm'
```

也可以直接用 `--url 'https://期刊官网地址'` 指定官网。借助 `--allow-host 域名` 可以明确允许跟进其他官方域名上的链接。两项额外入口参数均可重复填写。

## 批量采集

```bash
python -m periodical_collector batch --limit 5
python -m periodical_collector batch --resume
```

第一条只处理前五条 Excel 记录。第二条读取整份清单。如果期刊名、`journal_id` 和网址都相同，并且该期刊上次成功保存的 Word 或 PDF 附件仍在，程序会跳过该条记录。可用 `--excel 路径` 指定其他清单，用 `--output 路径` 改变输出目录。缺少有效网址的行仍会写入批次汇总。

命令结束会打印清单路径。默认以 `output/` 为根目录，每次运行使用独立子目录。单刊的 `manifest.json` 列出采集到的资料、证据路径和待核查项。批量的 `batch-summary.json` 汇总状态与各刊清单位置，运行期间逐条更新。

只有网页规定时，程序另存正文为 `期刊名投稿格式.md`，同时保留原网页快照。没有任何下载成功且通过验证的投稿格式 Word 或 PDF 附件时，状态为 `uncompleted`。有有效附件的记录仍可能包含待核查入口，可在其清单中查看。

## 可选的浏览器补查

默认使用 HTTP 和 Beautiful Soup。已知公开菜单或下载按钮需要浏览器执行时，可以安装 Playwright 并加 `--browser`。

```bash
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
python -m periodical_collector single --journal '财经法学' --browser
```

需要 AI 探索时，在项目根目录的 `.env.local` 写入 `BROWSER_USE_API_KEY=你的密钥`，然后运行以下命令。已设置的同名环境变量优先于本地配置。请将密钥留在本机。

```bash
python -m periodical_collector single --journal '法律适用' --browser --browser-use
```

符合补查条件时，程序向 Browser Use Cloud 发起按量计费的请求。程序使用 `bu-ultrafast`，单次代理调用的模型与搜索费用上限为 0.25 美元，浏览器和流量另计。API 密钥需要具有代理任务和网络策略权限，程序会用临时策略限制可访问域名。

两种补查可以同时启用，普通链接优先处理。程序以配置的搜索范围为边界，无法访问、需要登录、达到搜索上限或资料用途不明的线索留在清单中。

用以下命令查看请求数量、深度、等待时间、文件大小等配置。

```bash
python -m periodical_collector single --help
python -m periodical_collector batch --help
```
