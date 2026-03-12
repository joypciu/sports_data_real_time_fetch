# schedule_daily.ps1
# Registers a Windows Task Scheduler task to run daily_ingest.py every day at 6:00 AM.
# Run this script once as Administrator to set up the task.
#
# Usage:
#   .\schedule_daily.ps1                  # Register with defaults (6:00 AM, all leagues)
#   .\schedule_daily.ps1 -Time "08:00AM"  # Register at a different time
#   .\schedule_daily.ps1 -Unregister      # Remove the task
#
# After registration you can verify with:
#   Get-ScheduledTask -TaskName "ESPN Daily Ingest"

param(
    [string]$Time = "06:00AM",
    [switch]$Unregister
)

$TaskName   = "ESPN Daily Ingest"
$ScriptDir  = "e:\realtime match fetch"
$ScriptPath = Join-Path $ScriptDir "daily_ingest.py"

# --- Discover Python executable ---
# Try conda base first, then fall back to whatever is on PATH.
$PythonExe = $null
$CandidatePaths = @(
    "$env:USERPROFILE\miniforge3\python.exe",
    "$env:USERPROFILE\miniconda3\python.exe",
    "$env:USERPROFILE\anaconda3\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
    "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe"
)
foreach ($c in $CandidatePaths) {
    if (Test-Path $c) { $PythonExe = $c; break }
}
if (-not $PythonExe) {
    $PythonExe = (Get-Command python -ErrorAction SilentlyContinue)?.Source
}
if (-not $PythonExe) {
    Write-Error "Could not locate python.exe. Set `$PythonExe manually and re-run."
    exit 1
}

Write-Host "Python   : $PythonExe"
Write-Host "Script   : $ScriptPath"
Write-Host "Task     : $TaskName"

# --- Unregister mode ---
if ($Unregister) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Task '$TaskName' removed." -ForegroundColor Yellow
    exit 0
}

# --- Build task components ---
$Action = New-ScheduledTaskAction `
    -Execute    $PythonExe `
    -Argument   "`"$ScriptPath`"" `
    -WorkingDirectory $ScriptDir

# Run daily at $Time; also catch up if the PC was off (RunIfMissed)
$Trigger = New-ScheduledTaskTrigger -Daily -At $Time

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew

$Principal = New-ScheduledTaskPrincipal `
    -UserId    $env:USERNAME `
    -LogonType Interactive `
    -RunLevel  Highest

# --- Register (overwrite if exists) ---
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Task already exists — updating..." -ForegroundColor Cyan
    Set-ScheduledTask -TaskName $TaskName `
        -Action   $Action `
        -Trigger  $Trigger `
        -Settings $Settings | Out-Null
} else {
    Register-ScheduledTask `
        -TaskName  $TaskName `
        -Action    $Action `
        -Trigger   $Trigger `
        -Settings  $Settings `
        -Principal $Principal `
        -Description "Fetches previous day's ESPN scores and appends finished games (with player stats) to the data/ JSON files." | Out-Null
}

Write-Host ""
Write-Host "Task '$TaskName' registered successfully." -ForegroundColor Green
Write-Host "Runs daily at $Time using: $PythonExe"
Write-Host ""
Write-Host "To run it immediately for testing:"
Write-Host "  Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "To verify:"
Write-Host "  Get-ScheduledTask -TaskName '$TaskName' | Select-Object TaskName, State"
