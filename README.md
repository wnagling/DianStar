# agentTry

首个可运行版本是只读项目分析 CLI：向 DeepSeek Responses API 提问，模型可列出项目文件、读取 UTF-8 文件，再给出带文件行号的答案。当前不执行命令或修改项目文件。

## 运行

按 [Rust 官方安装说明](https://rust-lang.org/tools/install/)安装工具链，在 [DeepSeek 平台](https://platform.deepseek.com/api_keys)创建 API Key，然后在 PowerShell 中设置环境变量并运行：

```powershell
$env:DEEPSEEK_API_KEY = [System.Net.NetworkCredential]::new("", (Read-Host "DeepSeek API Key" -AsSecureString)).Password
cargo run -- --root . "agentTry 首版准备实现什么？哪些决定还没做？请注明依据的文件和行号。"
```

默认模型是 DeepSeek V4.1 Flash（`deepseek-flash`），默认 API 地址是 `https://api.deepseek.com`。可用 `DEEPSEEK_MODEL`、`DEEPSEEK_BASE_URL` 覆盖。DeepSeek 的 Responses API 不保存会话状态，CLI 会在每轮工具调用后回传完整对话历史。

如需继续使用 OpenAI，设置 `AGENTTRY_PROVIDER=openai`、`OPENAI_API_KEY`、`OPENAI_MODEL`；可选 `OPENAI_BASE_URL`，默认 `https://api.openai.com/v1`。

每次运行最多请求模型 8 轮、处理 24 次工具调用；单个文件最多读取 64 KiB，单次文件列表最多返回 200 项。隐藏文件和项目外文件不可读取。工具调用遵循 [DeepSeek Responses API 文档](https://api-docs.deepseek.com/guides/responses_api/)的 function calling 流程。

验收时检查：CLI 是否自主调用文件工具；结论是否与 `产品文档.md`、`编程语言选型.md` 的内容相符；引用的行号是否准确；工具失败时是否说明限制。
