# 系统保护与 PowerShell 验收

本轮补齐文件工具和命令工具的系统保护，用户已明确接受 Windows 复杂命令采用受隔离的 PowerShell。没有提交或推送仓库，没有运行针对真实系统盘、驱动或启动文件的删除命令。

最终后端为 **LPAC**，不是普通 AppContainer。普通容器对宿主 `ALL APPLICATION PACKAGES` 可写目录的探针暴露了额外写权限，已通过 LPAC 创建属性排除，并加入真实拒绝验收。原生运行时使用 `registryRead`；只有固定系统 PowerShell 增加 `lpacInstrumentation`、`lpacCom` 初始化能力。模型不能指定这些能力，网络能力继续单独申请。LPAC 设置参考微软的 [创建示例](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)，能力SID通过 [DeriveCapabilitySidsFromName](https://learn.microsoft.com/en-us/windows/win32/api/securitybaseapi/nf-securitybaseapi-derivecapabilitysidsfromname) 生成；PowerShell所需日志能力也可在 [Chromium 的 LPAC 能力定义](https://chromium.googlesource.com/chromium/src/+/refs/tags/128.0.6613.171/sandbox/policy/win/lpac_capability.h) 找到。

## 实际行为

- `security/system_paths.py` 为文件、项目接入、Git 仓库/创建目标和回收范围提供统一禁区：磁盘根目录、系统/驱动/启动/安装目录、整个用户目录或桌面、凭据库和应用状态不能被登记成普通项目。仅应用持有活跃使用锁的 scratch 通过可信入口获得其自身目录范围。
- `hooks/commands.py` 调用 `security/host_commands.py`，在确认前及执行前拒绝明确的磁盘、驱动、启动、服务、提权、改权限、编码隐藏指令和 WSL/Docker 等宿主代理。共用内容检测器，不复制凭据正则。文字检查用于尽早说明拒绝原因，实际边界由 Windows 执行。
- `workspace_run` 和 `shell` 都强制 AppContainer。挂起创建、加入 Job、核对实际包身份和令牌之后才恢复；不继承管理员权限、启动描述、授权句柄或宿主敏感环境。默认不联网；`network=true` 按后台策略单独授权互联网能力，不扩大文件范围，也不添加本机回环豁免。
- 当前项目及本次专用临时目录可写，SDK只读。既有受保护路径不开放；普通配置及 Git 配置通过共享扫描器检查，发现凭据或超出检查限额时从访问白名单排除。Git 元数据通常只读，Git 修改仍使用专用工具。凭据检测有格式、资源和误报边界，不承诺识别所有未知秘密。
- 权限变更前先持久保存原 ACL、文件身份及预期状态。正常结束、失败、超时、取消恢复权限；进程崩溃后下次请求先恢复残留。外部权限变化不覆盖，保留记录并阻止无法核对的新命令。SDK只读能力在宿主内复用，退出时回收，异常退出记录可续办；不会把项目范围持久变成全宿主共享的 SDK 范围。
- PowerShell 使用子进程自己的 `DSWork:` 指向项目，`ProviderPath` 是真实目录；不修改祖先目录权限。CLIXML 只解析为有界文本，拒绝实体声明。命令默认120秒、最大600秒，输出最多2 MiB，进程树最多64进程、总内存4 GiB；取消、超时和前台退出清理子进程。
- 同项目命令、版本检测和文件操作共用项目锁。版本检测持独占锁并逐项完成探针，不同项目仍可并发。网络授权、旧 `bash` 禁用/强制审批策略和当前 `shell` 策略继续经过原网关。

## 前期 AppContainer 阶段回归

使用本机已有 Python 3.12、PowerShell、Git、Go；标准库 unittest，无依赖安装。临时文件和状态均指向项目 `.local/test-tmp` 与专用 `DIANSTAR_FILE_STATE_ROOT`，DPAPI、ACL、令牌和子进程用真实系统 API。测试时以普通用户运行，允许受控测试进程执行和可恢复的 SDK 只读权限调整；没有扩大系统、驱动或项目祖先目录的权限。

```powershell
python -W error::ResourceWarning -m unittest tests.test_host_safety tests.test_host_lifecycle tests.test_host_policy tests.test_all_tool_concurrency.ActualToolConcurrencyTests.test_real_commands_in_different_projects_overlap tests.test_environment_workflow -q
```

结果：**53项通过，81.783秒**。该轮 `test_host_policy` 有5项；之后增加1项工具框状态断言，已包含在下一轮。覆盖项目内正常写入、项目外创建/覆盖/重命名/删除/改权限拒绝、Everyone可写夹具仍被隔离、既有 `.env` 和普通凭据配置不可读、Git只读、SDK只读、子进程继承、默认网络拒绝、联网能力令牌差异、假启动错误标记、超时/取消/输出限额、ACL还原、实际进程退出后恢复和外部ACL变化冲突。根盘和驱动等危险样例仅预检，不实际执行。

```powershell
python -W error::ResourceWarning -m unittest tests.test_managed_storage tests.test_host_policy tests.test_command_policy tests.test_hook_registry tests.test_tool_policy tests.test_all_tool_concurrency -q
```

结果：**85项通过，44.227秒**。覆盖受管理临时目录真实脚本运行、原文件安全 Hook、审批/只读/禁用、旧名称策略、Git空白检查和跨项目真实命令并发。与53项有重叠，不将两轮数目相加为独立覆盖总数。scratch 用例补充了成功退出及实际输出断言，避免只检查“没有 error 字段”而漏掉程序失败。

中间回归出现过 Java/Go/Git 启动问题、旧并发夹具在项目外写同步文件，以及一次错误的测试模块名 `tests.test_git_workspace`。均未算作通过；本轮保留严格隔离，修复临时目录与启动器、调整夹具由宿主协调，并使用实际存在的 Git/命令测试模块复验。Java 的权限限制按下一节明确拒绝，没有伪称编译成功。

## 最终 LPAC 验收

```powershell
python -W error::ResourceWarning -m unittest tests.test_host_safety tests.test_host_lifecycle tests.test_host_policy tests.test_managed_storage tests.test_command_policy tests.test_hook_registry tests.test_tool_policy tests.test_all_tool_concurrency tests.test_environment_workflow tests.test_command_environment tests.test_session_environments tests.test_file_tools tests.test_file_recovery tests.test_file_transforms tests.test_change_review tests.test_tool_panel tests.test_architecture tests.test_project_access tests.test_git_engineering -q
```

本轮 **275项，397.089秒**：274项通过，1项网络测试夹具失败。LPAC在 `socket.socket()` 创建时已经拒绝网络，旧夹具只在 connect 阶段捕获拒绝，导致测试异常；未发生网络越界，也没有放宽权限。修正夹具为同时捕获创建和连接阶段，再运行下面专项，不重复执行其余已通过的同状态用例：

```powershell
python -W error::ResourceWarning -m unittest tests.test_host_safety tests.test_host_lifecycle -q
```

复验结果：**32项全部通过，57.708秒**。之后补充的冷启动 SDK 争用用例也单独通过（12.423秒）：首次工具链准备时，另一个项目等待专用权限锁，不误用10秒普通文件锁超时；权限准备和收尾锁最长等待60秒，实际命令仍用自己的执行超时。

另在最终LPAC下，实际尝试获取宿主测试进程的可复制令牌被拒绝，该新增用例单独通过（13.318秒）。UTF-16/CLIXML解码及审批文案的相关回归 `tests.test_host_policy tests.test_approval_summary` **20项通过，5.615秒**。以上轮次有重叠，不合计为独立用例总数；没有把修正前的275项称作整轮通过。

LPAC初始化中，直接查询用户态 class46 在本机不可用；最终不使用该查询或未文档化安全属性，使用受支持的创建属性并验证真实 AppContainer 身份和包SID，额外用公共应用包目录拒绝测试验证实际行为。PowerShell日志/COM能力仅给受信任的系统可执行文件，保持默认断网与文件边界。最终真实模型复验采用同一LPAC后端，结果见下节。

## 真实模型与构建验收

真实 `run_turn` 使用当前配置模型，在一次性 Git 测试项目完成创建 `main.py`、原生 Python 执行、PowerShell 查询真实项目目录以及复核记录。GitHub和Git写入关闭，模型没有安装依赖或写远程。最终LPAC轮 **9项工具事件全部成功**，3次具体动作确认由测试回调批准，**不代表真人已验收交互体验或所有步骤免确认**。此前普通容器轮有一次冗余 open_project 返回“项目已接入”，不计为成功；最终LPAC轮没有该调用。

- Python实际输出 `Hello isolated DianStar`，退出码0，命令执行313ms。
- PowerShell实际返回临时项目 `ProviderPath`，执行734ms；`Path` 显示 `DSWork:\`，两者在结果中区分。
- 初次环境检测13.141秒，主要包含 SDK只读权限建立。SDK权限在同一宿主内复用，但宿主退出后会清理；不声称每次启动均无成本。
- Go真实单文件、模块及直接绑定程序调用均成功输出 Hello World；Git实际 `diff --check` 可返回预期空白问题，没有通过 shim 启动失败冒充检查结果。
- 本机 `C:\JDK21` 由系统持有，普通用户无权建立必要的只读访问。现在返回 `command_runtime_inaccessible`，目标未执行；环境配置仍可查询/清除。**本轮未验收该JDK编译运行成功**，没有提权、改系统ACL或安装替代SDK。

## 安全方案取舍与待办

Git Bash 的 MSYS运行时在本机 AppContainer 中无法初始化。微软仓库有同类[兼容问题报告](https://github.com/microsoft/mxc/issues/1061)，与本机结果一致；经用户同意 Windows 不注册 Bash，提供 PowerShell。实现参考微软的 [AppContainer启动说明](https://learn.microsoft.com/en-us/windows/win32/secauthz/implementing-an-appcontainer)与 [Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)。

中途尝试给项目祖先目录添加临时读取权限的测试被自动审批拒绝，原因是范围扩大、清理可靠性未充分确认；该方案已撤回，最终使用进程内 PSDrive，不依赖这项权限扩大。另实测某些包/能力SID拒绝项不足以保护既有文件，最终改为敏感路径访问白名单排除，并以真实读取测试验收。

当前限制：

- Windows已有系统读权限及受控SDK读取仍受各自ACL、运行时兼容性约束；不能把“允许执行”说成所有编译器、包管理器都兼容。PowerShell受限语言模式，复杂原生参数优先 argv。
- 非Windows没有已验收的隔离后端，当前停止命令执行。只跳过旧Bash测试不构成其他平台验收。
- 项目隔离检查上限50000项，拒绝链接/junction/多重硬链接；普通配置扫描沿用单文件2 MiB、累计32 MiB上限，未检查配置不开放给命令。外部编辑器、同账号恶意程序及操作系统漏洞不属于DianStar锁的保证范围。
- LPAC仍遵守宿主对 `ALL RESTRICTED APPLICATION PACKAGES` 及命名能力的系统授权，SDK本身作为可信运行时提供只读范围；不声称拥有虚拟机或内核文件过滤器那样独立于宿主ACL的绝对目录边界。SDK细粒度访问及链接审计、更强宿主隔离应继续完善。
- 命令仍可能改坏项目内普通文件。超时、非零退出及未知执行状态不自动撤销已发生的项目修改；使用文件工具事务、trash或Git检查点处理相应恢复，不能把这些机制混为一体。
- 专用 `command-work` 与逐命令构建缓存尚未接入 tmp/trash 的30天维护；未来需要受控持久缓存、磁盘配额和命令级项目检查点。本轮没有提前清空这些目录或删除用户数据。

已同步工具说明、架构、安全机制、使用指南、开发测试说明、运行时提示词与 todo。未提交、未推送。
