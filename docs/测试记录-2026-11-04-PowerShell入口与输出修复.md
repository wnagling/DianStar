# PowerShell 入口与输出修复验收

日期：2026-11-04。修复对象来自 [Shell扩展覆盖](./测试记录-2026-11-03-Shell扩展覆盖.md) 的两个可复现缺口。本轮未提交、未推送。

## 实际修改

- 新增 `platform/powershell.py`，供 `shell` 与 `workspace_run` 的系统PowerShell -Command/-c 共用同一启动器。统一固定系统程序、NoProfile/NonInteractive、DSWork项目盘、Unicode CLIXML输出及结果解码。
- 通用入口先按原环境定位程序，再识别真实系统PowerShell及允许的参数，生成可审批、执行前可重新比较的实际启动计划；原来的Hook、工作区锁、环境版本检查、网关审批与LPAC没有绕开。
- 使用 `Out-String -Stream` 逐行传递脚本输出，避免脚本显式exit时丢失先前内容；保留真实非零退出码，不通过将失败改为成功来通过测试。
- 进程执行器在超时、输出超限及收尾时继续有界收集输出管道剩余字节，不再将其直接丢弃。原有输出总量、进程树清理、内存及时间限制保持。
- `powershell_arguments_unsupported` 显示为未执行；未知参数不进入审批和进程启动。Python、Go、Java等直接argv执行路径保持原逻辑。
- 同步工具Schema、system.md、README、架构与工具文档，说明固定系统Windows PowerShell的兼容语法和实际支持范围。

## 支持边界

支持固定系统PowerShell，-Command/-c 后一个完整脚本文本，可带 -NoLogo、-NoProfile、-NonInteractive、-OutputFormat Text/XML，内部统一使用Unicode传输并返回普通文本。其他运行时、未知或交互参数、stdin脚本及编码启动明确拒绝；不静默改变其含义。

额外实测 `.ps1` -File 形式遇到隔离中的 AuthorizationManager 授权检查失败。本轮没有关闭执行策略、修改ACL或通过解释脚本正文绕过它；文件启动当前在通用入口明确拒绝，不能声称已支持所有PowerShell CLI形式。原失败记录保留于 .local/powershell-file-green.log。

显式exit退出前的正常输出现在保留。强制超时只能回收已经交给操作系统管道的输出：PowerShell序列化器或其他程序尚未刷新的内部缓冲仍可能丢失，不能承诺完整恢复。小输出在PowerShell被强制终止前仍缓冲的探测失败也保留于 .local/powershell-timeout-output.log；最终超时用例用超过运行时缓冲容量的数据验证已到管道内容的保留，留出3秒正常启动时间，不将启动尚未完成误判为输出丢失。

## 先复现，再修复

通过公开ToolGateway新增实际隔离测试：

1. `Write-Output 'before-failure'; exit 7` 原来返回退出码7但content为空，测试先失败，逐行传递后通过。
2. `workspace_run` 启动PowerShell查询目录、列举中文文件并读取内容，原来落到错误目录且中文错误输出乱码，测试先失败，统一启动后通过。
3. 继续验证原生Python失败输出与退出码、超时管道输出、邻接目录读取拒绝、联网审批、审批期间撤销和未知/编码/文件启动拒绝。脚本文件启动不计作执行通过，而验证其在审批前明确拒绝。

未模拟命令执行或隔离结果。固定执行场景使用新建临时目录；审批回调只处理这些夹具，没有授予用户真实目录额外权限。

## 90项矩阵

使用原脚本、原题目与原评分条件：

```powershell
python scripts/evaluate_shell_matrix.py --output .local/shell-matrix-fixed-final-20261104
```

结果：**90/90通过**。修复前同矩阵49/90；专用shell从44/45变为45/45，通用入口启动PowerShell从5/45变为45/45。覆盖8类路径、字面特殊文件名、原生程序实际工作目录、6种编码/空输入、错误流、显式退出、管道及20000行输入。每项继续检查LPAC和输入文件未改动。

先在统一启动器上跑过一轮90/90；之后进程管道收尾也发生修改，因此在最终执行代码上再次跑完整矩阵，而非沿用修改前的结果。原始日志为.local/shell-matrix-fixed-final-20261104.log，完整逐项数据和夹具保留在同名目录。

## 124项相关回归

```powershell
python -W error::ResourceWarning -m unittest tests.test_powershell_tools tests.test_host_safety tests.test_host_lifecycle tests.test_host_policy tests.test_command_policy tests.test_command_environment tests.test_session_environments tests.test_all_tool_concurrency tests.test_architecture tests.test_tool_policy -q
```

结果：**124项全部通过，107.569秒，无跳过**。测试包含新的9条PowerShell工具用例，以及系统路径与凭据保护、默认断网、子进程/超时/取消、SDK只读、ACL恢复、输出限额、环境版本、审批撤销、不同项目并发和架构依赖边界。日志.local/powershell-full-regression.log；测试状态使用独立绝对DIANSTAR_FILE_STATE_ROOT及仓库内.local/test-tmp，不使用日常恢复记录。

提示词说明更新后另执行 `python -m unittest tests.test_prompts -q`，2项通过（2.039秒）。首次未设置独立状态根的受限进程遇到默认状态库访问拒绝，已改用独立测试状态重跑；没有修改日常状态库权限或关闭保护。日志.local/powershell-prompt-regression.log。

## 真实模型补验

```powershell
python scripts/evaluate_shell.py --output .local/shell-fixed-live-20261104 --clarified --ids task-01 task-02 task-07 task-21
```

真实使用当前配置的deepseek-flash及DeepSeek API，无模拟回答；按实际答案文件与输入保持评分，**4/4完成目标**。目录范围固定在新建夹具，不联网、不安装、不发布Git。审批由测试回调处理，耗时不含用户阅读点击。

| 任务 | 秒 | 命令次数 | 审批次数 |
| --- | ---: | ---: | ---: |
| task-01 | 23.97 | 1 | 2 |
| task-02 | 25.89 | 5 | 6 |
| task-07 | 28.11 | 2 | 4 |
| task-21 | 62.95 | 6 | 7 |

模型仍出现系统PowerShell不具备的System.IO.Path.GetRelativePath调用、无效复核引用或文件定位失败，然后才完成任务。它们不属于本轮两个执行缺口的修复效果，不宣称模型效率已提高。原始manifest、事件、结果与夹具保留在.local/shell-fixed-live-20261104，日志为同名.log。

本轮没有重新执行全部模型题目、安装编译器或进行长期压力测试。审批过密、证据引用、SDK发现和构建产物事后审计仍需后续专项。
