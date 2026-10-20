# agentTry

agentTry 是一个用 Python 编写的本地命令行 Agent 学习项目。现在可以通过 OpenAI 兼容 SDK 调用 DeepSeek、进行单次问答或可恢复的终端对话，并使用天气工具查询城市气温。程序模块的关系见 [当前架构](./docs/架构.md)，长期方向见 [产品文档](./docs/产品文档.md)。

## 当前功能

- **DeepSeek 问答**：默认使用 `deepseek-flash`，从项目根目录的 `.env` 读取用户自己的 API Key。每次 API 请求的超时为 60 秒，SDK 自动重试已关闭。
- **终端连续对话**：为会话生成 ID，将成功的问答保存在本机；可列出、恢复或新建会话。`/compress` 会清空后续请求使用的上下文，但保留聊天记录，目前不生成摘要。
- **天气查询**：模型选择 `get_weather` 工具，程序向 Open-Meteo 查询地点和天气，再把结果交给模型回答。支持地点当地的今天及未来 7 天；今天提供当前气温和当日最高、最低气温，未来日期提供最高、最低气温。没说日期时先查今天，再问用户想查询哪天。
- **任务循环与验收**：循环处理模型回答、工具调用和工具结果，并限制最多六次决策。天气回答需要有实际查询结果，且城市、日期和气温要通过 [天气完成条件](./agent_try/weather_contract.py) 的检查；不通过时会要求模型修正。无法解析的工具标记不会显示为最终答案。

目前**没有**项目文件读取、文件修改或命令执行工具。模型不能凭当前工具读取项目文件，普通问答也不应被当成对项目内容的事实核查。天气地点由用户提供；程序不读取设备位置，也不按公网 IP 猜测位置。

## 安装

需要 Python 3.10 或更新版本、可用的 DeepSeek API Key，以及访问 DeepSeek 和 Open-Meteo 的网络。在项目根目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
```

然后在**项目根目录**的 `.env` 中将 `DEEPSEEK_API_KEY=your-key-here` 的占位值替换为自己的 Key。不要把真实 Key 写入 `.env.example`、README、聊天消息或 Git 提交。安装时使用的 Python 与运行时使用的 Python 应当是同一个环境。

## 使用

以下命令均在项目根目录运行：

```powershell
# 进入连续对话
.\.venv\Scripts\python.exe -m agent_try

# 单次提问，不保存会话
.\.venv\Scripts\python.exe -m agent_try "潮州今天天气多少度？"

# 查看会话 ID、恢复会话
.\.venv\Scripts\python.exe -m agent_try --list
.\.venv\Scripts\python.exe -m agent_try --resume 会话ID

# 查看命令帮助
.\.venv\Scripts\python.exe -m agent_try --help
```

连续对话启动后会显示会话 ID，输入普通文字即可提问：

| 指令 | 作用 |
| --- | --- |
| `/new` | 创建并切换到新会话 |
| `/compress` | 清空后续请求的上下文，保留本地记录 |
| `/exit` | 退出程序 |

每轮成功问答后才写入会话文件。会话文件位于项目根目录的 `.local/conversations/`，每个 ID 对应一个 JSON 文件；文件是本机明文，`.local/` 已被 Git 忽略。长对话会重复发送当前上下文，增加请求内容和费用。

在 VS Code 中打开整个 `agentTry` 文件夹，选中已安装项目依赖的 Python 解释器，再从“运行和调试”使用 `.vscode/launch.json` 提供的“连续对话”“恢复会话”或“提问 DeepSeek”配置。程序应以模块 `agent_try` 启动；直接把 `agent_try/__main__.py` 当独立脚本运行会导致相对导入错误。如果 PowerShell 中文显示乱码，可先运行 `$env:PYTHONIOENCODING='utf-8'`。

## 天气工具怎样工作

`agent_try/agent.py` 编排本轮任务；`agent_try/deepseek.py` 把工具名称及参数说明发给 DeepSeek。模型请求 `get_weather(location, date)` 后，`agent_try/tools/weather.py` 用 [Open-Meteo Geocoding API](https://open-meteo.com/en/docs/geocoding-api) 查城市，再用[天气预报 API](https://open-meteo.com/en/docs) 查气温。程序把工具结果交回模型，`agent_try/loop.py` 继续循环，`agent_try/weather_contract.py` 检查候选回答，验收通过后才显示。

`date` 可为 `today`、`tomorrow` 或未来 7 天内的 `YYYY-MM-DD`。查询以目的地当地日期为准。中文城市名会尝试有无“市”字及部分自治区简称；回退结果必须与城市名匹配。重名地点无法可靠区分时，需要用户补充地区或国家。当前气温可能暂不可用；最高、最低气温为预报，当前气温也来自天气模型，并非温度计实测值。终端回答不附带数据来源文字，来源在本节记录。查询需要联网；使用 Open-Meteo 免费接口前请查看其[使用条件](https://open-meteo.com/en/terms)。

## 密钥与权限边界

`.env` 和 `.local/` 已列入 `.gitignore`，因此不会因普通的 `git add .` 进入仓库。程序会在本机读取 Key 并通过 SDK 发送请求；DeepSeek 模型收到用户消息和工具结果，不需要收到 Key。**有本机项目文件权限的开发工具仍可能读取 `.env`**；Git 忽略规则不构成访问控制。添加文件工具之前的密钥隔离待办见 [2026-10-21 待办](./docs/开发日志/2026-10-21-待办.md)。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目功能、安装、运行和文件导航 |
| `pyproject.toml` | Python 包元数据、依赖及 `agent-try` 命令入口 |
| `.gitignore` | 排除密钥、会话、虚拟环境和缓存 |
| `.env.example` | API Key 配置格式示例，不含真实密钥 |
| `.vscode/launch.json` | VS Code 的模块运行与调试配置 |
| `agent_try/__init__.py` | Python 包入口 |
| `agent_try/__main__.py` | 命令行参数、交互提示、会话指令和错误展示 |
| `agent_try/conversations.py` | 会话 ID、JSON 持久化、恢复和上下文起点 |
| `agent_try/agent.py` | 一轮任务的编排、工具分派与回答保存 |
| `agent_try/deepseek.py` | DeepSeek SDK 请求、消息转换与异常工具标记解析 |
| `agent_try/loop.py` | 有次数上限的 Think → Act → Observe 循环 |
| `agent_try/weather_contract.py` | 天气任务提示、发送给模型的数据及回答核对 |
| `agent_try/tools/__init__.py` | 工具包入口 |
| `agent_try/tools/weather.py` | 天气工具定义、城市解析、日期校验和 Open-Meteo 查询 |
| `tests/test_deepseek.py` | 问答、会话和模型工具调用的离线测试 |
| `tests/test_loop.py` | 循环停止规则和天气完成条件测试 |
| `tests/test_weather.py` | 地名、日期与天气数据解析测试 |
| `docs/架构.md` | 当前模块边界、任务流程和数据权限说明 |
| `docs/产品文档.md` | 长期产品目标；其中的规划不代表已经实现 |
| `docs/编程语言选型.md` | Python 选型与历史 Rust 原型的取舍 |
| `docs/参考技术.md` | 供后续设计参考的外部技术笔记 |
| `docs/开发日志/2026-10-13.md` | 历史 Rust 阶段的开发记录 |
| `docs/开发日志/2026-10-18.md` | Python 问答、终端会话与保存功能的开发记录 |
| `docs/开发日志/2026-10-19.md` | 天气工具及真实调用的开发记录 |
| `docs/开发日志/2026-10-21-待办.md` | 文件读取工具和 API Key 隔离的后续任务 |

运行时还会出现两个**不进入 Git**的本地路径：`.env` 存放用户自己的 Key，`.local/conversations/` 存放会话。此前的 Rust 只读原型保存在 Git 历史提交 `7729740`，不属于当前 Python 运行入口。

## 验证与下一步

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

离线测试使用模拟的模型与天气响应，不需要真实 Key。真实 DeepSeek 问答和天气调用已分别验证；实际联网结果会随时间与服务状态变化。下一步是先界定文件工具可读取的范围和密钥隔离，再添加受限的 `list_files` / `read_file`。
