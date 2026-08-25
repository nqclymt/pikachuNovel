param(
    [string]$Executable = "",
    [int]$Port = 18765
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Executable) {
    $Executable = Join-Path $ProjectRoot "release\PikachuNovel.exe"
}
$Executable = (Resolve-Path -LiteralPath $Executable).Path
$ValidationRoot = Join-Path $ProjectRoot "build\exe-validation"
$Profile = Join-Path $ValidationRoot "profile"
$Workspaces = Join-Path $ValidationRoot "workspaces"
New-Item -ItemType Directory -Path $Profile, $Workspaces -Force | Out-Null

$PreviousUserProfile = $env:USERPROFILE
try {
    $env:USERPROFILE = $Profile
    $Process = Start-Process -FilePath $Executable -ArgumentList @(
        "--cli", "desktop", "--port", "$Port", "--workspace-root", $Workspaces
    ) -PassThru
}
finally {
    $env:USERPROFILE = $PreviousUserProfile
}

try {
    $Deadline = (Get-Date).AddSeconds(45)
    $Health = $null
    while ((Get-Date) -lt $Deadline) {
        if ($Process.HasExited) {
            throw "Desktop EXE exited early with code $($Process.ExitCode)"
        }
        try {
            $Health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
            break
        }
        catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $Health) {
        throw "Desktop health endpoint did not become ready."
    }

    $Index = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/" -TimeoutSec 5
    $Asset = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/assets/app.js" -TimeoutSec 5
    $Update = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/update-check" -TimeoutSec 8
    if (-not $Update.current_version -or -not $Update.latest_version) {
        throw "Frozen update-check endpoint returned incomplete version metadata."
    }
    $WorkspaceName = "exe-validation-$($Process.Id)"
    $TaskBody = @{
        type = "workspace_init"
        workspace = $WorkspaceName
        args = @{}
    } | ConvertTo-Json
    $Task = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/api/tasks" `
        -ContentType "application/json" -Body $TaskBody -TimeoutSec 5
    $TaskDeadline = (Get-Date).AddSeconds(30)
    while ($Task.status -in @("queued", "running", "stopping") -and (Get-Date) -lt $TaskDeadline) {
        Start-Sleep -Milliseconds 200
        $Task = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/tasks/$($Task.id)" -TimeoutSec 5
    }
    if ($Task.status -ne "succeeded") {
        throw "Frozen background CLI task ended with status '$($Task.status)': $($Task.message)"
    }
    $TaskLogs = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/tasks/$($Task.id)/logs" -TimeoutSec 5
    if ($TaskLogs.content.Contains([char]0xfffd)) {
        throw "Frozen background CLI log contains invalid UTF-8 replacement characters."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Workspaces $WorkspaceName))) {
        throw "Frozen background CLI task did not create its workspace."
    }

    Start-Sleep -Milliseconds 500
    $Process.Refresh()
    if (-not $Process.MainWindowHandle) {
        throw "Desktop process has no visible main window."
    }

    [pscustomobject]@{
        Health = $Health.status
        IndexStatus = $Index.StatusCode
        IndexBytes = $Index.RawContentLength
        AssetStatus = $Asset.StatusCode
        AssetBytes = $Asset.RawContentLength
        UpdateCurrentVersion = $Update.current_version
        UpdateLatestVersion = $Update.latest_version
        TaskStatus = $Task.status
        TaskLogBytes = $TaskLogs.content.Length
        MainWindow = $true
        ProcessId = $Process.Id
    }
}
finally {
    if (-not $Process.HasExited) {
        if ($Process.CloseMainWindow()) {
            $Process.WaitForExit(10000) | Out-Null
        }
    }
    if (-not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force
        $Process.WaitForExit()
    }
    # PyInstaller one-file mode can leave its extracted child alive after the
    # bootloader process exits. Match this validation invocation exactly.
    Get-CimInstance Win32_Process -Filter "Name = 'PikachuNovel.exe'" |
        Where-Object {
            $_.ExecutablePath -eq $Executable -and
            $_.CommandLine -like "*--port $Port*" -and
            $_.CommandLine -like "*--workspace-root $Workspaces*"
        } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Wait-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
        }
}
