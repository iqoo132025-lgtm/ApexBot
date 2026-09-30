<#
.SYNOPSIS
    Registers (or updates) the scheduled task that keeps the production Top100 loop alive.
    Run from an elevated PowerShell:
        powershell -ExecutionPolicy Bypass -File scripts\install_apex_top100_task.ps1

.DESCRIPTION
    Triggers:
      - At system startup: the loop runs after a reboot even before anyone logs on.
      - Every -RepeatMinutes, indefinitely: if the loop (and its wrapper) exited for any
        reason, the next tick starts it again. While it runs, ticks are ignored
        (MultipleInstances = IgnoreNew), so this never creates a second loop.
    Settings: restart on failure, no execution time limit, runs on battery, starts a
    missed run as soon as possible.
    Principal: the current user with LogonType S4U - runs whether or not the user is
    logged on, no password is stored, no window. Outbound HTTPS works; Windows file
    shares would not (none are used).

    The task stores no secrets. The loop reads optional Telegram settings from the
    user's environment exactly as an interactive run does.

    -Disabled registers the task without enabling it (used for a controlled handoff).
    -TaskName / -WrapperArgs exist for isolated supervisor testing only.
#>
param(
    [string]$TaskName      = 'APEX-Top100-Production',
    [string]$RepoDir       = 'C:\Users\JASSIM\ApexBot',
    [int]   $RepeatMinutes = 5,
    [string]$WrapperArgs   = '',
    [switch]$Disabled
)

$ErrorActionPreference = 'Stop'
$wrapper = Join-Path $RepoDir 'scripts\run_apex_top100_production.ps1'
if (-not (Test-Path -LiteralPath $wrapper)) { throw "wrapper not found: $wrapper" }

$psArgs = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$wrapper`" $WrapperArgs".Trim()
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $psArgs -WorkingDirectory $RepoDir

$atBoot = New-ScheduledTaskTrigger -AtStartup
$atBoot.Delay = 'PT1M'   # let networking come up first
$every  = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
            -RepetitionInterval (New-TimeSpan -Minutes $RepeatMinutes)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -DontStopOnIdleEnd -Priority 5

$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited

$task = New-ScheduledTask -Action $action -Trigger @($atBoot, $every) -Settings $settings `
    -Principal $principal -Description 'APEX Top100 production loop (paper / signals only). Single instance: the loop holds an OS lock on <db>.lock.'
Register-ScheduledTask -TaskName $TaskName -InputObject $task -Force | Out-Null
if ($Disabled) { Disable-ScheduledTask -TaskName $TaskName | Out-Null }

$t = Get-ScheduledTask -TaskName $TaskName
'{0}: {1} as {2} ({3}); every {4} min + at startup' -f $t.TaskName, $t.State, $user, 'S4U', $RepeatMinutes
