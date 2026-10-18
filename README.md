# agentTry

agentTry 是一个学习和构建本地 Agent 的项目。长期方向见 [产品文档](./产品文档.md)。当前阶段的 Python CLI 可以通过 SDK 向 DeepSeek 发送一个问题并打印回答；目标是逐步扩展为对项目自身的只读分析。

## 当前状态

- 已有：Python 命令行入口，以及单次、非流式 DeepSeek 问答的接入代码。
- 未有：`list_files` / `read_file` 工具、工具调用循环、真实 API 验收。当前模型无法读取项目文件，回答不能作为项目事实的依据。
- 暂不实现：文件修改、命令执行、记忆、多 Agent 调度和外部 Agent 接入。

此前的 Rust 只读原型保存在本地 Git 提交 `7729740`。它曾通过离线单元测试，但没有完成真实 DeepSeek API 验收。[2026-10-13 开发日志](./开发日志/2026-10-13.md)记录的是该历史阶段；当前语言决定见 [编程语言选型](./编程语言选型.md)。

## 运行

需要 Python 3.10 或更新版本。先安装项目依赖，再在当前 PowerShell 会话中设置 API Key：

```powershell
python -m pip install -e .
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new("", (Read-Host "DeepSeek API Key" -AsSecureString)).Password
python -m agent_try --help
python -m agent_try "请用一句话介绍你自己。"
```

默认模型为 `deepseek-flash`，默认 API 地址为 `https://api.deepseek.com`。如有需要，可用 `DEEPSEEK_MODEL` 和 `DEEPSEEK_BASE_URL` 环境变量覆盖。一次运行只请求模型一次，不保留对话历史；请求最多生成 512 token，超时为 60 秒，最多重试一次。不要将 API Key 写入仓库。

无 API Key 时，程序会报错，不会发出请求。离线检查可运行 `python -m unittest discover -s tests`；它使用模拟响应，不能代替真实 DeepSeek API 验收。

## 文件结构

```text
agent_try/__main__.py   命令行入口和错误提示
agent_try/deepseek.py   单次 DeepSeek SDK 请求
agent_try/__init__.py   Python 包
pyproject.toml          项目元数据和 SDK 依赖
tests/test_deepseek.py  离线接入检查
产品文档.md              长期方向
编程语言选型.md          当前 Python 决定与历史 Rust 记录
开发日志/2026-10-13.md  历史开发日志
```

## 下一小步

先用真实 API Key 验证一次问答。随后实现受项目根目录约束的只读文件工具，并验证路径和文件大小限制，再让模型调用工具。真实工具循环跑通后，再检查结论和文件行号；在此之前不宣称项目分析 Agent 已可用。
