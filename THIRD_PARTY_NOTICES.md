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
| Source Han Serif CN Regular 2.003 | 译文阅读与保留版式渲染的内嵌字体 | SIL Open Font License 1.1；未修改，来源为 [Adobe Source Han Serif](https://github.com/adobe-fonts/source-han-serif)。完整许可证见字体目录。 |
| Pandoc 3.8.3 | Word、EPUB 和部分 PDF 的转换与导出 | GPL-2.0-or-later；项目以独立命令行程序形式随 Windows 安装包分发。许可证见 `licenses/pandoc/`，对应源码可从 [Hackage](https://hackage.haskell.org/package/pandoc-3.8.3) 获取。 |
| MTranServer 4.0.33 | 英译简中离线翻译服务 | Apache-2.0；来源为 [xxnuo/MTranServer](https://github.com/xxnuo/MTranServer)，许可证见 `licenses/mtranserver/`。 |
| Firefox Translations 英译简中模型 | MTranServer 使用的离线翻译模型 | MPL-2.0；来源为 [Mozilla Firefox Translations models](https://github.com/mozilla/firefox-translations-models)，许可证见 `licenses/mtranserver/`。 |

## 大型发行资源

Pandoc、MTranServer 和模型文件不提交进 Git 源码仓库，而是在发布构建时从固定的运行时包取得。正式 Windows 安装包包含这些组件及相应许可证。

MinerU 是外部文档解析服务。项目不分发 MinerU 源码或模型；用户自行提供访问令牌并遵守其适用条款。名称和商标归其权利人所有。
