# LitMTrans 本地资源与开发指南

源码仓库只保存运行程序所需的小型核心资源，包括图标、排版模板、Lua 过滤器和内嵌思源宋体。大型二进制运行时（Pandoc、MTranServer 可执行程序）和离线模型文件不进入 Git 源码仓库。

## 1. 默认源码运行（无需额外配置）

开发者克隆仓库后，安装依赖即可直接运行：
```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe litmtrans.py
```
默认环境下，PDF 解析、EPUB 本地解析、大模型翻译（OpenAI/DeepSeek/Gemini 等）、Edge 本地/联网翻译、AI 对话、思维导图、HTML 导出均可正常使用。

## 2. 可选增强组件配置（按需添加）

若开发者需要调试本地 Pandoc 转换或 MTranServer 离线神经网络机翻，可按如下目录结构放置可选组件：

| 组件 | 推荐版本 | 放置路径 | 说明 |
| :--- | :--- | :--- | :--- |
| **Pandoc** | 3.8.3 (或 3.1+) | `resources/pandoc.exe` 或安装在系统 `PATH` | 用于 Word 深度排版转换、公式转 Word 原生公式及 Office 文档预览。若系统环境变量已有 `pandoc`，程序会自动识别。 |
| **MTranServer** | 4.0.33+ (Windows x64) | `resources/mtranserver/bin/mtranserver-windows-amd64.exe` | 本地离线机翻服务进程。 |
| **机翻配置** | - | `resources/mtranserver/config/records.json` | 语言包与模型索引配置文件。 |
| **语言模型** | - | `resources/mtranserver/models/<语言对>/`（如 `en_zh-Hans`） | 离线翻译模型文件（含 `model.*.bin`、`lex.*.bin`、`*.spm` 词表）。 |

## 3. 字体与许可证

`fonts/SourceHanSerifCN-Regular.ttf` 为思源宋体 CN Regular 2.003，采用 SIL Open Font License 1.1，已内置在源码仓库中。完整许可证位于同目录，详细第三方声明见根目录的 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

## 4. 全量 Release 打包

本地已就绪可选组件后，设置环境变量即可构建包含完整离线功能的安装包：
```powershell
$env:LITMTRANS_BUNDLE_OPTIONAL_RUNTIME = "1"
.\.venv\Scripts\python.exe scripts/build_release.py
```
