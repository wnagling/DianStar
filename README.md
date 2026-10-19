# agentTry

agentTry 是一个学习和构建本地 Agent 的项目。长期方向见 [产品文档](./产品文档.md)。当前阶段已通过 SDK 完成 DeepSeek 问答，下一步逐步扩展为对项目自身的只读分析。

## 当前状态

- 已有：Python 命令行入口、从本地 `.env` 读取密钥、单次问答、可保存并恢复的终端连续对话，以及查询城市当前气温和未来 7 天最高、最低气温的工具。
- 未有：`list_files` / `read_file` 文件工具。当前模型无法读取项目文件，回答不能作为项目事实的依据。
- 验收：2026-10-18 已完成真实 DeepSeek 单次问答；2026-10-19 已完成真实天气工具调用，结果见 [开发日志](./开发日志/2026-10-19.md)。
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

`.env` 的格式可参考 [.env.example](./.env.example)：只需将等号右侧替换为你的密钥。程序默认请求 `deepseek-flash`。每次 DeepSeek API 请求设置 60 秒时限，SDK 自动重试已关闭。天气工具得到结果后直接格式化回答，一轮只需一次 DeepSeek 请求。出错时不打印密钥。单次问答以失败状态退出；连续对话会提示错误并允许继续输入。

## 天气工具

例如运行 `python -m agent_try "北京明天的最高和最低气温是多少？"`。模型会提取地点和日期，调用 `agent_try/tools/weather.py` 中的 `get_weather(location, date)`；程序核对工具结果后直接格式化回答。首版支持目的地当地的今天及之后 7 天；查询今天时返回带当地时间的当前气温，以及当日最高、最低气温；查询未来日期时只返回最高、最低气温。`date` 可用 `today`、`tomorrow` 或 `YYYY-MM-DD`。重名地点如果无法可靠区分，会要求补充地区或国家。没有地点或日期时应先补充信息。

天气数据来自 [Open-Meteo](https://open-meteo.com/)，城市名称通过其 [Geocoding API](https://open-meteo.com/en/docs/geocoding-api) 解析；最高、最低气温是[预报](https://open-meteo.com/en/docs)，当前气温也是模型估计值，并非温度计实测值。当前气温缺失时会明确提示暂不可用。查询需要联网，失败时程序会提示错误。其免费接口仅供非商业使用；商业使用请先检查其[服务条款](https://open-meteo.com/en/terms)。这项功能不读取设备位置，也不根据公网 IP 猜测城市。

终端回答只显示地点、日期和气温，不附带数据来源文字；来源与使用条件记录在本节。

如果 DeepSeek 偶尔把内部工具标记当作文本返回，程序只解析完整且参数合法的 `get_weather` 调用；不能解析时会报错，不会把标记显示为答案。旧会话文件中已保存的错误标记仍留在本机记录里，但后续请求会跳过该轮上下文。

离线检查：`python -m unittest discover -s tests`。它使用模拟回复；真实 API 的单次问答已另行验证，见上面的开发日志。

## 文件结构

```text
agent_try/__main__.py   命令行入口和错误提示
agent_try/deepseek.py   DeepSeek SDK 请求及工具调用
agent_try/tools/weather.py  城市天气查询函数
agent_try/tools/__init__.py 工具包入口
agent_try/__init__.py   Python 包
.vscode/launch.json     VS Code 模块运行配置
pyproject.toml          项目元数据和依赖
.env                    本地密钥文件，不进入 Git
.env.example            不含真实密钥的格式示例
.local/conversations/   本机会话记录，不进入 Git
tests/test_deepseek.py  离线检查
tests/test_weather.py   天气工具离线检查
产品文档.md              长期方向
编程语言选型.md          当前 Python 决定与历史 Rust 记录
开发日志/2026-10-13.md  历史开发日志
```

## 下一小步

下一步实现受项目根目录约束的只读文件工具，并验证路径和文件大小限制，再让模型调用。文件工具完成后，再检查结论和文件行号；在此之前不宣称项目分析 Agent 已可用。
