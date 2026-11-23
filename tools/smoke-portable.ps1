param([Parameter(Mandatory)][string]$ZipPath)

$ErrorActionPreference = 'Stop'
$zip = (Resolve-Path -LiteralPath $ZipPath).Path
$build = Join-Path (Split-Path -Parent $PSScriptRoot) 'build'
New-Item -ItemType Directory -Path $build -Force | Out-Null
$build = [System.IO.Path]::GetFullPath($build)
$extract = [System.IO.Path]::GetFullPath((Join-Path $build "portable smoke $([guid]::NewGuid())"))
if (-not $extract.StartsWith($build + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The extraction folder must be inside build.'
}
New-Item -ItemType Directory -Path $extract | Out-Null
$report = Join-Path $extract 'result.txt'
$environment = @{}
$keys = @('PATH', 'PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'QT_PLUGIN_PATH',
    'QT_QPA_PLATFORM_PLUGIN_PATH', 'QT_QPA_PLATFORM', 'QT_CONF_PATH')
try {
    Expand-Archive -LiteralPath $zip -DestinationPath $extract
    foreach ($key in $keys) {
        $environment[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, $null, 'Process')
    }
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $exe = Join-Path $extract 'PaperPdfViewer\PaperPdfViewer.exe'
    $launchArgs = @('--smoke-test', ('"{0}"' -f $report))
    $process = Start-Process -FilePath $exe -ArgumentList $launchArgs -WorkingDirectory $extract -WindowStyle Hidden -PassThru
    if (-not $process.WaitForExit(45000)) {
        $process.Kill()
        $process.WaitForExit()
        throw 'Portable application did not exit within 45 seconds.'
    }
    $result = if (Test-Path -LiteralPath $report) { Get-Content -LiteralPath $report -Encoding UTF8 -Raw }
    if ($process.ExitCode -ne 0 -or $result -notmatch '^PASS Paper PDF Viewer') {
        throw "Portable smoke test failed (exit $($process.ExitCode)): $result"
    }
    Write-Host $result.Trim()
} finally {
    foreach ($key in $environment.Keys) {
        [Environment]::SetEnvironmentVariable($key, $environment[$key], 'Process')
    }
    Remove-Item -LiteralPath $extract -Recurse -Force
}
