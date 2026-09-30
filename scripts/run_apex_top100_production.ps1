<#
.SYNOPSIS
    Runs ONE production Top100 loop and returns its exit code. Started by the
    scheduled task APEX-Top100-Production (see install_apex_top100_task.ps1).

.DESCRIPTION
    - Runs from the repository with an explicit interpreter and an explicit DB path.
    - Forces UTF-8 for this process only (PR #13: Arabic logs must not crash the loop).
    - stdout -> logs\top100-<start>.log, stderr -> logs\top100-<start>.err.log.
      One pair per start; nothing is ever deleted (about 50 KB/day in normal running).
    - Supervisor events (start / exit code / skip) -> logs\supervisor.log.
    - Never passes --demo / --once / reset flags. Contains no secrets: Telegram
      settings, if any, come from the account's own environment variables.

    Duplicate protection is layered:
      1. Task Scheduler: MultipleInstances = IgnoreNew.
      2. This wrapper: a named mutex, so a manual run next to the task exits.
      3. run_top100.py --loop: an OS file lock on <db>.lock. This is the one that
         matters; it holds even if 1 and 2 are bypassed, and dies with the process.

    Exit codes: 0 = another instance already owns the loop (nothing to do),
    otherwise the loop's own exit code. The loop never returns on its own,
    so any exit is abnormal and non-zero lets Task Scheduler record a failure.

    -EntryScript / -DbPath / -LogDir / -MutexName exist only for isolated
    supervisor testing with a harmless stand-in process and temporary paths.
#>
param(
    [string]$RepoDir     = 'C:\Users\JASSIM\ApexBot',
    [string]$Python      = 'C:\Python314\python.exe',
    [string]$EntryScript = 'run_top100.py',
    [string]$DbPath      = 'C:\Users\JASSIM\ApexBot\apex_top100.db',
    [string]$LogDir      = 'C:\Users\JASSIM\ApexBot\logs',
    [string]$MutexName   = 'Global\APEX-Top100-Production'
)

$ErrorActionPreference = 'Stop'
$AlreadyRunningExit = 75   # apex_top100.instance_lock.ALREADY_RUNNING_EXIT

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$SupervisorLog = Join-Path $LogDir 'supervisor.log'

function Write-Supervisor([string]$Message) {
    $line = '{0} [wrapper pid={1}] {2}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'), $PID, $Message
    Add-Content -Path $SupervisorLog -Value $line -Encoding UTF8
}

$mutex = New-Object System.Threading.Mutex($false, $MutexName)
$owned = $false
try {
    try { $owned = $mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
    if (-not $owned) {
        Write-Supervisor 'another wrapper is already supervising; exiting'
        exit 0
    }

    foreach ($p in @($RepoDir, $Python, (Join-Path $RepoDir $EntryScript))) {
        if (-not (Test-Path -LiteralPath $p)) { Write-Supervisor "missing: $p"; exit 2 }
    }
    Set-Location -LiteralPath $RepoDir

    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8'

    $stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
    $outLog = Join-Path $LogDir "top100-$stamp.log"
    $errLog = Join-Path $LogDir "top100-$stamp.err.log"
    $argv   = @('-u', "`"$EntryScript`"", '--loop', '--db', "`"$DbPath`"")

    $proc = Start-Process -FilePath $Python -ArgumentList $argv -WorkingDirectory $RepoDir `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog -NoNewWindow -PassThru
    $null = $proc.Handle   # keeps ExitCode readable after exit (Windows PowerShell 5.1)
    Write-Supervisor ("started loop pid={0} db={1} log={2}" -f $proc.Id, $DbPath, $outLog)

    $proc.WaitForExit()
    $code = $proc.ExitCode

    if ($code -eq $AlreadyRunningExit) {
        Write-Supervisor "loop pid=$($proc.Id) found the DB lock held by another loop; exiting"
        exit 0
    }
    Write-Supervisor "loop pid=$($proc.Id) EXITED code=$code (abnormal: the loop never returns on its own)"
    if ($code -eq 0) { $code = 1 }
    exit $code
}
finally {
    if ($owned) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
