# DianStar

项目原名 AgentTry，现统一为 DianStar；Python 包名和完整启动命令为 `dianstar`，快捷命令为 `DS`。需要 Python 3.11 或更新版本。改名与升级说明见 [2026-10-23 开发记录](./docs/开发日志/2026-10-23.md)。

DianStar 是一个用 Python 编写的本地命令行 Agent 学习项目。现在可以通过 OpenAI 兼容 SDK 调用 DeepSeek、进行单次问答或可恢复的终端对话，并使用天气工具查询城市气温。程序模块的关系见 [当前架构](./docs/架构.md)，长期方向见 [产品文档](./docs/产品文档.md)。

运行时默认提示词在 `dianstar/system_prompt.md`，程序启动时由 `prompts.py` 读取并发送给 DeepSeek。修改后重新启动程序即可生效。`AGENTS.md` 是 Codex 等开发助手可选的仓库说明文件，Python 应用不会自动加载它；本项目没有将其作为运行时提示词。

## 当前功能

- **DeepSeek 问答**：每个 Key 有独立的模型列表和默认模型；首次安装的默认 Key 预置 `deepseek-flash` 和 `deepseek-v4-pro`，新建 Key 的模型列表为空。可用 `/config` 添加、删除具名 Key，选择默认 Key，并分别增删模型。程序从项目根目录的 `.env` 读取所选 Key，并启用 thinking。每次 API 请求的超时为 60 秒，SDK 自动重试已关闭。推理内容用于后续请求的上下文；TUI 在当前请求期间显示最近四行推理内容。
- **终端连续对话**：默认进入 Textual 终端界面，会话列表初始收起，可用顶部按钮、Ctrl+L 或 `/switch` 展开；Ctrl+B 默认打开模型切换器。切换会话后可滚动查看历史，右下角显示当前会话名。空会话中间显示从桌面“兔子.png”的轮廓生成的盲文字符画，以淡色点纹表现原图的暖色亮面和右侧灰色阴影。木棉花取自桌面参考图，以红金色点阵作背景；兔子使用较小的 20×12 或 12×7 版本，叠在花朵中央的前层，花瓣从四周露出，轮廓之间留白。窗口放不下花朵时仅显示兔子。开始对话后，字符画会随聊天内容向上滚走，不写入会话文件。每个会话首次成功提问后默认取问题前 6 个字符；可以重命名或恢复。输入 `/` 可查看命令候选，按 Tab 补全。成功的问答和模型工具消息保存在本机。终端会渲染回答中的 Markdown，聊天记录保留模型最终回答原文。`/compress` 会清空后续请求使用的上下文，但保留聊天记录，目前不生成摘要。
- **流式回答**：TUI 在回答预览上方用独立边框显示四行 thinking，收到 DeepSeek 的正文片段后立即显示；完成后收起临时区域，将完整回答写入聊天记录。工具调用不会显示。流式回答预览最多占 8 行，较长回答完成后可在聊天记录中滚动查看。单次命令行问答仍在完成后一次输出。
- **天气查询**：模型选择 `get_weather` 工具，程序向 Open-Meteo 查询地点和天气，再把结果交给模型回答。一次请求多个城市时，各次查询会分别执行；某座城市查询失败不会阻止其他城市。支持地点当地的今天及未来 7 天；可获得最高和最低气温，以及有数据时的每日天气现象、降水量、最大降水概率和最大风速；今天还可获得当前气温、天气现象和风速。没说日期时默认查今天；回答按用户询问的指标组织，不强制罗列全部字段。地名或日期查询失败会作为结构化错误交给模型，由模型决定是否换一种写法重试、追问或直接回答。
- **任务循环**：循环处理模型回答、一次或多次工具调用及其结果，最多六次模型决策；每次决策最多八个工具调用。模型决定是否查询天气、如何处理工具错误以及回答重点；程序校验工具参数、天气查询日期和返回地点，并限制重复调用。程序不再用气温关键词或数值规则检查最终措辞。模型可能漏查工具或写错数据，程序无法保证发现这类问题。无法解析的工具标记不会显示为最终答案。

目前**没有**项目文件读取、文件修改或命令执行工具。模型不能凭当前工具读取项目文件，普通问答也不应被当成对项目内容的事实核查。天气地点由用户提供；程序不读取设备位置，也不按公网 IP 猜测位置。

## 安装

需要 Python 3.11 或更新版本、可用的 DeepSeek API Key，以及访问 DeepSeek 和 Open-Meteo 的网络。在项目根目录打开 PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
```

然后在**项目根目录**的 `.env` 中将 `DEEPSEEK_API_KEY=your-key-here` 的占位值替换为自己的 Key。这个变量会作为已有的第一个 Key 使用；也可以随后在 `/config` 中添加其他 Key。不要把真实 Key 写入 `.env.example`、README、聊天消息或 Git 提交。安装时使用的 Python 与运行时使用的 Python 应当是同一个环境。

也可在 TUI 中输入 `/config` 设置 Key。进入配置菜单后用 ↑/↓ 选择项目，按 Enter 进入；在 `api_key` 中选择已有 Key 编辑，或选“添加 Key”输入名称和密钥。可“保存并设为默认”；编辑页的“删除 Key”需要再按一次确认，会同时删除其模型配置和 `.env` 中对应的密钥。删除最后一个 Key 后保留一个空的默认配置位置，方便重新设置。编辑页会加载已有密钥，默认以圆点遮挡；点击“显示／隐藏”切换可见性，可直接修改后保存，密钥不能为空。退出编辑后会清空输入框，再次进入时恢复遮挡。所有 Key 均保存在项目根目录的 `.env` 中，配置文件只保存名称、模型和默认选择，密钥不写入聊天记录。

在 `model` 中选中模型后，按 Enter 或点击“编辑选中模型”可修改模型 ID；保存默认模型的新 ID 时，默认选择会同步更新。按 Esc 或“取消”放弃修改。每个 Key 的模型列表独立保存。在 `api_key` 中可点击“编辑选中 Key”修改名称和密钥，只有保存后才会生效。

## 给 Windows 用户试用

维护者可在 Windows 上从项目根目录运行以下命令，生成无需安装 Python 的便携 ZIP：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1
```

脚本会在 `.local/packaging/` 创建独立打包环境，将 ZIP 放入 `release/`。将整个 ZIP 发给试用者；对方解压后双击 `DianStar.cmd` 或 `DianStar.exe`，再输入 `/config` 配置自己的 API Key。包内含程序代码、依赖、提示词和终端图案数据，不含制作者的 `.env`、会话或配置。便携版把试用者的 `.env` 和 `.local/` 写在解压目录，因此应放在可写的个人文件夹中。Windows 版须在 Windows 上构建，其他系统需要分别构建。打包后的提示词已嵌入程序，修改源码提示词需要重新打包。

## 使用

以下命令均在项目根目录运行：

```powershell
# 进入终端界面连续对话
.\.venv\Scripts\python.exe -m dianstar

# 使用原有的纯文本交互界面
.\.venv\Scripts\python.exe -m dianstar --plain

# 单次提问，不保存会话
.\.venv\Scripts\python.exe -m dianstar "潮州今天天气多少度？"

# 按会话 ID 查看和恢复
.\.venv\Scripts\python.exe -m dianstar --list
.\.venv\Scripts\python.exe -m dianstar --resume 会话ID

# 查看命令帮助
.\.venv\Scripts\python.exe -m dianstar --help
```

安装项目后，也可以在任意终端输入 `DS` 启动 DianStar TUI。Windows 命令名不区分大小写；该命令需要安装项目所用 Python 的 `Scripts` 目录位于 PATH。若使用项目 `.venv`，请先激活虚拟环境再输入 `DS`。

连续对话启动后会显示当前会话，输入普通文字即可提问。左侧列表默认收起；点击顶部“显示会话／收起会话”按钮或按 Ctrl+L，可展开或收起。`/switch` 会展开列表并将焦点移过去，用 ↑/↓ 选择，按 Enter 恢复并显示旧聊天记录。Ctrl+B 默认打开模型切换器，可用 ↑/↓ 和 Enter 选择模型；底栏显示当前模型，不显示模型切换快捷键。输入 `/` 可查看候选，按 Tab 补全命令。右下角显示当前会话名。在项目根目录双击 `start_dianstar.cmd`，可在独立的 PowerShell 窗口打开界面；脚本优先使用 `.venv`，否则使用 PowerShell 中已安装依赖的 `python`。程序退出或启动失败后窗口会保留，方便查看错误。

| 指令 | 作用 |
| --- | --- |
| `/new` | 创建并切换到新会话 |
| `/rename 新名称` | 重命名当前会话；只输入 `/rename` 会提示输入名称 |
| `/resume 会话名` | 按名称恢复会话；旧的会话 ID 也仍可使用 |
| `/switch` | 聚焦最近使用的会话列表，用方向键和回车选择 |
| `/config` | 打开配置界面：设置 API Key、模型列表、默认模型和快捷键（仅 TUI） |
| `/help` | 显示所有斜杆命令 |
| `/compress` | 清空后续请求的上下文，保留本地记录 |
| `/exit` | 退出程序 |

例如输入 `/resume 北京明天天气`，程序会读取该名称对应的本地会话；恢复后输入的问题会带上该会话尚未清空的上下文。名称完全相同的会话有多个时，程序不会猜选，可用 `/switch` 按时间选择。找不到会话时，当前会话保持不变。旧会话文件没有名称时，会从第一句提问推导默认名称；首次重命名或继续对话后写入新格式。`--plain` 保留旧的数字选择方式，供不支持全屏界面的终端使用。

`/config` 主菜单的各选项使用相同的方框图标。选择 `api_key` 可管理多个具名 Key，并指定一个默认 Key。新建 Key 的模型列表为空；到 `model` 页面选中该 Key，在输入框填写模型 ID 后按 Enter 或点“添加模型”。列表中每个模型占一行，选中后可点“删除选中模型”；增删都会立即保存。`default model` 也可先选 Key，再选该 Key 的默认模型。模型切换快捷键只切换当前默认 Key 的模型。“快捷键”页目前有“模型切换”及其输入框；在框内填写如 `Ctrl+B` 或 `Alt+M`，按 Enter 保存。支持 Ctrl/Alt 加字母或数字、F1–F12；与现有操作冲突的组合键会被拒绝。详情页按 Esc 返回菜单，菜单中再按 Esc 关闭配置。删除默认模型时，该 Key 会改用列表中的第一项；删除最后一个模型后，该 Key 不能发起提问，直到添加模型。配置从下一次提问开始生效；模型 ID 只做格式校验，实际可用性由 DeepSeek API 决定。Key 名称、每个 Key 的模型和快捷键保存在 `.local/config.json`，其中不包含密钥。原来的单 Key 模型配置会在首次保存时自动转为新格式。纯文本模式输入 `/config` 会提示切换到 TUI。

每轮成功问答后才写入会话文件。会话文件位于项目根目录的 `.local/conversations/`，每个 ID 对应一个 JSON 文件；其中保存会话名、可见问答，以及供模型继续对话的工具调用、工具结果和 `reasoning_content`。`/switch` 按文件最近修改时间排序。文件是本机明文，`.local/` 已被 Git 忽略。旧格式会话可继续恢复。长对话会重复发送当前上下文，增加请求内容和费用。

在 VS Code 中打开整个 `DianStar` 文件夹，选中已安装项目依赖的 Python 解释器，再从“运行和调试”使用 `.vscode/launch.json` 提供的“连续对话”“恢复会话”或“提问 DeepSeek”配置。程序应以模块 `dianstar` 启动；直接把 `dianstar/__main__.py` 当独立脚本运行会导致相对导入错误。如果 PowerShell 中文显示乱码，可先运行 `$env:PYTHONIOENCODING='utf-8'`。

## 天气工具怎样工作

`dianstar/agent.py` 编排本轮任务；`dianstar/deepseek.py` 把工具名称及参数说明发给 DeepSeek。模型请求 `get_weather(location, date)` 后，`dianstar/tools/weather.py` 用 [Open-Meteo Geocoding API](https://open-meteo.com/en/docs/geocoding-api) 查城市，再用[天气预报 API](https://open-meteo.com/en/docs) 查天气数据。程序把工具结果连同模型上一条工具调用消息及其推理内容交回模型，`dianstar/loop.py` 继续循环，由模型组织最终回答。

`date` 可为 `today`、`tomorrow` 或未来 7 天内的 `YYYY-MM-DD`。查询以目的地当地日期为准。天气工具统一逗号格式，并尝试中文城市名有无“市”字；未提供地区的中文城市名会先尝试带“市”的写法，避免优先匹配到同名乡镇。它不保存自治区简称表。工具会检查查询到的中文城市名是否与请求的城市名一致；跨语言名称尚不能完全独立核对，仍依赖地名服务的解析结果。查不到地点、地点有歧义、日期无效或服务暂不可用时，工具只返回错误代码和原请求参数，不提供天气数据。模型可结合用户问题及历史决定下一步，但循环次数和重复调用仍受程序限制。每日天气现象代表该日最强的预报天气现象，不代表全天每小时的状况。最高、最低气温为预报，当前气温也来自天气模型，并非温度计实测值。工具交给模型的数据不包含来源字段，来源在本节记录。查询需要联网；使用 Open-Meteo 免费接口前请查看其[使用条件](https://open-meteo.com/en/terms)。

## 密钥与权限边界

`.env` 和 `.local/` 已列入 `.gitignore`，因此不会因普通的 `git add .` 进入仓库。第一个 Key 对应 `.env` 中的 `DEEPSEEK_API_KEY`；新增 Key 使用 `DEEPSEEK_API_KEY_KEY2` 等变量。程序会在本机读取当前默认 Key 并通过 SDK 发送请求；DeepSeek 模型收到用户消息和工具结果，不需要收到 Key。**有本机项目文件权限的开发工具仍可能读取 `.env`**；Git 忽略规则不构成访问控制。添加文件工具之前的密钥隔离待办见 [2026-10-21 待办](./docs/开发日志/2026-10-21-待办.md)。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目功能、安装、运行和文件导航 |
| `pyproject.toml` | Python 包元数据、依赖及 `dianstar` 命令入口 |
| `.gitignore` | 排除密钥、会话、虚拟环境和缓存 |
| `.env.example` | API Key 配置格式示例，不含真实密钥 |
| `.vscode/launch.json` | VS Code 的模块运行与调试配置 |
| `dianstar/__init__.py` | Python 包入口 |
| `dianstar/__main__.py` | 命令行参数、单次问答、纯文本模式和 TUI 入口 |
| `dianstar/tui.py` | Textual 终端界面、历史显示、键盘操作及会话命令 |
| `dianstar/config_ui.py` | `/config` 的键盘菜单、Key 管理及各项配置详情页 |
| `dianstar/config.py` | 多 Key 配置、各 Key 模型列表、默认选择和快捷键持久化；密钥的本地更新 |
| `dianstar/rabbit_art.py`、`dianstar/assets/rabbit.json`、`dianstar/assets/rabbit-source.png` | 绘制随包保存的终端兔子字符画；PNG 是重新生成字符数据的素材，运行时只读取 JSON |
| `dianstar/welcome_art.py`、`dianstar/assets/kapok.json`、`dianstar/assets/kapok-source.png` | 从参考图生成木棉花点阵，绘制在兔子后层并保护兔子轮廓 |
| `scripts/build_flower_art.py` | 从原有裁剪范围生成花朵基础点阵，并用手绘点阵补足右下花瓣；仅更新素材时使用，需 Pillow |
| `start_dianstar.cmd`、`start_dianstar.ps1` | 在独立 PowerShell 窗口启动应用并保留错误信息 |
| `dianstar/conversations.py` | 会话名称和 ID、JSON 持久化、恢复及上下文起点 |
| `dianstar/agent.py` | 一轮任务的编排、工具分派与回答保存 |
| `dianstar/prompts.py` | 加载运行时系统提示词 |
| `dianstar/system_prompt.md` | 可编辑的通用助手默认提示词 |
| `dianstar/deepseek.py` | DeepSeek SDK 请求、消息转换与异常工具标记解析 |
| `dianstar/loop.py` | 有次数上限的 Think → Act → Observe 循环 |
| `dianstar/tools/__init__.py` | 工具包入口 |
| `dianstar/tools/weather.py` | 天气工具定义、城市解析、日期校验和 Open-Meteo 查询 |
| `dianstar/tools/weather_contract.py` | 裁剪发送给模型的天气数据 |
| `tests/test_deepseek.py` | 问答、会话和模型工具调用的离线测试 |
| `tests/test_cli.py` | 终端 Markdown 回答展示测试 |
| `tests/test_tui.py` | 终端界面的补全、切换会话和问答保存测试 |
| `tests/test_config.py` | 模型配置、Key 保存和配置界面的测试 |
| `tests/test_conversations.py` | 会话名称、旧格式兼容和时间排序测试 |
| `tests/test_loop.py` | 循环停止规则测试 |
| `tests/test_weather.py` | 地名、日期与天气数据解析测试 |
| `tests/data/weather_cities.csv` | 覆盖全球六个地区的 100 个城市及预期国家代码 |
| `scripts/__init__.py` | 验证脚本包入口 |
| `scripts/check_weather_cities.py` | 联网批量检查天气工具并生成逐城市报告 |
| `scripts/build_rabbit_art.py` | 从原图重新生成兔子像素数据；仅更新素材时使用，需 Pillow |
| `docs/架构.md` | 当前模块边界、任务流程和数据权限说明 |
| `docs/测试记录-2026-10-20.md` | 离线、真实问答和 100 城市天气查询的测试结果与边界 |
| `docs/产品文档.md` | 长期产品目标；其中的规划不代表已经实现 |
| `docs/编程语言选型.md` | Python 选型与历史 Rust 原型的取舍 |
| `docs/参考技术.md` | 供后续设计参考的外部技术笔记 |
| `docs/开发日志/2026-10-13.md` | 历史 Rust 阶段的开发记录 |
| `docs/开发日志/2026-10-18.md` | Python 问答、终端会话与保存功能的开发记录 |
| `docs/开发日志/2026-10-19.md` | 天气工具及真实调用的开发记录 |
| `docs/开发日志/2026-10-21-待办.md` | 文件读取工具和 API Key 隔离的后续任务 |

运行时还会出现三个**不进入 Git**的本地路径：`.env` 存放用户自己的多个 Key，`.local/config.json` 存放 Key 名称、各自的模型设置和默认选择，`.local/conversations/` 存放会话。此前的 Rust 只读原型保存在 Git 历史提交 `7729740`，不属于当前 Python 运行入口。

## 验证与下一步

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

离线测试使用模拟的模型与天气响应，不需要真实 Key。真实 DeepSeek 问答和天气调用已分别验证；实际联网结果会随时间与服务状态变化。下一步是先界定文件工具可读取的范围和密钥隔离，再添加受限的 `list_files` / `read_file`。

要检查全球 100 个城市的真实天气查询，在项目根目录运行：

```powershell
.\.venv\Scripts\python.exe -m scripts.check_weather_cities
```

脚本只调用 Open-Meteo，不读取 `.env`，也不向 DeepSeek 发送请求。它检查返回地点的国家代码、当地日期以及气温字段，把逐项结果写入 `.local/weather_city_report.json`。少量试跑可加 `--limit 3`；任一城市失败时脚本以非零状态退出。联网服务暂时不可用也会记为错误，不能据此断定程序逻辑有误。

本次测试的具体结果见 [2026-10-20 测试记录](./docs/测试记录-2026-10-20.md)。
