# agentTry

agentTry 是一个学习和构建本地 Agent 的项目。长期方向见 [产品文档](./产品文档.md)。当前阶段先通过 SDK 完成一次 DeepSeek 问答，再逐步扩展为对项目自身的只读分析。

## 当前状态

- 已有：Python 命令行入口、从本地 `.env` 读取密钥、单次 DeepSeek 问答代码。
- 未有：`list_files` / `read_file` 工具、工具调用循环、真实 API 验收。当前模型无法读取项目文件，回答不能作为项目事实的依据。
- 暂不实现：文件修改、命令执行、记忆、多 Agent 调度和外部 Agent 接入。

此前的 Rust 只读原型保存在本地 Git 提交 `7729740`。它曾通过离线单元测试，但没有完成真实 DeepSeek API 验收。[2026-10-13 开发日志](./开发日志/2026-10-13.md)记录的是该历史阶段；当前语言决定见 [编程语言选型](./编程语言选型.md)。

## 运行

需要 Python 3.10 或更新版本。安装依赖后，在项目根目录的 `.env` 文件中填写你自己的 DeepSeek API Key。`.env` 已被 Git 忽略；不要将密钥发到聊天或复制进其他项目文件。

```powershell
python -m pip install -e .
python -m agent_try --help
python -m agent_try "请用一句话介绍你自己。"
```

`.env` 的格式可参考 [.env.example](./.env.example)：只需将等号右侧替换为你的密钥。程序默认请求 `deepseek-flash`，每次运行发送一个问题、接收一个回答，不保存对话历史。整次请求设置 60 秒总时限，SDK 自动重试已关闭；出错时以失败状态退出，且不打印密钥。

离线检查：`python -m unittest discover -s tests`。它使用模拟回复，不能代替真实 API 验收。真实验收需在你本机填写密钥后运行上面的固定问题，确认打印有效回答并正常退出。

## 文件结构

```text
agent_try/__main__.py   命令行入口和错误提示
agent_try/deepseek.py   单次 DeepSeek SDK 请求
agent_try/__init__.py   Python 包
pyproject.toml          项目元数据和依赖
.env                    本地密钥文件，不进入 Git
.env.example            不含真实密钥的格式示例
tests/test_deepseek.py  离线检查
产品文档.md              长期方向
编程语言选型.md          当前 Python 决定与历史 Rust 记录
开发日志/2026-10-13.md  历史开发日志
```

## 下一小步

先完成一次真实 DeepSeek API 验收。随后实现受项目根目录约束的只读文件工具，并验证路径和文件大小限制，再让模型调用工具。真实工具循环跑通后，再检查结论和文件行号；在此之前不宣称项目分析 Agent 已可用。
