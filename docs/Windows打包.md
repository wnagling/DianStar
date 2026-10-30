# Windows 便携版构建与试用

返回 [项目首页](../README.md)。本页面向维护者，源码安装见 [使用指南](./使用指南.md)。

用户通过 `/github` 管理账号，浏览器登录和凭据保存使用 Git 安装附带的登录组件。该组件应随用户的 Git 安装提供，不复制构建者的登录。凭据使用系统凭据库，不随便携 ZIP 分发；本次没有重新构建便携包。

维护者可在 Windows 上从项目根目录运行以下命令，生成无需安装 Python 的便携 ZIP：

打包前须由维护流程准备 `.local/tools/github-mcp-server/v2.0.3/github-mcp-server.exe`。脚本校验固定 SHA256，再把组件和许可证一起放入便携包；不会复制维护者的 Token 或 MCP 配置。用户不需要安装或配置 MCP 服务。源码方式运行时也自动发现此组件或 PATH 中的安装；缺少组件时报告应用安装缺项。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1
```

脚本会在 `.local/packaging/` 创建独立打包环境，将 ZIP 放入 `release/`。将整个 ZIP 发给试用者；对方解压后双击 `DianStar.cmd` 或 `DianStar.exe`，再输入 `/config` 配置自己的 API Key。包内含程序代码、依赖、提示词和终端图案数据，不含制作者的 `.env`、会话或配置。便携版把试用者的 `.env` 和 `.local/` 写在解压目录，因此应放在可写的个人文件夹中。Windows 版须在 Windows 上构建，其他系统需要分别构建。打包后的提示词已嵌入程序，修改源码提示词需要重新打包。
