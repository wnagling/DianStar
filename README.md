# agentTry

agentTry 是一个学习和构建本地 Agent 的项目。长期方向见 [产品文档](./产品文档.md)。当前阶段已通过 SDK 完成 DeepSeek 问答，下一步逐步扩展为对项目自身的只读分析。

## 当前状态

- 已有：Python 命令行入口、从本地 `.env` 读取密钥、单次问答、可保存并恢复的终端连续对话，以及 Windows 设备定位工具调用（当前系统权限尚未允许真实定位）。
- 未有：`list_files` / `read_file` 工具。当前模型无法读取项目文件，回答不能作为项目事实的依据。
- 验收：2026-10-18 已完成一次真实 DeepSeek 单次问答，结果见 [开发日志](./开发日志/2026-10-18.md)。
- 暂不实现：文件修改、命令执行、记忆、多 Agent 调度和外部 Agent 接入。

此前的 Rust 只读原型保存在本地 Git 提交 `7729740`。它曾通过离线单元测试，但没有完成真实 DeepSeek API 验收。[2026-10-13 开发日志](./开发日志/2026-10-13.md)记录的是该历史阶段；当前语言决定见 [编程语言选型](./编程语言选型.md)。

## 运行

需要 Python 3.10 或更新版本。安装依赖后，在项目根目录的 `.env` 文件中填写你自己的 DeepSeek API Key。`.env` 已被 Git 忽略；不要将密钥发到聊天或复制进其他项目文件。

```powershell
python -m pip install -e .
python -m agent_try --help
python -m agent_try
python -m agent_try --list
python -m agent_try --resume 会话ID
python -m agent_try "请用一句话介绍你自己。"
```

不带问题启动时会创建新会话并显示 ID，可在终端连续输入。`/new` 创建另一个新会话，`/compress` 清空**后续请求使用的上下文**但保留本地聊天记录，`/exit` 退出。`--list` 列出已保存的会话 ID，`--resume 会话ID` 恢复会话及其上下文位置；运行时请把“会话ID”替换为实际显示的 ID。每轮成功问答后才写入本地文件；请求失败的轮次不加入记录。每轮请求会重新发送当前上下文，因此长对话会增加请求内容和费用。目前 `/compress` 只清空上下文，尚不生成摘要。

会话记录保存在项目根目录 `.local/conversations/` 中，每个 ID 对应一个 JSON 文件。`.local/` 已被 Git 忽略；其中的聊天内容仍是本机明文文件，请勿在对话中输入不希望保存的密钥等敏感内容。带问题参数的单次问答不保存会话。

在 VS Code 中打开整个 `agentTry` 文件夹，选择已安装依赖的 Python 解释器，然后在“运行和调试”中选择“agentTry：连续对话”，按 F5。要恢复旧会话，选“agentTry：恢复会话”并输入 ID；单次问答仍可选“agentTry：提问 DeepSeek”。这些配置都以模块方式启动程序；直接点击 `__main__.py` 的“运行 Python 文件”按钮仍会把它当作独立脚本运行。

如果当前 PowerShell 的中文输出显示为乱码，可在运行前设置 `$env:PYTHONIOENCODING='utf-8'`。

`.env` 的格式可参考 [.env.example](./.env.example)：只需将等号右侧替换为你的密钥。程序默认请求 `deepseek-flash`。每次 DeepSeek API 请求设置 60 秒时限，SDK 自动重试已关闭；定位工具可能使一轮问答包含两次请求。出错时不打印密钥。单次问答以失败状态退出；连续对话会提示错误并允许继续输入。

离线检查：`python -m unittest discover -s tests`。它使用模拟回复；真实 API 的单次问答已另行验证，见上面的开发日志。

## 文件结构

```text
agent_try/__main__.py   命令行入口和错误提示
agent_try/deepseek.py   DeepSeek SDK 请求与定位工具调用
agent_try/location.py   Windows 设备定位函数
agent_try/__init__.py   Python 包
.vscode/launch.json     VS Code 模块运行配置
pyproject.toml          项目元数据和依赖
.env                    本地密钥文件，不进入 Git
.env.example            不含真实密钥的格式示例
.local/conversations/   本机会话记录，不进入 Git
tests/test_deepseek.py  离线检查
产品文档.md              长期方向
编程语言选型.md          当前 Python 决定与历史 Rust 记录
开发日志/2026-10-13.md  历史开发日志
```

## Windows 设备定位工具

在提问中明确要求查询这台设备的当前位置时，DeepSeek 可以调用 `get_location`。终端会先询问是否允许读取设备位置并将坐标发送给 DeepSeek；只有输入 `y`、`yes` 或 `是` 才会继续。Windows 还可能要求系统定位权限；可到“设置 → 隐私和安全性 → 位置”开启定位服务及桌面应用的位置访问权限。拒绝后不会读取位置，模型会收到拒绝信息。

工具返回纬度、经度和 Windows 报告的精度（米），坐标在发送前保留四位小数。首版不将坐标转换成城市或街道。Windows 无法提供位置、权限被拒或请求超时，都会返回错误给模型。模型的最终回答可能包含位置，并按现有规则保存到本机 `.local/conversations/`；单次问答不保存。工具调用过程中的原始坐标不会单独写入会话文件。

本功能使用 Windows 定位服务及 `winrt-Windows.Devices.Geolocation`，仅在 Windows 上可用。模型调用工具后还会发出第二次 DeepSeek 请求，所以一次提问可能产生两次 API 请求。

## 下一小步

下一步实现受项目根目录约束的只读文件工具，并验证路径和文件大小限制，再让模型调用。文件工具完成后，再检查结论和文件行号；在此之前不宣称项目分析 Agent 已可用。
