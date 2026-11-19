# Build both Windows x64 formats from one application folder.
param(
    [string]$IsccPath,
    [switch]$PortableOnly
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Create .venv and install requirements-build.txt first.'
}

function Invoke-BuildStep([string]$Command, [string[]]$Arguments) {
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Command exited with status $LASTEXITCODE." }
}

$version = (& $python -c 'from paper_reader import VERSION; print(VERSION)').Trim()
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') {
    throw 'Could not read the application version.'
}
New-Item -ItemType Directory -Path 'build' -Force | Out-Null
$originalPath = $env:PATH
$originalCache = $env:PYINSTALLER_CONFIG_DIR
try {
    # DLL discovery must use the wheels, Python, and Windows system DLLs.
    # Other developer tools on PATH can ship incompatible DLLs with the same names.
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $env:PYINSTALLER_CONFIG_DIR = Join-Path $PSScriptRoot 'build\pyinstaller-cache'
    Invoke-BuildStep $python @(
        '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir', '--windowed',
        '--name', 'PaperPdfReader', '--specpath', 'build',
        '--collect-binaries', 'pymupdf',
        'run_reader.py'
    )
} finally {
    $env:PATH = $originalPath
    $env:PYINSTALLER_CONFIG_DIR = $originalCache
}
$appDirectory = Join-Path $PSScriptRoot 'dist\PaperPdfReader'
$runtime = Join-Path $appDirectory '_internal'
foreach ($dependency in @('python312.dll', 'PySide6\plugins\platforms\qwindows.dll')) {
    if (-not (Test-Path -LiteralPath (Join-Path $runtime $dependency))) {
        throw "Runtime file missing from the bundle: $dependency"
    }
}
Invoke-BuildStep $python @('tools\collect_licenses.py', $appDirectory)
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination $appDirectory

$release = Join-Path $PSScriptRoot 'release'
New-Item -ItemType Directory -Path $release -Force | Out-Null
$zip = Join-Path $release "PaperPdfReader-$version-portable.zip"
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $appDirectory, $zip, [System.IO.Compression.CompressionLevel]::Optimal, $true
)
& (Join-Path $PSScriptRoot 'tools\smoke-portable.ps1') -ZipPath $zip

if (-not $PortableOnly) {
    if (-not $IsccPath) {
        $isccCommand = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        $programFiles32 = [Environment]::GetEnvironmentVariable('ProgramFiles(x86)')
        $candidates = @(
            $isccCommand.Source,
            "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 7\ISCC.exe",
            "$programFiles32\Inno Setup 7\ISCC.exe"
        )
        $IsccPath = $candidates | Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } |
            Select-Object -First 1
    }
    if (-not $IsccPath) { throw 'Inno Setup 7 (ISCC.exe) was not found. Pass -IsccPath.' }
    Invoke-BuildStep $IsccPath @('-qp', "-dAppVersion=$version", 'installer\PaperPdfReader.iss')
}
Get-ChildItem -LiteralPath $release -Filter "PaperPdfReader-$version-*" | Select-Object Name, Length
