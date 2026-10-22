# Start DianStar in the current console and leave PowerShell open after exit.
param([switch]$Check)

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $projectRoot
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython) {
    $pythonPath = $venvPython
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        Write-Host 'Python was not found. Create .venv and install the project.' -ForegroundColor Red
        return
    }
    $pythonPath = $pythonCommand.Source
}

if ($Check) {
    Write-Host "Project: $projectRoot"
    Write-Host "Python: $pythonPath"
    & $pythonPath -c 'import textual, openai'
    if ($LASTEXITCODE -eq 0) { Write-Host 'Dependencies installed' }
    return
}

Write-Host "Python: $pythonPath"
& $pythonPath -m agent_try
if ($LASTEXITCODE -ne 0) {
    Write-Host "DianStar exited with code $LASTEXITCODE. See the error above." -ForegroundColor Red
}
