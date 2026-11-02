# Hello World 的审批与耗时验收

日期：2026-11-01。

## 原问题与实际耗时

用户反馈的流程把多轮 Go 查找、WSL 探测、新文件写入、包管理器检查和安装都分别要求确认，并在正文显示复杂脚本。核对本机会话的执行结果：Scoop 安装 Go 的 `duration_ms=278000`（4分38秒），之后运行命令约12843毫秒；额外目录查询约几百毫秒至1.75秒，WSL 探测约5.64秒。模型往返、人工确认与应用复核另占时间。因此总耗时不能全部算成 Hello World 的运行时间，也不能声称减少弹窗会消除软件下载时间。

## 实现范围

- 一次 `workspace_environment_inspect` 检查原生程序和版本；PATH 加有限 Go 常见位置，不遍历软件目录或全盘，不启动 WSL，不运行安装器。每程序最多两个候选，固定版本参数，每次最多4秒；取消会收拢同批查询，Go 查询设置 `GOTOOLCHAIN=local`。
- `TaskScope` 仅从本轮明确创建项目的直接请求绑定具体目录。空目录初始化、该范围内新增普通文件、明确要求的 Go 单文件或当前包运行/构建可免重复确认。提问、示例、否定与无法识别的表述不扩大默认范围。文件 Hook、恢复记录、变更检查、后台强制审批和撤销继续有效；已有文件覆盖、删除、其他项目、安装、任意脚本及执行注入参数仍确认。
- 简单 Bash Go/版本命令绑定实际程序直接执行；复杂脚本不通过“只读”标签整体放行。新增环境查询尊重 `workspace_run` 依赖策略。
- 安装正文说明动作和影响，例如“是否通过 Scoop 为当前账号安装 Go”；脚本、PATH 等完整值保留在详情。正在执行的操作以自然语言显示，即使同批其他操作先完成也保留安装活动状态。
- Go 运行/构建作为独立执行证据提供给复核，不冒充单元测试通过；简单程序已有运行结果时，收尾不再仅提示缺少单元测试。

## 回归

18个相关模块的回归命令：

```powershell
python -W error::ResourceWarning -m unittest tests.test_environment_workflow tests.test_approval_summary tests.test_bash_tools tests.test_command_environment tests.test_session_environments tests.test_command_policy tests.test_tool_policy tests.test_project_access tests.test_file_tools tests.test_file_recovery tests.test_file_transforms tests.test_change_review tests.test_orchestration tests.test_all_tool_concurrency tests.test_runtime tests.test_architecture tests.test_prompts tests.test_permissions -q
```

首轮发现60×20窗口的安装说明多出一行滚动，压缩正文后：`Ran 223 tests in 114.661s`，`OK`，日志 `.local/workflow-final.log`。布局同时检查80×24与60×20，正文无滚动，确认按钮可见，详情仍包含完整脚本。

随后补齐 Go 当前包 `.` 与独立执行证据，相关6模块追加76项检查，其余75项通过，新增测试因代码片段误放进另一个测试方法产生 NameError。修正测试分组后，针对新增执行证据与真实 Go 单文件、模块、Bash 运行补跑：`Ran 2 tests in 4.990s`，`OK`，日志 `.local/workflow-fixed-tests.log`。保留原失败日志 `.local/workflow-module-final.log`，不把该次76项执行写成全通过。后台强制确认、禁用、撤销、受保护内容、项目外输入与执行注入参数均有用例；未安装依赖或软件。

## 真实 DianStar

使用实际模型、`app.runtime.run_turn`、真实 Git MCP 和已安装 Go；在隔离的测试 Desktop 下创建新项目。运行时模型配置读取日常设置，客户端直接读取原凭据；凭据不复制到测试文件，Git 配置写入夹具专用目录。回调拒绝任何意外确认，以检测是否仍会弹窗；日常配置做哈希基线检查。

初次发现普通 `go run .`、`go build -o hello-go.exe .` 未纳入默认任务范围，出现两次多余确认；之后 `go run main.go` 已执行成功。该轮严格输出检查还误将常见的 `Hello, World!` 与 `Hello World` 判为不同。保留 `.local/go-workflow-live-20261101` 的原记录，修正包运行范围和等价输出验收后，在全新目录重试。

最终 `.local/go-workflow-live-20261101-accepted/verification.json`：

- `goal_verified=true`，`no_redundant_confirmations=true`，`approval_count=0`。
- 创建 Git 项目、一次环境查询、写 `main.go`/`go.mod`、实际 `go run .`、复核完成，输出 `Hello, World!`，退出码0。
- 总耗时19.906秒，Go 命令耗时2843毫秒；已有 Go、网络与模型响应状态不同会改变耗时，不承诺每次固定20秒。
- 日常配置哈希未改变，未触及用户桌面的原 hello-go 项目，未再次安装 Go。

本轮仍观察到模型在创建前重复尝试打开尚未创建的项目；工具没有因此写入或弹窗，后续转为正常创建。这是后续可继续压缩的规划冗余，不把本轮解释为模型永不绕路。安装提示的界面与分类已验收，本轮没有重新执行真实下载安装。

本机脚本 `.local/go-workflow-live.py`，最终历史、进度与报告位于 accepted 目录。测试状态与项目均留在 `.local`，不进入提交。更新后的 Python 代码须重启 DianStar 才会被已运行的应用加载；测试新建默认流程时选用新目录，已有项目覆盖仍会确认。
