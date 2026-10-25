# Windows 便携版构建与试用

返回 [项目首页](../README.md)。本页面向维护者，源码安装见 [使用指南](./使用指南.md)。

维护者可在 Windows 上从项目根目录运行以下命令，生成无需安装 Python 的便携 ZIP：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_windows.ps1
```

脚本会在 `.local/packaging/` 创建独立打包环境，将 ZIP 放入 `release/`。将整个 ZIP 发给试用者；对方解压后双击 `DianStar.cmd` 或 `DianStar.exe`，再输入 `/config` 配置自己的 API Key。包内含程序代码、依赖、提示词和终端图案数据，不含制作者的 `.env`、会话或配置。便携版把试用者的 `.env` 和 `.local/` 写在解压目录，因此应放在可写的个人文件夹中。Windows 版须在 Windows 上构建，其他系统需要分别构建。打包后的提示词已嵌入程序，修改源码提示词需要重新打包。
