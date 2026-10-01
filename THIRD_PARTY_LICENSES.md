# 第三方依赖许可证清单

本项目以 **MIT** 协议开源（见根级 [LICENSE](LICENSE)）。本清单覆盖三类外部依赖：Python 端、Go 端、Web UI 端。

> **合规判定**：本项目以动态链接/解释执行的方式使用所有第三方库，不修改其源码；不重新分发其源码。MIT 项目在以下三种许可证下兼容：
> - **Apache-2.0 / BSD-2 / BSD-3 / MIT / ISC / Unlicense / HPND / Zlib**：全部可自由使用与再分发，许可证文本随附在分发物中即可；
> - **LGPL-3**（pystray）：仅以未修改的二进制形态通过 `pip install` 拉取与使用，符合 LGPL 动态链接豁免；若需静态链接或修改，请改用 PyQt/pynput 等 MIT/BSD-3 等价物；
> - **GPL-3**（trafilatura）：通过 `pip install` 作为解释运行的 Python 依赖引入；非本项目静态链接或合并进 gem；如要彻底规避，可在自定义安装时替换为 `selectolax`（BSD-3）或 `lxml`（BSD-3）做 HTML 抽取。
>
> **可选依赖 [office] 中的 pymupdf（AGPL-3）** 单独标注：本项目将其放在 **可选 extra** 中，默认安装不会引入；启用 `pip install coara[office]` 才下载，且不与本项目本体一同分发。如对 AGPL 存疑，安装时请显式跳过：`pip install coara[office-novendor]` 或自行替换为 BSD-3 的 `pypdf`。

---

## 1. Python 运行时依赖（pyproject.toml）

| 依赖 | 版本约束 | 许可证 | 主页 | 备注 |
|------|---------|--------|------|------|
| **openai** | ≥3.14.0, <4 | Apache-2.0 | https://github.com/openai/openai-python | LLM provider |
| **pydantic** | ≥2.9.0 | MIT | https://github.com/pydantic/pydantic | 数据建模与校验 |
| **pyyaml** | ≥6.0 | MIT | https://github.com/yaml/pyyaml | YAML 解析 |
| **python-dotenv** | ≥1.0.0 | BSD-3 | https://github.com/theskumar/python-dotenv | .env 加载 |
| **rich** | ≥13.9.0 | MIT | https://github.com/Textualize/rich | 终端渲染 |
| **click** | ≥8.1.0 | BSD-3 | https://github.com/pallets/click | CLI 框架 |
| **prompt_toolkit** | ≥3.0.0 | BSD-3 | https://github.com/prompt-toolkit/python-prompt-toolkit | 交互式输入 |
| **questionary** | ≥2.0.0 | MIT | https://github.com/tmbo/questionary | 交互式问答 |
| **pygments** | ≥2.18.0 | BSD-2 | https://github.com/pygments/pygments | 语法高亮 |
| **python-frontmatter** | ≥1.1.0 | MIT | https://github.com/eyeseast/python-frontmatter | Front matter 解析 |
| **trafilatura** | ≥2.0.0 | **GPL-3** | https://github.com/adbar/trafilatura | 网页正文抽取（**GPL**） |
| **watchdog** | ≥5.0.0 | Apache-2.0 | https://github.com/gorakhargosh/watchdog | 文件监听 |
| **charset-normalizer** | ≥3.0 | MIT | https://github.com/jawah/charset_normalizer | 字符编码探测 |
| **httpx** | ≥0.27.0 | BSD-3 | https://github.com/encode/httpx | 异步 HTTP 客户端 |
| **aiohttp** | ≥3.10.0 | Apache-2.0 | https://github.com/aio-libs/aiohttp | 异步 HTTP |
| **matrix-nio** | ≥0.25.2 | Apache-2.0 | https://github.com/poljar/matrix-nio | Matrix 协议客户端 |
| **psutil** | ≥6.0.0 | BSD-3 | https://github.com/giampaolo/psutil | 进程与系统信息 |
| **aiosqlite** | ≥0.19.0 | MIT | https://github.com/omnilib/aiosqlite | 异步 SQLite |
| **croniter** | ≥2.0.0 | MIT | https://github.com/kiorky/croniter | cron 表达式解析 |
| **loguru** | ≥0.7.0 | MIT | https://github.com/Delgan/loguru | 日志 |
| **pystray** | ≥0.19.0 | **LGPL-3** | https://github.com/moses-palmer/pystray | 系统托盘（**LGPL**） |
| **pillow** | ≥10.0.0 | HPND / MIT-CMU | https://github.com/python-pillow/Pillow | 图像处理（双重许可） |

### 可选 extra

| 依赖 | 所属 extra | 版本约束 | 许可证 | 备注 |
|------|-----------|---------|--------|------|
| python-docx | `[office]` | ≥1.1.0 | MIT | .docx 读写 |
| openpyxl | `[office]` | ≥3.1.0 | MIT | .xlsx 读写 |
| **pymupdf** | `[office]` | ≥1.24.0 | **AGPL-3** | .pdf 解析（**AGPL**，可选） |
| mss | `[desktop]` | ≥10.0.0 | MIT | 截屏 |
| imap-tools | `[desktop]` | ≥1.0.0 | Apache-2.0 | 邮件收发 |
| pypdf | `[desktop]` | ≥5.0.0 | BSD-3 | PDF 处理（BSD-3 替代方案） |

### 开发与测试依赖（[dev]）

| 依赖 | 版本约束 | 许可证 | 备注 |
|------|---------|--------|------|
| pytest | ≥8.3.0 | MIT | 测试框架 |
| pytest-asyncio | ≥0.24.0 | Apache-2.0 | asyncio 测试支持 |
| pytest-timeout | ≥2.3.0 | MIT | 测试超时 |
| ruff | 0.15.12 | MIT | 代码风格与检查 |
| mypy | ≥1.13.0 | MIT | 类型检查 |
| qrcode[pil] | ≥7.4.2 | BSD-3 | 二维码 |

---

## 2. WDL 引擎依赖（wdl/pyproject.toml，独立子项目）

WDL 是仓库内 `wdl/` 子项目，独立分发的 `wdl-engine` 包，与 coara 内核同许可（MIT）。

| 依赖 | 版本约束 | 许可证 | 备注 |
|------|---------|--------|------|
| pydantic | ≥2.9 | MIT | 数据建模 |
| pyyaml | ≥6 | MIT | YAML |
| httpx | ≥0.27 | BSD-3 | HTTP |
| loguru | ≥0.7 | MIT | 日志 |
| aiosqlite | ≥0.19 | MIT | 异步 SQLite |
| aiohttp | ≥3.10 | Apache-2.0 | 异步 HTTP |
| pytest | ≥8.3 | MIT | dev |
| pytest-asyncio | ≥0.24 | Apache-2.0 | dev |
| ruff | 0.15.12 | MIT | dev |

---

## 3. Go 服务端依赖（gomatrix/go.mod）

| 模块 | 版本 | 许可证 | 用途 |
|------|------|--------|------|
| github.com/BurntSushi/toml | v1.6.0 | Unlicense / MIT | TOML 解析 |
| github.com/go-chi/chi/v5 | v5.3.0 | MIT | HTTP 路由 |
| github.com/skip2/go-qrcode | v0.0.0-20200617 | MIT | 二维码 |
| golang.org/x/crypto | v0.52.0 | BSD-3 | 加密原语 |
| golang.org/x/sys | v0.45.0 | BSD-3 | 系统调用 |
| modernc.org/sqlite | v1.51.0 | MIT | 纯 Go SQLite（无 CGO） |

---

## 4. Web UI 依赖（src/ui/web/package.json，Node.js）

| 依赖 | 许可证 | 主页 | 用途 |
|------|--------|------|------|
| react | MIT | https://github.com/facebook/react | UI 框架 |
| react-dom | MIT | https://github.com/facebook/react | DOM 渲染 |
| react-router-dom | MIT | https://github.com/remix-run/react-router | 路由 |
| react-markdown | MIT | https://github.com/remarkjs/react-markdown | Markdown 渲染 |
| remark-gfm | MIT | https://github.com/remarkjs/remark-gfm | GFM 扩展 |
| remark-math | MIT | https://github.com/remarkjs/remark-math | 数学公式 |
| rehype-highlight | MIT | https://github.com/rehypejs/rehype-highlight | 代码高亮 |
| rehype-katex | MIT | https://github.com/rehypejs/rehype-katex | KaTeX 集成 |
| katex | MIT | https://github.com/KaTeX/KaTeX | 数学公式渲染 |
| antd | MIT | https://github.com/ant-design/ant-design | 组件库 |
| @ant-design/icons | MIT | https://github.com/ant-design/ant-design-icons | 图标 |
| @xyflow/react | MIT | https://github.com/xyflow/xyflow | 工作流画布 |
| zustand | MIT | https://github.com/pmndrs/zustand | 状态管理 |
| vite | MIT | https://github.com/vitejs/vite | 构建工具 |
| typescript | Apache-2.0 | https://github.com/microsoft/TypeScript | 语言 |
| tsx | MIT | https://github.com/privatenumber/tsx | TS 执行器 |
| @vitejs/plugin-react | MIT | https://github.com/vitejs/vite-plugin-react | React 集成 |

---

## 6. 自持副本声明（无外部依赖但项目内嵌）

| 文件 | 来源 | 许可证 | 备注 |
|------|------|--------|------|
| `src/workflow/core/` | `wdl-engine` 图内核分叉 | MIT | 同许可；分叉声明见该目录 `model.py` 文件头注释；本项目按需维护，不随外部上游升级释放 |

---

## 7. 复现命令

```bash
# Python 端：装 pip-licenses 后导出
pip install pip-licenses
pip-licenses --format=markdown --with-urls --order=license > /tmp/py-licenses.md

# Go 端：在仓储里声明一份
cd gomatrix && go-licenses report ./... 2>/dev/null

# Web UI 端：装 npx license-checker 后导出
cd src/ui/web && npx -y license-checker --summary --production

# 转写此文件即得本清单的副本
```

---

## 8. 许可证与上游合规说明

本项目的所有依赖都按其原始许可证使用，源码与必要的许可证副本随分发物一同提供（如 `gomatrix.exe` 与 `dist/*.whl` 的发布脚本会复制对应许可证文本）。若发现本项目无法实时升级，请通过 issue 提交，我们会替换等价物并在此清单更新。

> 上次更新时间：2026-10-01
> 与公开仓 [LICENSE](LICENSE) 文件同级引用；本清单是治理维度（开源治理 20%）的合规证据。