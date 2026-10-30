# 在 Windows 上生成可分发的便携包；只打包程序资源，不复制本机配置。
param([string]$Python = 'python')

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$buildRoot = Join-Path $projectRoot '.local\packaging'
$venvPython = Join-Path $buildRoot 'venv\Scripts\python.exe'
$buildId = Get-Date -Format 'yyyyMMdd-HHmmss'
$runRoot = Join-Path $buildRoot $buildId
$distRoot = Join-Path $runRoot 'dist'
$packageDir = Join-Path $distRoot 'DianStar'
$releaseDir = Join-Path $projectRoot 'release'
$zipPath = Join-Path $releaseDir "DianStar-Windows-$buildId.zip"
$githubComponent = Join-Path $projectRoot '.local\tools\github-mcp-server\v2.0.3\github-mcp-server.exe'
if (-not (Test-Path -LiteralPath $githubComponent) -or
    (Get-FileHash -LiteralPath $githubComponent -Algorithm SHA256).Hash -ne '647FE7E7F3C1296D1ACB936B5C7704BBCDB6BDA95B4533C524157446B33F0F16') {
    throw '打包前须准备并校验应用管理的 GitHub 连接组件 v2.0.3。'
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    & $Python -m venv (Join-Path $buildRoot 'venv')
    if ($LASTEXITCODE -ne 0) { throw '无法创建打包环境。' }
}

Push-Location $projectRoot
try {
    & $venvPython -m pip install --disable-pip-version-check -e . 'pyinstaller>=6,<7'
    if ($LASTEXITCODE -ne 0) { throw '安装打包依赖失败。' }

    & $venvPython -m PyInstaller `
        --noconfirm --clean --onedir --console --name DianStar `
        --collect-data dianstar `
        --hidden-import dianstar.tools.mcp.servers.git.server `
        --distpath $distRoot `
        --workpath (Join-Path $runRoot 'work') `
        --specpath (Join-Path $runRoot 'spec') `
        (Join-Path $projectRoot 'scripts\dianstar_entry.py')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller 构建失败。' }

    $componentTarget = Join-Path $packageDir '.local\tools\github-mcp-server\v2.0.3'
    New-Item -ItemType Directory -Force -Path $componentTarget | Out-Null
    Copy-Item -LiteralPath $githubComponent -Destination (Join-Path $componentTarget 'github-mcp-server.exe')
    Copy-Item -LiteralPath (Join-Path $projectRoot 'tests\fixtures\github-mcp-server.LICENSE') -Destination (Join-Path $componentTarget 'LICENSE')
    Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\DianStar.cmd') -Destination $packageDir
    Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\README-试用.txt') -Destination (Join-Path $packageDir 'README.txt')

    if (Test-Path -LiteralPath (Join-Path $packageDir '.env')) {
        throw '打包目录意外包含 .env；已停止分发。'
    }
    if (Test-Path -LiteralPath (Join-Path $packageDir '.local')) {
        throw '打包目录意外包含本地会话；已停止分发。'
    }

    New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory($packageDir, $zipPath)
    Write-Host "完成：$zipPath"
} finally {
    Pop-Location
}
