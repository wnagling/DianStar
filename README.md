# agentTry

agentTry 是一个学习和构建本地 Agent 的项目。长期方向见 [产品文档](./产品文档.md)。当前阶段只准备一个 **Python 命令行骨架**，目标是逐步完成对项目自身的只读分析。

## 当前状态

- 已有：Python 命令行入口，可查看帮助和接收问题。
- 未有：模型 API 调用、`list_files` / `read_file` 工具、工具调用循环、真实 API 验收。
- 暂不实现：文件修改、命令执行、记忆、多 Agent 调度和外部 Agent 接入。

此前的 Rust 只读原型保存在本地 Git 提交 `7729740`。它曾通过离线单元测试，但没有完成真实 DeepSeek API 验收。[2026-10-13 开发日志](./开发日志/2026-10-13.md)记录的是该历史阶段；当前语言决定见 [编程语言选型](./编程语言选型.md)。

## 运行骨架

需要 Python 3.10 或更新版本；当前骨架不需要安装第三方依赖。

```powershell
python -m agent_try --help
python -m agent_try "agentTry 目前实现了什么？"
```

第二条命令会明确提示分析功能尚未实现。

## 文件结构

```text
agent_try/__main__.py   命令行入口
agent_try/__init__.py   Python 包
pyproject.toml          项目元数据
产品文档.md              长期方向
编程语言选型.md          当前 Python 决定与历史 Rust 记录
开发日志/2026-10-13.md  历史开发日志
```

## 下一小步

先在 Python 中实现受项目根目录约束的只读文件工具，并验证路径和文件大小限制。然后接入 DeepSeek，完成工具调用循环。真实 API 运行时再检查工具调用、结论和文件行号；在此之前不宣称 Agent 已可用。
