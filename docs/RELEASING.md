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
5. 创建 Release。

一般不要修改已公开 Release 的同名安装包；需要修复时提高版本号并重新发布。

## 完整安装包

正式 Windows Release 包含 Pandoc、MTranServer、英译简中模型和内嵌中文字体。GitHub Actions 会先运行 `scripts/fetch_release_runtime.py` 下载固定运行时包并做一次 SHA-256 校验，再以 `LITMTRANS_BUNDLE_OPTIONAL_RUNTIME=1` 构建完整安装包。组件来源和许可证见 `THIRD_PARTY_NOTICES.md`。

## 代码签名

项目当前没有 Windows 代码签名，因此 SmartScreen 可能提示未知发布者。以后如取得证书，可在生成安装包之后、生成更新清单之前签名。
