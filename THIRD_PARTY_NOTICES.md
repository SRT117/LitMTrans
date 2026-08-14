# LitMTrans 第三方组件与资源声明

LitMTrans 项目自身代码采用 MIT License。本文件记录不受该许可证覆盖的关键依赖和资源。LitMTrans 只发布 Windows 安装包，必须同时携带实际打包组件要求的许可证与版权声明。

| 组件 | 用途 | 许可证/发布注意事项 |
| --- | --- | --- |
| PySide6 / Qt | 桌面界面与 WebEngine | 社区版本采用 LGPLv3/GPLv3；分发 Qt 动态库时须遵守 LGPLv3 的通知、许可证和可替换库要求。 |
| pypdfium2 / PDFium | PDF 页面渲染与文本几何 | pypdfium2 为 Apache-2.0/BSD-3-Clause；PDFium 及其实际二进制依赖的许可证必须随安装包分发。 |
| requests | HTTP 请求 | Apache-2.0。 |
| python-docx | Word 文档处理 | MIT。 |
| NumPy、Matplotlib、Pillow、zstandard | 计算、图像和压缩支持 | 以各发行版本附带的许可证为准。 |
| PyInstaller | 构建工具 | 使用其 GPL 例外；最终安装包仍必须满足其中实际包含依赖的许可证。 |
| Source Han Serif CN Regular 2.003 | 译文阅读与保留版式渲染的内嵌字体 | SIL Open Font License 1.1；未修改，来源为 [Adobe Source Han Serif](https://github.com/adobe-fonts/source-han-serif)，文件为 `resources/fonts/SourceHanSerifCN-Regular.ttf`，SHA-256：`8ba5ec09db04b1d1599edeff3fb5627ca11eaaf85e339e5c32684cb94e806993`。完整许可证见同目录的 `LICENSE-SourceHanSerif.txt`。 |
| Pandoc 3.8.3 | Word、EPUB 和部分 PDF 的转换与导出 | 不进入公开基础安装包。可选本地构建使用 `resources/pandoc.exe`，SHA-256：`19b8b7c191e33f6870f4cb92768fc3ce558be75f9a22f671d5d22ea35dca95bd`。Pandoc 是独立命令行程序，以 GPL-2.0-or-later 授权；许可证与版权声明位于 `licenses/pandoc/`。公开分发时还必须提供对应源码。 |
| MTranServer 与语言模型 | 可选离线本地翻译 | 不进入公开基础安装包。只有来源、版本、许可证和再分发权都已核实的运行时与模型才可另行发布；所需声明和源码获取方式必须随包提供。当前本地运行时 SHA-256：`70999a5842984247aeacbd381bc1df26737a129e1a6683be3572bd0cc1b5a798`。 |

## 未随仓库发布的资源

Pandoc、MTranServer、模型文件和用户文档不构成 GitHub 源码发行的一部分。基础安装包只携带仓库中经过审核的小型资源。可选运行时只有在能确认来源、版本、许可证和再分发权，并随包提供所需声明后，才能另行发布。

MinerU 是外部文档解析服务。项目不分发 MinerU 源码或模型；用户自行提供访问令牌并遵守其适用条款。名称和商标归其权利人所有。
