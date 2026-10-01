<#
.SYNOPSIS
    Registers (or updates) the scheduled task that keeps the read-only APEX dashboard running.
    Run from an elevated PowerShell:
        powershell -ExecutionPolicy Bypass -File scripts\install_apex_dashboard_task.ps1

.DESCRIPTION
    The dashboard (run_dashboard.py) is read-only and separate from the engine: it opens the
    DB with mode=ro, never takes the production lock and never starts or stops the loop.
    This task is independent of APEX-Top100-Production; installing, starting or stopping it
    does not touch the production loop.

    Triggers: at system startup (1 min delay) and every -RepeatMinutes. While it runs, ticks
    are ignored (MultipleInstances = IgnoreNew). A second copy could not start anyway, because
    the port is already bound, so it would exit at once.
    Principal: the current user, LogonType S4U - no stored password, no window.
    Binds to 127.0.0.1 only (run_dashboard.py default). No secrets in the task.

    Stop:    Stop-ScheduledTask -TaskName APEX-Dashboard   (the action is python itself)
    Remove:  Unregister-ScheduledTask -TaskName APEX-Dashboard

    -Disabled registers without enabling. -TaskName / -Port are for isolated testing.
#>
param(
    [string]$TaskName      = 'APEX-Dashboard',
    [string]$RepoDir       = 'C:\Users\JASSIM\ApexBot',
    [string]$Python        = 'C:\Python314\python.exe',
    [int]   $Port          = 8765,
    [int]   $RepeatMinutes = 5,
    [switch]$Disabled
)

$ErrorActionPreference = 'Stop'
foreach ($p in @($Python, (Join-Path $RepoDir 'run_dashboard.py'))) {
    if (-not (Test-Path -LiteralPath $p)) { throw "not found: $p" }
}

$action = New-ScheduledTaskAction -Execute $Python -Argument "-u run_dashboard.py --port $Port" -WorkingDirectory $RepoDir
$atBoot = New-ScheduledTaskTrigger -AtStartup
$atBoot.Delay = 'PT1M'
$every  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -DontStopOnIdleEnd
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited

$task = New-ScheduledTask -Action $action -Trigger @($atBoot, $every) -Settings $settings -Principal $principal `
    -Description "APEX read-only dashboard on http://127.0.0.1:$Port (mode=ro, GET only). Independent of APEX-Top100-Production."
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
if ($Disabled) { Disable-ScheduledTask -TaskName $TaskName | Out-Null }

$t = Get-ScheduledTask -TaskName $TaskName
'{0}: {1} as {2} (S4U); http://127.0.0.1:{3}; every {4} min + at startup' -f $t.TaskName, $t.State, $user, $Port, $RepeatMinutes
