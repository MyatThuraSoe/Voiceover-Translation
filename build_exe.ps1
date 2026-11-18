$ErrorActionPreference = 'Stop'

# Build the RecapUpgrade Windows desktop app.
#   powershell -ExecutionPolicy Bypass -File .\build_exe.ps1
#
# Output: dist\RecapUpgrade\RecapUpgrade.exe
#         dist\RecapUpgradeSetup.exe    (Windows installer, per-user)
#         dist\RecapUpgrade-win64.zip   (portable folder)

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

$Python = 'py'
$PythonArgs = @('-3.12')
$Dist = Join-Path $Root 'dist\RecapUpgrade'
$Exe = Join-Path $Dist 'RecapUpgrade.exe'
$Setup = Join-Path $Root 'dist\RecapUpgradeSetup.exe'

Write-Host '== RecapUpgrade build ==' -ForegroundColor Cyan
& $Python @PythonArgs -c "import sys; print('Python', sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) { throw 'py -3.12 was not found.' }

& $Python @PythonArgs -c "import PyInstaller, tkinterdnd2; print('PyInstaller', PyInstaller.__version__)"
if ($LASTEXITCODE -ne 0) {
    throw 'Build tools missing. Run: py -3.12 -m pip install -r requirements-build.txt'
}

Write-Host '-- running PyInstaller --' -ForegroundColor Cyan
& $Python @PythonArgs -m PyInstaller --noconfirm --clean (Join-Path $Root 'RecapUpgrade.spec')
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
if (-not (Test-Path -LiteralPath $Exe)) { throw "Missing build output: $Exe" }

Write-Host '-- copying runtime files next to the exe --' -ForegroundColor Cyan
foreach ($dir in @('ffmpeg', 'recap_runtime', 'services')) {
    $source = Join-Path $Root $dir
    if (-not (Test-Path -LiteralPath $source)) { throw "Missing required folder: $dir" }
    Copy-Item -LiteralPath $source -Destination $Dist -Recurse -Force
}

# Drop bytecode caches from the copied runtime folders; they are build leftovers.
foreach ($dir in @('recap_runtime', 'services')) {
    Get-ChildItem -LiteralPath (Join-Path $Dist $dir) -Directory -Recurse -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

# Program resources only. Personal config.json / presets.json / .env are never shipped:
# the app creates them under %LOCALAPPDATA%\RecapUpgrade on first run.
$runtimeFiles = @(
    'factory_presets.json',
    '.env.example',
    'LICENSE'
)
foreach ($file in $runtimeFiles) {
    $source = Join-Path $Root $file
    if (-not (Test-Path -LiteralPath $source)) { throw "Missing required file: $file" }
    Copy-Item -LiteralPath $source -Destination $Dist -Force
}
Copy-Item -LiteralPath (Join-Path $Root 'assets\RecapUpgrade.png') -Destination $Dist -Force

# Never leave user data from an earlier local build inside the folder we ship.
foreach ($file in @('config.json', 'presets.json', '.env')) {
    Remove-Item -LiteralPath (Join-Path $Dist $file) -Force -ErrorAction SilentlyContinue
}
foreach ($dir in @('jobs', 'logs', 'models', 'userdata')) {
    $path = Join-Path $Dist $dir
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force }
}

$readme = @'
RecapUpgrade 2.2.0 - Windows desktop app
======================================

Start:  RecapUpgrade.exe

Output modes
  Myanmar . translate   any supported source language -> Myanmar narration
  English . rewrite     English source -> fresh English wording, same meaning

  English narrators are Edge voices (default en-GB-RyanNeural); Myanmar uses
  my-MM-ThihaNeural / my-MM-NilarNeural.

User data
  Settings, presets, jobs, logs and downloaded models are stored outside the
  program folder, so upgrades never touch them:

      %LOCALAPPDATA%\RecapUpgrade\
          config.json     app settings (created on first save)
          presets.json    your saved presets
          .env            GEMINI_API_KEY=... for Gemini translation/TTS
          jobs\           job output and run logs
          logs\           app logs and font cache
          models\         optional local TTS models (MMS)

  Open it from the app log ("User data: ...") or paste the path above.

  The .env is seeded from .env.example on first run. Edge TTS needs no key;
  a Gemini key is required only for Gemini translation and Gemini TTS.

Portable mode
  Create an empty file named "portable.flag" next to RecapUpgrade.exe to keep
  user data in a "userdata" folder beside the exe instead of %LOCALAPPDATA%.
  Use this only when the app folder is writable. Alternatively set the
  RECAP_DATA_DIR environment variable to any writable folder.

Program files (read-only)
  ffmpeg\               bundled ffmpeg.exe + ffprobe.exe
  recap_runtime\        translation / Whisper runtime used by the planner
  services\             Chromium text renderer (Myanmar + Latin)
  factory_presets.json  built-in presets
  .env.example          template for the optional Gemini key
  LICENSE               MIT license

Notes
  * First run downloads the Whisper model into the user cache; keep internet on.
  * Subtitle/title rendering uses Playwright with installed Chrome or Edge.
  * Custom Image Overlays have per-image Start/End times (MM:SS, H:MM:SS or
    seconds; blank End = whole video). "Use preview" fills the current time.
  * Uninstalling keeps %LOCALAPPDATA%\RecapUpgrade so your settings survive.
'@
Set-Content -LiteralPath (Join-Path $Dist 'README.txt') -Value $readme -Encoding UTF8

# Verify against a throw-away data folder so the build does not touch real user data.
$VerifyData = Join-Path $Root 'build\verify-data'
if (Test-Path -LiteralPath $VerifyData) { Remove-Item -LiteralPath $VerifyData -Recurse -Force }
$previousDataDir = $env:RECAP_DATA_DIR
$env:RECAP_DATA_DIR = $VerifyData

try {
    Write-Host '-- self-test --' -ForegroundColor Cyan
    & $Exe --self-test
    if ($LASTEXITCODE -ne 0) { throw 'Self-test failed. Do not ship this build.' }

    Write-Host '-- smoke-test --' -ForegroundColor Cyan
    & $Exe --smoke-test
    if ($LASTEXITCODE -ne 0) { throw 'GUI smoke-test failed.' }

    Write-Host '-- render-test (Chromium + Myanmar/English text) --' -ForegroundColor Cyan
    & $Exe --render-test
    if ($LASTEXITCODE -ne 0) { throw 'Chromium render-test failed.' }
}
finally {
    $env:RECAP_DATA_DIR = $previousDataDir
    if (Test-Path -LiteralPath $VerifyData) {
        Remove-Item -LiteralPath $VerifyData -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
Write-Host '-- building Windows installer (Inno Setup) --' -ForegroundColor Cyan
$Iscc = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'
if (-not (Test-Path -LiteralPath $Iscc)) {
    $Iscc = $null
    foreach ($candidate in @(
        'C:\Program Files (x86)\Inno Setup 6\ISCC.exe',
        'C:\Program Files\Inno Setup 6\ISCC.exe'
    )) {
        if (Test-Path -LiteralPath $candidate) { $Iscc = $candidate; break }
    }
}
if (-not $Iscc) {
    $fromPath = Get-Command 'iscc' -ErrorAction SilentlyContinue
    if ($fromPath) { $Iscc = $fromPath.Source }
}
if (-not $Iscc) {
    throw 'Inno Setup 6 not found. Install it with: winget install --id JRSoftware.InnoSetup -e'
}

if (Test-Path -LiteralPath $Setup) { Remove-Item -LiteralPath $Setup -Force }
& $Iscc (Join-Path $Root 'installer\RecapUpgrade.iss')
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed.' }
if (-not (Test-Path -LiteralPath $Setup)) { throw "Missing installer output: $Setup" }

# ---------------------------------------------------------------------------
# Portable zip: the only artifact that gets portable.flag, so extracting it is
# self-contained while the installer keeps using %LOCALAPPDATA%.
$flag = Join-Path $Dist 'portable.flag'
New-Item -ItemType File -Path $flag -Force | Out-Null
try {
    $zip = Join-Path $Root 'dist\RecapUpgrade-win64.zip'
    if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
    Write-Host '-- creating portable zip --' -ForegroundColor Cyan
    Compress-Archive -LiteralPath $Dist -DestinationPath $zip -CompressionLevel Optimal
}
finally {
    Remove-Item -LiteralPath $flag -Force -ErrorAction SilentlyContinue
}

# Publish checksums next to the artifacts so downloads can be verified.
$checksums = Join-Path $Root 'dist\SHA256SUMS.txt'
$lines = foreach ($file in @($Setup, $zip)) {
    $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $file
    "{0}  {1}" -f $hash.Hash.ToLower(), (Split-Path $file -Leaf)
}
Set-Content -LiteralPath $checksums -Value $lines -Encoding ASCII

Write-Host ""
Write-Host 'BUILD OK' -ForegroundColor Green
Write-Host ("EXE:      " + $Exe)
Write-Host ("SETUP:    " + $Setup)
Write-Host ("PORTABLE: " + $zip)
Write-Host ("SUMS:     " + $checksums)
exit 0
