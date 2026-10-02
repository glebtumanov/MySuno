# MySuno bootstrap (idempotent, no admin rights needed).
# Usage:  powershell -ExecutionPolicy Bypass -File setup.ps1 [-SkipModels]
param([switch]$SkipModels)
$ErrorActionPreference = "Stop"
$Root   = $PSScriptRoot
$PyHome = Join-Path $Root "python"
$Ace    = Join-Path $Root "vendor\ACE-Step-1.5"
$VenvPy = Join-Path $Ace ".venv\Scripts\python.exe"

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
}

# 1. Signed CPython 3.12 from python.org.
#    NOT the uv-managed python-build-standalone: Windows Smart App Control blocks its unsigned binaries.
if (-not (Test-Path "$PyHome\python.exe")) {
    Write-Host "[1/5] Installing Python 3.12 into $PyHome ..."
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements --disable-interactivity `
        --override "/quiet InstallAllUsers=0 TargetDir=$PyHome Include_launcher=0 PrependPath=0 Include_test=0 Include_doc=0 Shortcuts=0 AssociateFiles=0"
    if (-not (Test-Path "$PyHome\python.exe")) { throw "Python install failed" }
} else { Write-Host "[1/5] Python already installed." }

# 2. uv
Refresh-Path
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "[2/5] Installing uv ..."
    winget install -e --id astral-sh.uv --accept-package-agreements --accept-source-agreements --disable-interactivity
    Refresh-Path
} else { Write-Host "[2/5] uv already installed." }

# Keep uv cache inside the project (the Claude desktop app virtualizes AppData writes).
$env:UV_CACHE_DIR = Join-Path $Root ".uv\cache"
$env:UV_LINK_MODE = "copy"
$env:UV_PYTHON_DOWNLOADS = "never"

# 3. ACE-Step 1.5 sources (zip from GitHub; git is not required)
if (-not (Test-Path "$Ace\pyproject.toml")) {
    Write-Host "[3/5] Downloading ACE-Step 1.5 ..."
    New-Item -ItemType Directory -Force (Join-Path $Root "vendor") | Out-Null
    $zip = Join-Path $Root "vendor\ace-step.zip"
    Invoke-WebRequest -Uri "https://github.com/ACE-Step/ACE-Step-1.5/archive/refs/heads/main.zip" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath (Join-Path $Root "vendor") -Force
    Rename-Item (Join-Path $Root "vendor\ACE-Step-1.5-main") "ACE-Step-1.5"
    Remove-Item $zip
} else { Write-Host "[3/5] ACE-Step sources already present." }

# 4. Dependencies (torch 2.7.1+cu128 etc. as pinned upstream) + our extras
Write-Host "[4/5] Installing Python dependencies (first run downloads ~8 GB) ..."
Push-Location $Ace
try {
    uv sync --python "$PyHome\python.exe"
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed" }
} finally { Pop-Location }
uv pip install --python $VenvPy -r (Join-Path $Root "requirements-extra.txt")
if ($LASTEXITCODE -ne 0) { throw "extra dependencies failed" }
# Audiobox Aesthetics (оценка качества для режимов выбора лучшего); --no-deps: torch/torchaudio уже стоят из lock-файла ACE
uv pip install --python $VenvPy --no-deps audiobox_aesthetics
if ($LASTEXITCODE -ne 0) { throw "audiobox_aesthetics failed" }

# 5. Model weights
if (-not $SkipModels) {
    Write-Host "[5/5] Downloading model weights (~10-15 GB, resumable) ..."
    Push-Location $Root
    try {
        $env:PYTHONIOENCODING = "utf-8"
        & $VenvPy -m mysuno.download
        if ($LASTEXITCODE -ne 0) { throw "model download failed (is Hugging Face reachable? try a VPN)" }
    } finally { Pop-Location }
} else { Write-Host "[5/5] Skipped model download." }

Write-Host "`nDone. Start the app with run.bat"
