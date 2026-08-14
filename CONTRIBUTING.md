# LitMTrans 贡献指南

感谢参与 LitMTrans。提交前请确保修改范围明确、结果可复现，并且不包含本机数据。

## 开发检查

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q .
```

请为行为变更补充或更新测试。不要提交 API 密钥、访问令牌、对话记录、原始文献、解析结果、模型、构建目录或第三方二进制文件。

## 代码与文档

- 使用 UTF-8，保留现有中文界面的语言一致性。
- 注释只解释公开 API、非直观约束、隐私/安全边界或算法理由；不保留调试过程和版本迭代记录。
- 新增第三方依赖前，说明许可证、来源、用途及二进制分发要求。
- 不要以“免费网页接口”替代经授权的服务 API。

## 提交问题

问题报告请包含复现步骤、预期行为、实际行为、系统/Python 版本及已脱敏日志。请不要附上真实密钥或无权公开的文档。
