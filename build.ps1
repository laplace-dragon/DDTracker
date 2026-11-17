$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --clean ".\DD轨迹.spec"

Write-Host "Build complete: dist\DD轨迹.exe"

