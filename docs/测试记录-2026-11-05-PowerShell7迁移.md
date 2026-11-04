# PowerShell 7 安装与 DianStar 迁移验收

完成日期：2026-11-05（任务于11月4日开始）。保留此前用户修改、评估脚本和本机IDE配置，验收阶段未提交或推送；后续发布以 Git 记录为准。

## 安装结果

已从[微软官方v7.6.9发布](https://github.com/PowerShell/PowerShell/releases/tag/v7.6.9)安装Windows x64稳定版，独立用户目录为 `%LOCALAPPDATA%/Programs/PowerShell/7/pwsh.exe`，本机实际为 `C:/Users/wang/AppData/Local/Programs/PowerShell/7/pwsh.exe`。已运行新程序确认7.6.9/Core，并回读用户PATH确认安装目录已注册；现有终端可能需重开。

官方ZIP：PowerShell-7.6.9-win-x64.zip，109620855字节。SHA256：ce7484804c0f0ad4dda10c958e71acdaf7eebfb22505c1b480f89e0e9f1da94d。完整包同时匹配官方Release API digest和hashes.sha256清单，pwsh.exe Authenticode签名为Valid。网络较慢时使用HTTP Range续传，最终完整SHA256通过后才安装；校验清单实际为UTF-16，按正确编码读取。旧宿主的安全模块加载冲突没有通过跳过签名检查解决，改用已通过官方哈希的新运行时及其配套模块核验签名。

Windows PowerShell5.1与7并存，未卸载系统组件。ZIP安装不自动更新，需要后续按官方发布更新。安装信息及原用户PATH备份留在本机.local/powershell7-install；本仓库不提交安装包、账号配置或其他应用的私有运行时。

## 项目修改

- `platform/shells.py` 从独立用户安装、Program Files、用户Scoop依次发现7，不从项目PATH或其他应用内部目录选择同名程序。缺失时给出powershell7_unavailable，不悄悄回到5.1。普通对话、文件工具和直接启动其他程序不以PowerShell为前提。
- `shell` 使用选定7。`workspace_run` 的裸pwsh/powershell名称以及系统旧powershell.exe的-Command请求也统一绑定到7；实际结果executable记录真正程序。这是迁移到7的路由，不是5.1兼容模式。非选定的其他PowerShell副本不自动执行。
- 公共启动器继续统一DSWork目录、NoProfile/NonInteractive、Unicode输出、退出码与超时收尾；未新增-File、交互、编码或任意运行时参数支持。
- 固定Windows隔离启动器按已选中的PowerShell身份提供原先日志/COM运行时能力，不由模型传入能力名单。LPAC、默认断网、SDK只读、受保护文件排除、Job资源与清理规则保持。
- 剪贴板和右键菜单复用7查找逻辑。WinForms菜单C#编译补充新.NET引用程序集，真实后台ready协议验证通过，没有显示菜单或修改用户剪贴板。
- Schema、system.md、README及工具/架构/安全/开发文档按7更新。README给出微软官方安装方式与推荐使用7的说明。

## 语言模式说明更正

对系统5.1和新7使用相同LPAC执行器实际查询LanguageMode并尝试定义无副作用类型，两者均返回FullLanguage且允许Add-Type。此前文档的“保持受限语言模式”不准确，本轮已更正当前说明；不能把ConstrainedLanguage视作现有安全保证。实际文件、网络及进程保护来自LPAC/权限、网关、Hook和Job规则。该更正不是迁移中降低语言模式，也没有设置全局执行策略或关闭隔离。

## 测试

新增真实工具版本用例先在旧代码失败（major=5/Desktop），迁移后通过。新增独立安装发现优先级、忽略PATH影子程序、不回退5.1、旧名及裸名路由到7、原生菜单后台启动用例。

```powershell
python -W error::ResourceWarning -m unittest tests.test_powershell_tools tests.test_host_safety tests.test_host_lifecycle tests.test_host_policy tests.test_command_policy tests.test_command_environment tests.test_session_environments tests.test_all_tool_concurrency tests.test_architecture tests.test_tool_policy tests.test_clipboard -q
```

**135项全部通过，118.991秒，无跳过。** 使用独立绝对DIANSTAR_FILE_STATE_ROOT和.local/test-tmp；日志.local/powershell7-regression.log。覆盖版本与发现、两个命令入口、路径与中文、退出/超时、系统/凭据及相邻目录保护、默认断网、子进程清理、ACL恢复、环境版本、授权撤销、并发与剪贴板/菜单协议。实际菜单初始化另有真实进程探测成功，UI常规交互用例仍有模拟部分。

```powershell
python scripts/evaluate_shell_matrix.py --output .local/powershell7-matrix-20261105
```

**90/90通过**，沿用原固定场景及判定，实际执行7.6.9并继续检查LPAC和输入文件不变；日志.local/powershell7-matrix.log，逐项结果位于同名目录。

```powershell
python -m unittest tests.test_prompts tests.test_shell_evaluation -q
```

**7项通过，2.197秒。** 日志.local/powershell7-prompt-grader.log。

## 真实模型补验

```powershell
python scripts/evaluate_shell.py --output .local/powershell7-live-20261105 --edges --ids edge-01 edge-13
```

通过现有配置的deepseek-flash及真实DeepSeek API执行，未模拟模型回答；项目范围固定在新夹具，不联网、不安装或发布Git。审批回调仅用于夹具，生产审批规则未更改。

| 题目 | 结果 | 秒 | 实际命令数 | 审批数 |
| --- | --- | ---: | ---: | ---: |
| edge-01 | 通过 | 25.84 | 1 | 1 |
| edge-13 | 通过 | 11.3 | 1 | 2 |

edge-01验证中文路径与文件内容；edge-13实际查询主版本/PSEdition，并使用System.IO.Path.GetRelativePath得到项目相对路径。实际答案为7、Core、sub/b.txt三行，按磁盘结果及命令执行证据判定。manifest、脱敏事件和结果保留在.local/powershell7-live-20261105，日志.local/powershell7-live.log。该补验不替代全部历史模型任务或长时间压力测试。

## 范围和后续

- 不再复用Codex内部的7.6.5；新安装7.6.9可独立使用。未修改终端默认profile，输入pwsh可启动7，powershell.exe系统命令仍为5.1，DianStar内部路由另有上述规则。
- 脚本文件启动(-File)、交互及其他运行时参数仍明确拒绝，不能宣称所有PowerShellCLI形式已验收。
- SDK位置兼容、审批过密、复核证据引用、构建产物事后检查、长期负载仍是原有后续事项。
- `--edges`新增迁移用edge-13，重现旧12类边界集请按旧manifest中的edge-01至edge-12选择；历史24次结果原样保留。
