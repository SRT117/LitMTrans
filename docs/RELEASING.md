# 发布 Windows 版本

## 发布前

1. 在 `app_version.py` 更新 `APP_VERSION`。
2. 在 `CHANGELOG.md` 写明本版本变化。
3. 运行测试和发布检查。
4. 在干净的 Windows 11 虚拟机上安装并测试上一版本，再覆盖安装候选版本。
5. 检查新安装、卸载、更新、密钥读取、工作文件夹和主要功能。

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe scripts\validate_release.py
```

版本号使用 `MAJOR.MINOR.PATCH`。Git 标签必须与应用版本完全一致，例如 `APP_VERSION = "1.0.1"` 对应 `v1.0.1`。

## GitHub Actions

推送版本标签后，Release 工作流会：

1. 运行测试和发布检查；
2. 使用 PyInstaller 构建 Windows x64 程序目录；
3. 使用 Inno Setup 生成当前用户安装包；
4. 生成 `update.json` 和 `SHA256SUMS.txt`；
5. 创建草稿 Release。

草稿不会被已安装程序识别为最新版本。下载草稿安装包，在干净环境完成冒烟测试后，再手动发布 Release。

不要修改已公开 Release 的同名安装包。需要修复时提高版本号并重新发布，否则已经下载的 `update.json` 和安装包摘要会不一致。

## 可选运行时

公开的基础安装包不包含 Pandoc、MTranServer 或语言模型。本地构建只有在已核实来源、版本、许可证和再分发权后，才可设置 `LITMTRANS_BUNDLE_OPTIONAL_RUNTIME=1`。

如果以后发布包含这些组件的安装包，应为它使用不同的文件名，并随 Release 提供对应许可证和依法需要的源码或源码获取方式。

## 代码签名

SHA-256 可以发现下载损坏或清单不一致，但不能替代 Windows Authenticode。取得代码签名证书后，应在生成安装包之后、生成 `update.json` 之前完成签名和时间戳。

