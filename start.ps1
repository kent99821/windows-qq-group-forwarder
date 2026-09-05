$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $projectDir

if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    throw "未找到 .venv。请先运行：py -3.12 -m venv .venv，然后安装 requirements.txt"
}

& ".\.venv\Scripts\python.exe" -m app.main web --config config.toml
