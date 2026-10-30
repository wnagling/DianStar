# MCP 接入与 Git 工程操作

返回 [项目首页](../README.md)。

DianStar 内部通过 MCP 连接本地 stdio 服务。当前接入内置 Git 工程服务和应用管理的 GitHub 官方连接组件；GitHub 支持仓库搜索、PR、Issue、CI 与代码审查。暂不支持任意服务器、HTTP 传输、MCP prompts/resources 或服务器请求模型采样。

## 自动接入

MCP 是 DianStar 内部协议，普通用户无需了解或配置。设置菜单不再有 MCP 入口，对应设置页面已删除；本地 Git、项目识别和工具连接由应用自动管理。用户直接提出克隆、修改、提交、推送或 GitHub 协作任务，涉及修改时按后台策略在执行前确认。

GitHub 组件优先使用应用管理的固定版本 v2.0.3，并校验可执行文件 SHA256；其次发现系统 PATH 中已安装的 github-mcp-server。便携包随附已校验的组件及许可证。已有后台显式程序覆盖仍保留；不会执行项目资料指定的程序。聊天过程中不自动下载或执行未校验组件。

GitHub 身份认证是独立前提：通过 `/github` 或设置中的“GitHub 账号”完成浏览器登录或输入令牌，使用系统凭据库中 DianStar 专用位置。账号核验成功后供后续提问使用，不修改项目范围和审批。没有专用账号状态时兼容原有环境凭据及明确允许的 Git 凭据后备；退出会停止所有后备来源，不删除外部凭据。缺少组件时提示检查应用安装，不恢复 MCP 设置页面。见 [账号登录验收](./测试记录-2026-10-30-GitHub账号登录.md)。

## 后台连接与策略（维护者）

.local/mcp.json 继续兼容已有仓库、目标目录、组件覆盖和模式策略，普通用户不需要编辑。自动识别当前项目与 GitHub origin，不把整个父目录或全账号仓库纳入范围。enabled 与 requires_approval 两项后台工具策略继续有效，默认修改动作逐次确认；未来只读/全权限模式接口保持不变。

已明确限制的仓库范围、私有内容范围和账号权限检查继续有效。普通任务只提供具体的 open_project 项目打开能力，不提供 get_mcp_configuration/configure_mcp；内部配置工具仅可由可信后端显式注册。打开新项目时确认项目名、完整路径和关联主项目，只记住该项目，不改后台功能策略，也不提交/推送。当前项目无需重复打开。连接随提问自动建立与释放，mcp_status 供 Agent 内部诊断实际生效范围，不用静态配置列表误判当前项目不可用。

固定 Windows 组件路径为 .local/tools/github-mcp-server/v2.0.3/github-mcp-server.exe，SHA256 为 647fe7e7f3c1296d1acb936b5c7704bbcdb6bda95b4533c524157446b33f0f16。源码安装的组件依赖由安装维护流程准备，打包脚本会核验并随包复制，不携带制作者账号或配置。

## 可用能力

| 工具 | 参数与范围 |
| --- | --- |
| `mcp_status` | 无参数，列出本轮服务状态、允许仓库和已注册工具 |
| `mcp_git__git_status` | `repository` 为 mcp_status 列出的配置、当前项目或确认创建的仓库绝对路径；显示分支、暂存/未暂存、重命名/冲突以及未忽略的新文件状态，不读取新文件正文 |
| `mcp_git__git_diff` | `repository`；可选布尔值 `staged`，默认 false，查看工作区或暂存区差异 |
| `mcp_git__git_log` | `repository`；可选整数 `limit`，默认 10，范围 1–30；只返回提交短哈希、日期和标题 |
| `mcp_git__git_branches` | 本地分支、上游、基于本地跟踪记录的领先/落后状态；不是实时远程信息 |
| `mcp_git__git_remotes` | 远程名称及通过检查的 fetch/push 地址；地址内含密码等情况拒绝展示原值 |
| `mcp_git__git_stage` / `git_unstage` | 明确的仓库相对文件路径数组 `files`，1–100 个；不接受目录、通配式全量暂存、越界、链接或受限密钥路径；取消暂存不改写工作区 |
| `mcp_git__git_create_branch` | `branch`；可选 `start_point` 为 HEAD 或已有本地分支，`checkout` 默认 false；有修改时只允许从当前 HEAD 创建并切换 |
| `mcp_git__git_switch_branch` | `branch` 为已有本地分支，要求暂存区和工作区干净；不强制覆盖 |
| `mcp_git__git_commit` | `message` 最多 4000 字符；展示全部暂存差异和 Git 身份，提交批准的索引快照；不自动暂存、不包含未暂存修改 |
| `mcp_git__git_publish` | 明确的 `files`（1–100 个）、`message`、目标 `branch`，可选 `remote` 和 `set_upstream`；一次确认后完成选定文件暂存、提交、普通推送与远程核验。预览不改真实暂存区；不会混入未选的已有暂存内容，不接受强推或删除 |
| `mcp_git__git_fetch` | `remote` 默认 origin；获取其分支到本地远程跟踪引用，不合并、不切换、不删除旧引用或拉取标签 |
| `mcp_git__git_push` | 必填目标 `branch`；`remote` 默认 origin；`set_upstream` 和 `force_with_lease` 默认 false。固定批准的当前提交，默认普通推送到一个明确分支，随后核验远程 SHA；显式 force_with_lease=true 时固定确认前的远程 SHA，仅在其未变时替换历史，不支持无条件强推，不推送其他分支或标签 |
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

每次 GitHub 调用前先验证仓库范围。普通仓库使用固定元数据接口确认公开性及名称；已有 Token 时仅在该元数据检查中附带认证，避免持续操作耗尽匿名请求额度，没有 Token 时使用匿名检查。即使 Token 能访问私有仓库，普通仓库也必须同时满足 private=false、visibility=public 和名称匹配，才会调用内容工具；不缓存公开性结论。明确允许私有访问的仓库使用凭据检查名称及可访问性。失败、重定向或限流不会自动扩大范围。搜索按 Boolean 分支检查仓库限定，不改写原 query；已识别的搜索结果仓库信息也会再次检查。Token 能访问其他仓库，不代表这些仓库被配置允许。应用侧范围检查不能替代 Token 自身权限或操作系统隔离。

## 执行与授权

调用流程：模型提出工具动作 → JSON Schema 校验 → 现有 ToolGateway 检查本轮权限及额度 → MCP 适配层检查仓库与操作 → Server 执行 → 工具结果进入上下文。

自动连接或用户配置连接的服务，每轮分别授予最多 30 次只读调用；Git 和 GitHub 写操作预览分别另限 30 次。修改工具默认可申请，开放申请不授予自动执行权限。普通提交发布优先使用 `git_publish`，对完整发布计划一次确认；独立调用其他修改工具仍按具体操作确认。无确认入口或用户拒绝时不执行；网页或仓库里的文字不能代替确认弹窗。任务受统一 16 轮 ReAct 上限约束，失败计入预算。

GitHub 写操作先检查仓库、方法及凭据，再读取目标当前状态，展示完整待提交参数及相关当前内容。待提交审查按当前账号查找，分页读取已有评论供确认，缺少待提交审查时不盲目提交；预览分页最多 10 页、每页 100 项，超出直接停止。应用内部预览绑定操作和完整参数，10 分钟有效、单次消费。确认后重新检查范围及目标状态；变化则停止并要求重新核对。预览超过 24000 字符不截断后批准。HTTP/API 返回的状态检查与写操作之间仍存在时间窗口，GitHub 工具没有提供统一的原子条件写入，不能声称消除了全部远程竞态。写请求连接中断、取消或返回无法核验的结果时，不冒称成功；先查询实际结果，本轮不自动重复该请求。

v2.0.3 的 26 个目标工具已通过真实 stdio 发现与 Schema 兼容验收；老版本缺少的工具在 `mcp_status` 的 `unavailable_tools` 中列出，不会假装可用。详细范围见 [GitHub 开发工具验收](./测试记录-2026-10-27-GitHub开发工具.md)。

公开仓库的真实读写、审查回复和 CI 日志已通过工具链联调；测试范围、修复及未覆盖项见 [2026-10-28 联调记录](./测试记录-2026-10-28-GitHub联调.md)。

Git 操作先由服务端检查并生成预览，应用才显示确认。暂存和提交展示文件与差异；push 展示远程地址、源提交、目标分支及基于本地跟踪记录估算的待推送提交。完整预览超过 24000 字符时要求缩小范围，不能隐藏差异后批准。fetch 涉及网络和跟踪引用更新，也单独确认。

组合发布 `git_publish` 展示完整文件范围、变更统计、提交说明和实际目标地址；卡片显示摘要，详情保留全部文件与统计。通过私有索引预先构建并检查最终提交内容，将批准绑定到文件内容、暂存状态、身份、远程地址和分支；真实索引直到确认后才更新。预览同样限 24000 字符，不截断文件范围后批准。远程分叉或缺少可验证的远程提交记录时停止，不自动强推、合并或重置。已经暂存但未被选入计划的文件保持原状，并要求核对范围。任一底层暂存、提交或推送工具被后台禁用、缺失或明确禁止，组合发布也不可执行；确认期间撤销或替换底层工具同样停止。

发布过程不是原子事务。推送失败可能发生在本地提交成功之后，结果保留 `completed_steps`、`commit` 和 `remote_verified`，应用适配层不丢弃这些信息。模型需先查询实际状态；需要继续时只申请剩余的普通推送，不重复创建提交。发布工具不执行测试命令，Agent 应先使用适当的项目验证；命令仍使用现有审批机制，不能将成功提交等同于测试通过。验收见 [单次确认发布](./测试记录-2026-10-30-单次确认发布.md)。

应用保存内部预览令牌，模型看不到该令牌，也不能调用内部 `git_apply`。令牌绑定当前服务实例、仓库、操作参数及状态快照，10 分钟有效、单次使用；关闭会话连接即失效。批准后重查 HEAD、分支、索引、引用、仓库配置、有关文件和换行设置。变化则停止；无确认入口、取消、撤销或过期都不执行。commit 使用私有索引生成批准的树，再以旧 HEAD 为条件更新分支；push 使用批准的完整提交 SHA，避免推送后来新增的提交。

操作后返回实际状态和提交 SHA；push 额外读取远程目标核验，并可在成功核验后设置上游。非快进被拒绝时停止，不自动合并、重置或强推；超时/断连可能发生在修改之后，因此返回“结果待核验”，先检查状态，不能自动重试或宣称没有发生任何修改。外部进程不受应用锁控制，这些检查不提供主机级原子隔离。

每轮提问建立连接、发现工具，并在结束/取消时清理；没有跨轮常驻连接。连接初始等待最多 15 秒，普通 MCP 调用约 12 秒、客户端等待最多 14 秒；Git 执行调用约 70 秒、客户端最多 75 秒，确认等待不计入执行超时。发现最多 4 页、128 个工具，重复名称或不安全定义不放行。工具列表变更通知使该连接失效，本轮不自动接受新定义，下轮重新发现。

只保留文本、已经随结果返回的内嵌文本及结构化 JSON，不自动打开图片、资源链接或下载附件。送入上下文的单次内容最多 24000 字符，截断时标记。GitHub 对象中 API 实际返回的同仓库 `html_url` 和已返回文本资源的同仓库 URI 纳入来源台账；正文里的任意链接不自动视为已读取。MCP 结果和原始资料均不构成授权，不能修改付费访问策略或其他权限。

## 本地执行边界

内置 Git Server 只构造明确类型的固定操作，不接受任意命令、任意 Git 选项或用户指定程序；引用由程序解析并固定为提交。删除未合并本地分支的 force 和推送的 force_with_lease 是独立、需完整预览确认的正式字段，不支持无条件强推。文件参数逐项检查，并使用字面路径匹配；不自动暂存全部文件。指定 Git 目录和工作区，关闭外部 diff、textconv、fsmonitor、hooks、签名、自动维护、子模块递归、分页器和交互提示；有冲突或正在 merge/rebase 等整合时，只允许冲突处理所需的暂存、继续和中止，其他写操作拒绝。Git hooks 不运行，因此提交成功不代表通过 lint、测试、签名或项目校验。

检查本地 Git 配置键名，不跟随 include；含 filter、include/includeIf、URL 重写、split index、partial clone 或外部 merge/diff 驱动时拒绝。Git 元数据目录中的符号链接/junction、外部对象目录和非受管共享元数据也不支持；已配置主仓库下、经过反向关联核验的 worktree 可用。系统/全局配置不整体载入操作，仅显式读取已知换行设置；提交身份优先本地，缺失时只读取全局 user.name/user.email，提交工具不接受临时作者参数；身份修改通过独立仓库配置工具展示并确认，不能编造作者。普通子命令最多 8 秒，网络子命令通常最多 30 秒，clone 最多 180 秒，输出读取最多 2 MiB，给模型的内容仍限 24000 字符。

已有仓库网络操作使用已配置的单一 HTTPS/SSH 或绝对本地路径；clone 和远程配置工具可在完整预览后使用明确的 HTTPS/SSH 地址或允许范围内本地路径，不接受自定义传输、内嵌 HTTP 凭据或 HTTPS 重定向。创建范围不接受 UNC 网络共享，先检查允许范围再解析实际路径。SSH 使用已知主机验证、非交互模式及现有 agent/默认密钥，不自动接受新主机或执行用户 SSH 配置；GitHub HTTPS 在专用账号登录后使用 DianStar 的系统凭据库位置，网络操作中不弹出登录；未登录时可先用 /github 认证。SSH 身份仍独立管理，其他主机不使用此 GitHub 登录。网络步骤继承显式代理/系统代理；始终验证 HTTPS 证书。获取与推送地址不同时，不自动设置上游。真正远程服务端的 hooks 和分支保护仍由服务端执行。

diff 排除常见 `.env*`、`.pem`、`.key`、credentials、secrets 名称；这不是通用隐私检测。已跟踪的其他文件可能含敏感内容，状态与提交标题也可能包含私人信息。配置允许仓库意味着允许把这些查询结果发送给当前模型服务，请先用公开或测试仓库验收。

GitHub Server 从 SDK 的小型平台运行环境及代理设置启动：优先保留显式代理环境变量，缺失时将 Python 发现的系统代理（包括 Windows 系统代理）转换为子进程代理变量。只额外传入自己的 GitHub Token 与指定读/写工具配置，不传入模型 Key。错误不给模型回传原始异常或服务器 stderr；返回文本会遮挡匹配本机已知密钥的字面值，不能保证检测其他秘密、编码或拆分泄漏。

这些是应用层约束，**不是操作系统沙箱**。本地 MCP 程序仍以当前用户身份运行，可能有文件和网络权限；只运行可信来源程序，固定版本，使用最小权限凭据。Git 元数据、可执行文件和工作区应由用户控制；并发修改、恶意底层程序、路径竞态和主机级资源耗尽未被彻底隔离。用户选择的程序不是由模型指定。

## 手动测试请求

- “看看配置的本地仓库有哪些已跟踪文件修改，区分工作区和暂存区。”
- “总结这个仓库最近 5 次提交，说明主要变化。”
- “读取已配置 GitHub 仓库的 README，介绍项目用途，并给出来源。”
- “看看这个 GitHub 仓库最近的未关闭 issue，归纳前三项需要关注的问题。”
- “从当前分支创建并切换到 feature/demo，把 one.txt 暂存并提交，其他修改保留。”——分支、暂存和提交分别确认。
- “将当前提交推送到 origin 的 feature/demo 分支并设置上游。”——确认目标地址和分支，成功后核验远程 SHA。
- “获取 origin 的最新分支，看看当前分支是否落后。”——fetch 不应改写工作区或合并代码。

Git 工程能力与仓库管理已扩展，完整参数分组和验收见 [Git 工程补全记录](./测试记录-2026-10-28-Git工程补全.md)。通过 /github 登录后，GitHub API 和 GitHub HTTPS 操作使用应用专用的系统凭据库位置；凭据不写入项目配置。旧 Git 凭据后备仍由后台 github_use_git_credentials 管理，默认不启用，不再提供 MCP 设置页。

## Git 工程工具与日常接入

git_workspace_roots 可限制创建仓库/worktree 的父目录；默认策略下未配置此范围时，clone/init/worktree 可确认具体目标，父目录必须已存在，禁止网络共享或链接。确认只对应本次目标，不信任父目录中的其他仓库。workspace_write_enabled 缺省/null 默认逐次申请，false 可禁止文件编辑、单文件删除和命令；github_admin_repositories 缺省/null 使用当前项目，自定义列表必须属于 github_repositories，空列表禁止管理。github_use_git_credentials 仅控制旧 Git 凭据后备，缺省不启用；专用登录优先，退出状态阻止全部后备来源。

| 工具组 | 行为 |
| --- | --- |
| git_clone / git_init | repository 为目标绝对路径；clone 的 url 必填，branch 可选、depth 默认 0（完整），不覆盖现有目标；init 默认 main，也可在允许目录内初始化已有普通项目 |
| git_remote_set / git_remote_remove / git_set_upstream / git_checkout_remote | 管理明确的远程和上游，检出已获取的远程分支；不隐式创建任意远程地址 |
| git_config_read / git_config_set | 查看 local/global 已知配置及来源；只修改仓库级 user.name/user.email、core.autocrlf/eol/safecrlf、pull.ff/rebase，可用 remove=true 移除本地覆盖，不修改全局、执行命令或凭据键 |
| git_show / git_diff_refs | 查看提交详情、历史文件或两个明确提交的差异；source/ref 不接受任意命令或修订范围表达式 |
| git_pull | 必填 branch，remote 默认 origin；获取并核验批准的远程提交，只允许快进，分叉保留本地代码 |
| git_checkpoint / git_restore / git_revert | 检查点保留 HEAD、索引和用户工作文件，排除敏感及忽略文件，返回 refs/dianstar/checkpoints/...；restore 的 source/files 选择恢复，revert 撤销提交并保留历史 |
| git_stash_save/apply/drop/list | save 包含未跟踪文件，敏感路径拒绝入 stash；apply 使用固定 SHA 并恢复索引，不自动删除；drop 单独确认 |
| git_worktree_add/remove/list | 默认逐次确认具体目标，配置创建范围时须在范围内；关联主仓库须已接入或由当前项目识别；重启后核验 gitdir/commondir 反向关联，移除前要求无修改、未跟踪或忽略文件，不强制删除 |
| git_merge/rebase/cherry_pick、git_conflicts、git_conflict_continue/abort | 返回 completed/conflicts/in_progress；冲突保留现场，不自动反复执行，允许编辑、暂存后继续，或明确确认中止 |
| git_branch_rename/delete、git_remote_branch_delete | 删除本地分支默认要求已合并；force=true 明确申请删除未合并分支；远程删除固定批准的 SHA，执行时使用 lease 并回读，不删除其他分支 |

clone/init/worktree_add 成功后立即加入本轮允许仓库；应用保存到当前日常 mcp.json，下一轮继续可用。若保存失败，返回 configuration_saved=false，已完成的 Git 操作不会冒称回滚。目标、索引、配置、引用、工作文件或整合状态在确认期间变化，会拒绝执行。

workspace_list/read/search 读取已接入仓库的本地文件；workspace_edit 使用 create/replace/overwrite，replace 必须唯一匹配；workspace_delete 只删除明确文件、不递归目录；workspace_run 用 argv 数组启动进程，无隐式 shell，超时 1–600 秒，返回退出码及截断标记，超时/取消处理子进程。需要 shell 时必须在 argv 中明确请求对应解释器。所有这些工具都不是操作系统沙箱，进程仍拥有当前用户的系统权限；依赖脚本也可能产生外部副作用。

GitHub 管理工具放在 tools/local/github_admin.py，使用固定主机的官方 REST API，名称为 github_repository_settings_get/update、github_branch_protection_get/update、github_collaborators_list/set/remove；没有向官方 MCP Schema 增加私有字段。支持描述、主页、默认分支、可见性、功能和合并开关；分支保护按当前设置合并修改，保留未请求改变的审批与管理员规则；协作者使用官方 permission 字段，明确区分已加入和待接受邀请。写操作都经完整预览、确认、状态重查和回读，结果不明不自动重试。管理员 Token 不自动扩大允许仓库范围。
