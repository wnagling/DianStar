# MCP 接入与 Git 工程操作

返回 [项目首页](../README.md)。

DianStar 作为 MCP 客户端，连接本地 stdio Server。当前接入内置 Git 工程服务，以及用户安装的 GitHub 官方 MCP Server；GitHub 支持仓库搜索、PR、Issue、CI 与代码审查。暂不支持任意服务器、HTTP 传输、MCP prompts/resources 或服务器请求模型采样。

## 配置

更新依赖后重启：`python -m pip install -e .`。进入 `/config` → MCP。

1. 本地 Git：勾选启用，每行填写一个允许访问的仓库绝对路径。需已安装 Git，并且仓库是 `.git` 目录位于仓库内的普通工作区；暂不支持 linked worktree、子模块工作区和 bare 工作区。需要提交、推送等操作时，再勾选“允许申请 Git 写操作（每次单独确认）”；旧配置仍保持只读，模型不能自行开启。
2. GitHub：从 [GitHub 官方发布页](https://github.com/github/github-mcp-server/releases)选择对应系统的固定版本程序，检查发布说明与校验信息，解压到自己的可信目录。在界面填写程序的绝对路径，Windows 使用 `.exe`；每行填写一个允许访问的仓库，例如 `wnagling/DianStar`。私有访问和申请写操作各有独立仓库列表，必须属于允许访问列表；旧配置及空列表保持公开只读。允许读取私有内容表示允许工具把这些内容发送给当前模型。
3. 填写 GitHub Token 后保存，或在 `.env` / 环境变量中设置 `GITHUB_PERSONAL_ACCESS_TOKEN`。仅授予目标仓库与所需功能：例如仓库内容读取、Actions 读取、Issues 或 Pull requests 读写；服务还可能需要用户身份读取权限，实际受 Token 类型、仓库及组织策略限制。写操作预览会通过固定 GitHub API 读取当前资源状态；审查线程核验使用固定 GraphQL 查询。Token 留空保留原值，默认遮挡，离开页面清空输入框；Token 不写入 MCP JSON 或模型工具定义。删除凭据可从 `.env` 移除对应项，并检查是否存在同名环境变量。
4. 点击测试连接会启动指定服务，报告本次连接状态和开放工具数，随后关闭连接。测试不会调用模型或修改真实仓库，也不代表真实 GitHub 查询一定成功。保存后下一次提问生效；保存本身不启动服务。

`.local/mcp.json` 示例（路径替换为自己的实际目录，默认两项均关闭）：

```json
{
  "git_enabled": true,
  "git_write_enabled": false,
  "git_repositories": ["C:\\work\\my-project"],
  "github_enabled": false,
  "github_executable": "",
  "github_repositories": [],
  "github_private_repositories": [],
  "github_write_repositories": []
}
```

每项最多配置 10 个仓库。本地路径保存时解析为真实绝对路径，执行时再次检查。配置无效时不启用 MCP，普通对话仍可运行；模型可通过 `mcp_status` 查看错误。配置入口只由用户操作，模型不能修改服务器程序、凭据、仓库范围或启用状态。

## 可用能力

| 工具 | 参数与范围 |
| --- | --- |
| `mcp_status` | 无参数，列出本轮服务状态、允许仓库和已注册工具 |
| `mcp_git__git_status` | `repository` 为配置列出的绝对路径；显示分支、暂存/未暂存、重命名/冲突以及未忽略的新文件状态，不读取新文件正文 |
| `mcp_git__git_diff` | `repository`；可选布尔值 `staged`，默认 false，查看工作区或暂存区差异 |
| `mcp_git__git_log` | `repository`；可选整数 `limit`，默认 10，范围 1–30；只返回提交短哈希、日期和标题 |
| `mcp_git__git_branches` | 本地分支、上游、基于本地跟踪记录的领先/落后状态；不是实时远程信息 |
| `mcp_git__git_remotes` | 远程名称及通过检查的 fetch/push 地址；地址内含密码等情况拒绝展示原值 |
| `mcp_git__git_stage` / `git_unstage` | 明确的仓库相对文件路径数组 `files`，1–100 个；不接受目录、通配式全量暂存、越界、链接或受限密钥路径；取消暂存不改写工作区 |
| `mcp_git__git_create_branch` | `branch`；可选 `start_point` 为 HEAD 或已有本地分支，`checkout` 默认 false；有修改时只允许从当前 HEAD 创建并切换 |
| `mcp_git__git_switch_branch` | `branch` 为已有本地分支，要求暂存区和工作区干净；不强制覆盖 |
| `mcp_git__git_commit` | `message` 最多 4000 字符；展示全部暂存差异和 Git 身份，提交批准的索引快照；不自动暂存、不包含未暂存修改 |
| `mcp_git__git_fetch` | `remote` 默认 origin；获取其分支到本地远程跟踪引用，不合并、不切换、不删除旧引用或拉取标签 |
| `mcp_git__git_push` | 必填目标 `branch`；`remote` 默认 origin；`set_upstream` 默认 false。固定批准的当前提交，普通推送到一个明确分支，随后核验远程 SHA；不强制、不推送其他分支或标签 |
| `mcp_github__get_file_contents` / `get_repository_tree` | 读取远程文件、目录或仓库树 |
| `mcp_github__search_code` / `search_issues` / `search_pull_requests` | 沿用官方参数；代码搜索仅使用官方 `query` 等字段，不增加 `owner/repo`。每个 OR 分支须限定允许的 `repo:owner/repo`；Issue/PR 搜索也支持其官方可选 `owner/repo` 参数 |
| `mcp_github__issue_read` / `list_issues` | Issue 内容、评论、标签及父子任务 |
| `mcp_github__pull_request_read` / `list_pull_requests` | PR 内容、diff、文件、提交、评论、审查、状态与具体 check runs |
| `mcp_github__actions_list` / `actions_get` / `get_job_logs` | 工作流、运行记录、任务、产物信息与日志；日志正文使用官方 `return_content=true`，可设置 `tail_lines`。产物/日志链接不会被程序自动下载或执行 |
| `mcp_github__list_commits` / `get_commit` / Release、分支、标签读取 | 保留原有远程读取能力 |
| `mcp_github__create_pull_request` / `update_pull_request` | 创建 PR（包括 `draft=true`）或更新标题、正文、状态及其他官方支持的字段；须在写操作仓库列表内并逐次确认 |
| `mcp_github__issue_write` / `add_issue_comment` | 创建、更新 Issue 与评论；涉及其他仓库的父任务须同时获得相应范围 |
| `mcp_github__pull_request_review_write` | 创建、提交/删除待提交审查，解决/重新打开审查线程；仅暴露实际服务器支持的方法，v2.0.3 使用 `submit_pending` / `delete_pending` |
| `mcp_github__add_comment_to_pending_review` / `add_reply_to_pull_request_comment` | 向待提交审查添加行级/文件级评论，或回复审查评论；沿用官方 `pullNumber`、`commentId`、`subjectType` 等参数及枚举 |

GitHub 不直接开放整个工具集。客户端使用固定读/写工具清单，方法枚举与服务器实际定义取交集。正式参数名、类型和必填项沿用官方 Schema，不强行为所有工具增加 `owner/repo`；保留 `title` 等正式字段，允许按官方定义用空正文清空内容。新增工具不会自动获得权限，服务的 `readOnlyHint` 不能代替本地策略。未配置写操作仓库时，官方 Server 仍以 `--read-only` 启动；配置后只开启指定工具，写工具不会继承读取授权。

每次 GitHub 调用前先验证仓库范围。普通仓库使用不带 Token 的固定元数据接口确认公开性及名称；明确允许私有访问的仓库使用凭据检查名称及可访问性。失败、重定向或限流不会自动扩大范围。搜索按 Boolean 分支检查仓库限定，不改写原 query；已识别的搜索结果仓库信息也会再次检查。Token 能访问其他仓库，不代表这些仓库被配置允许。应用侧范围检查不能替代 Token 自身权限或操作系统隔离。

## 执行与授权

调用流程：模型提出工具动作 → JSON Schema 校验 → 现有 ToolGateway 检查本轮权限及额度 → MCP 适配层检查仓库与操作 → Server 执行 → 工具结果进入上下文。

已由用户在配置中启用的服务，每轮分别授予最多 30 次只读调用；Git 和 GitHub 写操作预览分别另限 30 次。写能力默认关闭，即使开启也不会得到自动执行授权；自然语言任务、网页或仓库里的文字不能代替确认弹窗。任务受统一 16 轮 ReAct 上限约束，失败计入预算。

GitHub 写操作先检查仓库、方法及凭据，再读取目标当前状态，展示完整待提交参数及相关当前内容。待提交审查按当前账号查找，分页读取已有评论供确认，缺少待提交审查时不盲目提交；预览分页最多 10 页、每页 100 项，超出直接停止。应用内部预览绑定操作和完整参数，10 分钟有效、单次消费。确认后重新检查范围及目标状态；变化则停止并要求重新核对。预览超过 24000 字符不截断后批准。HTTP/API 返回的状态检查与写操作之间仍存在时间窗口，GitHub 工具没有提供统一的原子条件写入，不能声称消除了全部远程竞态。写请求连接中断、取消或返回无法核验的结果时，不冒称成功；先查询实际结果，本轮不自动重复该请求。

v2.0.3 的 26 个目标工具已通过真实 stdio 发现与 Schema 兼容验收；老版本缺少的工具在 `mcp_status` 的 `unavailable_tools` 中列出，不会假装可用。详细范围见 [GitHub 开发工具验收](./测试记录-2026-10-27-GitHub开发工具.md)。

Git 操作先由服务端检查并生成预览，应用才显示确认。暂存和提交展示文件与差异；push 展示远程地址、源提交、目标分支及基于本地跟踪记录估算的待推送提交。完整预览超过 24000 字符时要求缩小范围，不能隐藏差异后批准。fetch 涉及网络和跟踪引用更新，也单独确认。

应用保存内部预览令牌，模型看不到该令牌，也不能调用内部 `git_apply`。令牌绑定当前服务实例、仓库、操作参数及状态快照，10 分钟有效、单次使用；关闭会话连接即失效。批准后重查 HEAD、分支、索引、引用、仓库配置、有关文件和换行设置。变化则停止；无确认入口、取消、撤销或过期都不执行。commit 使用私有索引生成批准的树，再以旧 HEAD 为条件更新分支；push 使用批准的完整提交 SHA，避免推送后来新增的提交。

操作后返回实际状态和提交 SHA；push 额外读取远程目标核验，并可在成功核验后设置上游。非快进被拒绝时停止，不自动合并、重置或强推；超时/断连可能发生在修改之后，因此返回“结果待核验”，先检查状态，不能自动重试或宣称没有发生任何修改。外部进程不受应用锁控制，这些检查不提供主机级原子隔离。

每轮提问建立连接、发现工具，并在结束/取消时清理；没有跨轮常驻连接。连接初始等待最多 15 秒，普通 MCP 调用约 12 秒、客户端等待最多 14 秒；Git 执行调用约 70 秒、客户端最多 75 秒，确认等待不计入执行超时。发现最多 4 页、128 个工具，重复名称或不安全定义不放行。工具列表变更通知使该连接失效，本轮不自动接受新定义，下轮重新发现。

只保留文本、已经随结果返回的内嵌文本及结构化 JSON，不自动打开图片、资源链接或下载附件。送入上下文的单次内容最多 24000 字符，截断时标记。GitHub 对象中 API 实际返回的同仓库 `html_url` 和已返回文本资源的同仓库 URI 纳入来源台账；正文里的任意链接不自动视为已读取。MCP 结果和原始资料均不构成授权，不能修改付费访问策略或其他权限。

## 本地执行边界

内置 Git Server 只构造固定操作，不接受任意命令、修订表达式、force 参数或用户指定程序。文件参数逐项检查，并使用字面路径匹配；不自动暂存全部文件。指定 Git 目录和工作区，关闭外部 diff、textconv、fsmonitor、hooks、签名、自动维护、子模块递归、分页器和交互提示；有冲突或正在 merge/rebase 等整合时拒绝写操作。Git hooks 不运行，因此提交成功不代表通过 lint、测试、签名或项目校验。

检查本地 Git 配置键名，不跟随 include；含 filter、include/includeIf、URL 重写、split index、partial clone 或独立工作区配置时拒绝。Git 元数据目录中的符号链接/junction、外部对象目录和共享元数据也不支持。系统/全局配置不整体载入操作，仅显式读取已知换行设置；提交身份优先本地，缺失时只读取全局 user.name/user.email，不允许模型指定作者。普通子命令最多 8 秒，网络子命令最多 30 秒，输出读取最多 2 MiB，给模型的内容仍限 24000 字符。

网络仅使用仓库已配置的单一 HTTPS/SSH 或绝对本地路径，不接受临时任意 URL、自定义传输、内嵌 HTTP 凭据或 HTTPS 重定向。SSH 使用已知主机验证、非交互模式及现有 agent/默认密钥，不自动接受新主机或执行用户 SSH 配置；Windows HTTPS 使用已有 Git Credential Manager 凭据且禁止弹出登录，未登录时需先在终端完成认证。GitHub MCP Token 不自动用于 Git 推送。网络步骤继承显式代理/系统代理；始终验证 HTTPS 证书。获取与推送地址不同时，不自动设置上游。真正远程服务端的 hooks 和分支保护仍由服务端执行。

diff 排除常见 `.env*`、`.pem`、`.key`、credentials、secrets 名称；这不是通用隐私检测。已跟踪的其他文件可能含敏感内容，状态与提交标题也可能包含私人信息。配置允许仓库意味着允许把这些查询结果发送给当前模型服务，请先用公开或测试仓库验收。

GitHub Server 从 SDK 的小型平台运行环境及显式代理变量启动，只额外传入自己的 GitHub Token 与指定读/写工具配置，不传入模型 Key。错误不给模型回传原始异常或服务器 stderr；返回文本会遮挡匹配本机已知密钥的字面值，不能保证检测其他秘密、编码或拆分泄漏。

这些是应用层约束，**不是操作系统沙箱**。本地 MCP 程序仍以当前用户身份运行，可能有文件和网络权限；只运行可信来源程序，固定版本，使用最小权限凭据。Git 元数据、可执行文件和工作区应由用户控制；并发修改、恶意底层程序、路径竞态和主机级资源耗尽未被彻底隔离。用户选择的程序不是由模型指定。

## 手动测试请求

- “看看配置的本地仓库有哪些已跟踪文件修改，区分工作区和暂存区。”
- “总结这个仓库最近 5 次提交，说明主要变化。”
- “读取已配置 GitHub 仓库的 README，介绍项目用途，并给出来源。”
- “看看这个 GitHub 仓库最近的未关闭 issue，归纳前三项需要关注的问题。”
- “从当前分支创建并切换到 feature/demo，把 one.txt 暂存并提交，其他修改保留。”——分支、暂存和提交分别确认。
- “将当前提交推送到 origin 的 feature/demo 分支并设置上游。”——确认目标地址和分支，成功后核验远程 SHA。
- “获取 origin 的最新分支，看看当前分支是否落后。”——fetch 不应改写工作区或合并代码。

pull、merge、rebase、强制推送、删除分支、worktree、冲突处理及通用文件编辑属于后续阶段。Git 的远程凭据与 GitHub MCP Token 分别管理，各自遵守已配置范围；GitHub MCP 私有读取和开发协作能力已按上文方式接入。
