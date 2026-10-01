<#
.SYNOPSIS
    Read-only health check for the production Top100 loop.
        powershell -ExecutionPolicy Bypass -File scripts\check_apex_top100_health.ps1

.DESCRIPTION
    Liveness = the engine heartbeat (regime_history.ts, written once per cycle), not
    log or DB file times, which Windows may not refresh while a file is held open.

    Never writes to the DB: file times come from the filesystem, the heartbeat is read
    through a SQLite connection opened with mode=ro, and the lock file is read from
    byte 1 on (byte 0 is the locked byte).

    A "loop instance" is a process tree: cmd.exe -> py.exe -> python.exe for one loop
    counts once. Only loops whose command line names this DB are production loops.

    Exit codes: 0 HEALTHY, 1 UNHEALTHY, 2 CRITICAL (duplicate loops or auto_execute on).
#>
param(
    [string]$RepoDir      = 'C:\Users\JASSIM\ApexBot',
    [string]$Python       = 'C:\Python314\python.exe',
    [string]$DbPath       = 'C:\Users\JASSIM\ApexBot\apex_top100.db',
    [string]$LogDir       = 'C:\Users\JASSIM\ApexBot\logs',
    [string]$TaskName     = 'APEX-Top100-Production',
    [string]$LoopPattern  = 'run_top100.py',
    [int]   $MaxAgeMinutes = 35,   # scan interval is 15 min: two missed cycles + margin
    [string]$DashboardTaskName = 'APEX-Dashboard',
    [int]   $DashboardPort = 8765
)

$ErrorActionPreference = 'Stop'
$now = Get-Date
$problems = New-Object System.Collections.Generic.List[string]
$critical = $false

function Show([string]$k, $v) { '{0,-24} {1}' -f $k, $v }
function Age([datetime]$t) { [math]::Round(($now - $t).TotalMinutes, 1) }

# --- processes -------------------------------------------------------------
$all = @(Get-CimInstance Win32_Process)
$byId = @{}; foreach ($p in $all) { $byId[[int]$p.ProcessId] = $p }
$dbLeaf = [IO.Path]::GetFileName($DbPath)
$match = @($all | Where-Object {
    $_.ProcessId -ne $PID -and $_.Name -match '^(python|pythonw|py|pyw|cmd)\.exe$' -and
    $_.CommandLine -like "*$LoopPattern*" -and $_.CommandLine -like '*--loop*' -and
    $_.CommandLine -like "*$dbLeaf*" })
$matchIds = @{}; foreach ($m in $match) { $matchIds[[int]$m.ProcessId] = $true }
# root of a tree = a match whose parent is not itself a match
$roots = @($match | Where-Object { -not $matchIds.ContainsKey([int]$_.ParentProcessId) })
$pythons = @($match | Where-Object { $_.Name -match '^pythonw?\.exe$' })

Show 'production loop running' ($(if ($roots.Count -ge 1) { 'yes' } else { 'no' }))
Show 'independent instances' $roots.Count
foreach ($py in $pythons) {
    Show '  python PID' ('{0} started {1:yyyy-MM-dd HH:mm:ss}' -f $py.ProcessId, $py.CreationDate)
}
if ($roots.Count -eq 0) { $problems.Add('no production loop is running') }
if ($roots.Count -gt 1) { $problems.Add("CRITICAL: $($roots.Count) independent production loops"); $critical = $true }

# --- lock ------------------------------------------------------------------
$lockPath = [IO.Path]::GetFullPath($DbPath) + '.lock'
if (Test-Path -LiteralPath $lockPath) {
    try {
        $fs = [IO.File]::Open($lockPath, 'Open', 'Read', 'ReadWrite')
        try { $null = $fs.Seek(1, 'Begin'); $info = (New-Object IO.StreamReader($fs)).ReadToEnd().Trim() }
        finally { $fs.Dispose() }
    } catch { $info = "unreadable: $($_.Exception.Message)" }
    Show 'lock owner (last)' $info
} else { Show 'lock owner (last)' 'no lock file yet' }

# --- log -------------------------------------------------------------------
$log = Get-ChildItem -LiteralPath $LogDir -Filter 'top100-*.log' -ErrorAction SilentlyContinue |
       Where-Object { $_.Name -notlike '*.err.log' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($log) {
    # Informational only: while the loop keeps the log open, Windows can report the
    # time the file was opened instead of the last write (seen in supervisor testing).
    # Liveness is judged by the engine heartbeat in the DB below.
    Show 'latest log' ('{0} (modified {1} min ago; may lag while open)' -f $log.Name, (Age $log.LastWriteTime))
    $err = [IO.Path]::ChangeExtension($log.FullName, $null).TrimEnd('.') + '.err.log'
    if ((Test-Path -LiteralPath $err) -and (Get-Item -LiteralPath $err).Length -gt 0) {
        Show 'stderr log' ('{0} bytes in {1}' -f (Get-Item -LiteralPath $err).Length, [IO.Path]::GetFileName($err))
    }
} else { Show 'latest log' 'none'; $problems.Add("no top100-*.log in $LogDir") }

# --- DB (read-only) ----------------------------------------------------------
if (Test-Path -LiteralPath $DbPath) {
    $db = Get-Item -LiteralPath $DbPath
    Show 'DB modified' ('{0:yyyy-MM-dd HH:mm:ss} ({1} min ago)' -f $db.LastWriteTime, (Age $db.LastWriteTime))
    $env:PYTHONUTF8 = '1'
    $uri = 'file:' + ($db.FullName -replace '\\', '/') + '?mode=ro'
    $code = "import sqlite3,sys;c=sqlite3.connect(sys.argv[1],uri=True);r=c.execute('SELECT MAX(ts) FROM regime_history').fetchone();print(r[0] or '')"
    try {
        $ts = (& $Python -c $code $uri 2>$null | Select-Object -First 1)
        if ($ts) {
            $hb = [DateTimeOffset]::FromUnixTimeSeconds([long]$ts).LocalDateTime
            Show 'engine heartbeat' ('{0:yyyy-MM-dd HH:mm:ss} ({1} min ago, regime_history)' -f $hb, (Age $hb))
            if ((Age $hb) -gt $MaxAgeMinutes) { $problems.Add("engine heartbeat is $(Age $hb) min old") }
        } else { Show 'engine heartbeat' 'none recorded'; $problems.Add('no engine heartbeat in DB') }
    } catch { Show 'engine heartbeat' "unreadable: $($_.Exception.Message)"; $problems.Add('heartbeat unreadable') }
} else { Show 'DB' "missing: $DbPath"; $problems.Add('production DB missing') }

# --- scheduled task ----------------------------------------------------------
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    $ti = Get-ScheduledTaskInfo -TaskName $TaskName
    Show 'task' ('{0}: {1}' -f $TaskName, $task.State)
    # Hex as text: in Windows PowerShell 5.1 the literal 0x800710E0 is a negative Int32
    $hex = '{0:X}' -f $ti.LastTaskResult
    $meaning = switch ($hex) {
        '0'        { 'last run finished' }
        '41301'    { 'running now' }
        '41303'    { 'has not run yet' }
        '41325'    { 'queued' }
        '800710E0' { 'tick skipped: the loop was already running (IgnoreNew) - expected' }
        default    { 'see Task Scheduler history' }
    }
    Show 'task last run' ('{0} result=0x{1} ({2})' -f $ti.LastRunTime, $hex, $meaning)
    Show 'task next run' $ti.NextRunTime
    if ($task.State -eq 'Disabled') { $problems.Add('scheduled task is disabled') }
} else { Show 'task' "$TaskName not registered"; $problems.Add('scheduled task missing') }

# --- dashboard (informational; never affects the verdict) --------------------
$dashTask = Get-ScheduledTask -TaskName $DashboardTaskName -ErrorAction SilentlyContinue
$dashUp = [bool](Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $DashboardPort -State Listen -ErrorAction SilentlyContinue)
Show 'dashboard' ('{0} on 127.0.0.1:{1}; task {2}' -f $(if ($dashUp) { 'listening' } else { 'not listening' }), $DashboardPort,
                  $(if ($dashTask) { $dashTask.State } else { 'not registered' }))

# --- auto_execute (effective default used by run_top100.py) ------------------
try {
    Push-Location -LiteralPath $RepoDir
    $ae = (& $Python -c "from apex_top100.integration import ApexV2Bridge;print(ApexV2Bridge().auto_execute)" 2>$null | Select-Object -First 1)
} finally { Pop-Location }
Show 'auto_execute' $ae
if ($ae -ne 'False') { $problems.Add("CRITICAL: auto_execute is '$ae'"); $critical = $true }

# --- verdict -----------------------------------------------------------------
''
if ($critical)            { 'STATUS: CRITICAL'; $problems | ForEach-Object { " - $_" }; exit 2 }
if ($problems.Count -gt 0) { 'STATUS: UNHEALTHY'; $problems | ForEach-Object { " - $_" }; exit 1 }
'STATUS: HEALTHY'
exit 0
