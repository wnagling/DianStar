# agentTry

agentTry 是一个学习和构建本地 Agent 的项目。长期方向见 [产品文档](./产品文档.md)。当前阶段已通过 SDK 完成 DeepSeek 问答，下一步逐步扩展为对项目自身的只读分析。

## 当前状态

- 已有：Python 命令行入口、从本地 `.env` 读取密钥、单次问答和终端连续对话。
- 未有：`list_files` / `read_file` 工具和工具调用循环。当前模型无法读取项目文件，回答不能作为项目事实的依据。
- 验收：2026-10-18 已完成一次真实 DeepSeek 单次问答，结果见 [开发日志](./开发日志/2026-10-18.md)。
- 暂不实现：文件修改、命令执行、记忆、多 Agent 调度和外部 Agent 接入。

此前的 Rust 只读原型保存在本地 Git 提交 `7729740`。它曾通过离线单元测试，但没有完成真实 DeepSeek API 验收。[2026-10-13 开发日志](./开发日志/2026-10-13.md)记录的是该历史阶段；当前语言决定见 [编程语言选型](./编程语言选型.md)。

## 运行

需要 Python 3.10 或更新版本。安装依赖后，在项目根目录的 `.env` 文件中填写你自己的 DeepSeek API Key。`.env` 已被 Git 忽略；不要将密钥发到聊天或复制进其他项目文件。

```powershell
python -m pip install -e .
python -m agent_try --help
python -m agent_try
python -m agent_try "请用一句话介绍你自己。"
```

不带问题启动后，可在终端连续输入；`/clear` 清空上下文，`/exit` 退出。历史只保存在当前进程内存中，退出即丢失；每轮请求会重新发送已有对话，因此长对话会增加请求内容和费用。请求失败的轮次不加入历史。

在 VS Code 中打开整个 `agentTry` 文件夹，选择已安装依赖的 Python 解释器，然后在“运行和调试”中选择“agentTry：连续对话”，按 F5。单次问答仍可选“agentTry：提问 DeepSeek”。这两个配置都以模块方式启动程序；直接点击 `__main__.py` 的“运行 Python 文件”按钮仍会把它当作独立脚本运行。

如果当前 PowerShell 的中文输出显示为乱码，可在运行前设置 `$env:PYTHONIOENCODING='utf-8'`。

`.env` 的格式可参考 [.env.example](./.env.example)：只需将等号右侧替换为你的密钥。程序默认请求 `deepseek-flash`。每轮请求设置 60 秒总时限，SDK 自动重试已关闭；出错时不打印密钥。单次问答以失败状态退出；连续对话会提示错误并允许继续输入。

离线检查：`python -m unittest discover -s tests`。它使用模拟回复；真实 API 的单次问答已另行验证，见上面的开发日志。

## 文件结构

```text
agent_try/__main__.py   命令行入口和错误提示
agent_try/deepseek.py   单次 DeepSeek SDK 请求
agent_try/__init__.py   Python 包
.vscode/launch.json     VS Code 模块运行配置
pyproject.toml          项目元数据和依赖
.env                    本地密钥文件，不进入 Git
.env.example            不含真实密钥的格式示例
tests/test_deepseek.py  离线检查
产品文档.md              长期方向
编程语言选型.md          当前 Python 决定与历史 Rust 记录
开发日志/2026-10-13.md  历史开发日志
```

## 下一小步

下一步实现受项目根目录约束的只读文件工具，并验证路径和文件大小限制，再让模型调用工具。真实工具循环跑通后，再检查结论和文件行号；在此之前不宣称项目分析 Agent 已可用。
