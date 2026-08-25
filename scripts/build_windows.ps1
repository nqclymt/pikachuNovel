param(
    [switch]$Clean,
    [string]$PythonExecutable
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$SpecPath = Join-Path $ProjectRoot "packaging\HarnessNovel.spec"
$OutputPath = Join-Path $ProjectRoot "release"
$WorkPath = Join-Path $ProjectRoot "build\pyinstaller"

if (-not $PythonExecutable) {
    $PythonExecutable = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $PythonExecutable -or -not (Test-Path -LiteralPath $PythonExecutable)) {
    throw "Python was not found. Pass -PythonExecutable with an installed Python path."
}

if ($Clean) {
    if (Test-Path -LiteralPath $OutputPath) {
        Remove-Item -LiteralPath $OutputPath -Recurse -Force
    }
    if (Test-Path -LiteralPath $WorkPath) {
        Remove-Item -LiteralPath $WorkPath -Recurse -Force
    }
}

New-Item -ItemType Directory -Path $OutputPath -Force | Out-Null
& $PythonExecutable -m PyInstaller --noconfirm --distpath $OutputPath --workpath $WorkPath $SpecPath

$Executable = Join-Path $OutputPath "PikachuNovel.exe"
if (-not (Test-Path -LiteralPath $Executable)) {
    throw "PyInstaller did not create $Executable"
}

$Hash = Get-FileHash -LiteralPath $Executable -Algorithm SHA256
"$($Hash.Hash.ToLowerInvariant())  PikachuNovel.exe" |
    Set-Content -LiteralPath (Join-Path $OutputPath "PikachuNovel.exe.sha256") -Encoding ascii

Write-Host "Built: $Executable"
Write-Host "SHA256: $($Hash.Hash.ToLowerInvariant())"
