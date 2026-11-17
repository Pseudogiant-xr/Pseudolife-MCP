# A machine-wide test PostgreSQL turn. Dot-source for the harness functions,
# or prefix a foreground worker command with this script (PowerShell 7).
# Parse wrapper options only up to --. PowerShell's parameter binder would
# otherwise consume child options such as python -c as this script's flags.

function Read-DevLockRecord([IO.Stream] $Stream) {
    $Stream.Position = 0
    $reader = [IO.StreamReader]::new($Stream, [Text.Encoding]::UTF8, $true, 1024, $true)
    try { $text = $reader.ReadToEnd() } finally { $reader.Dispose() }
    if (-not $text.Trim()) { return $null }
    try {
        $record = ConvertFrom-Json -InputObject $text -ErrorAction Stop
        if (-not $record.holder -or [int]$record.pid -le 0 -or
            [string]$record.process_started -notmatch '^\d+$' -or
            [long]$record.process_started -le 0 -or -not $record.acquired) { throw 'missing identity' }
        $null = [DateTimeOffset]::Parse($record.acquired)
        return $record
    } catch { throw 'cannot read the database lock holder identity; inspect the lock before retrying' }
}

function Test-DevLockOwner($Record) {
    # Never os.kill(pid, 0): on Windows that sends a console control event.
    try { $process = [Diagnostics.Process]::GetProcessById([int]$Record.pid) }
    catch [ArgumentException] { return $false }
    try { return $process.StartTime.ToUniversalTime().Ticks.ToString() -eq [string]$Record.process_started }
    catch { throw 'cannot read the database lock holder process identity; retry once it can be verified' }
    finally { $process.Dispose() }
}

function Enter-DevDatabaseLock {
    param([string] $Path = '', [string] $Holder = 'database run',
          [ValidateRange(0, 86400)] [int] $WaitSeconds = 1200)
    if (-not $Path) { $Path = Join-Path $HOME '.pseudolife-mcp/locks/test-postgres.lock' }
    $Path = [IO.Path]::GetFullPath($Path)
    $null = [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Path))
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $nextNotice = 0.0
    while ($true) {
        $hold = $null
        $record = $null
        try {
            # Readers must themselves share ReadWrite; writers never share Write.
            # Never unlink this inode: queued callers must keep using the same lock.
            $hold = [IO.File]::Open($Path, 'OpenOrCreate', 'ReadWrite', 'Read')
            if (-not $IsWindows) { $hold.Lock(0, 1) }
        } catch [IO.IOException] {
            if ($hold) { $hold.Dispose(); $hold = $null }
        }
        if ($hold) {
            try {
                $record = Read-DevLockRecord $hold
                if (-not $record -or -not (Test-DevLockOwner $record)) {
                    $record = @{ holder=$Holder; pid=$PID
                        process_started=[Diagnostics.Process]::GetCurrentProcess().StartTime.ToUniversalTime().Ticks.ToString()
                        acquired=[DateTimeOffset]::UtcNow.ToString('o') }
                    $bytes = [Text.Encoding]::UTF8.GetBytes(($record | ConvertTo-Json -Compress))
                    $hold.Position = 0
                    $hold.SetLength(0)
                    $hold.Write($bytes, 0, $bytes.Length)
                    $hold.Flush($true)
                    return $hold
                }
            } catch { $hold.Dispose(); throw }
            $hold.Dispose()
        } else {
            # Metadata is observational only; it can never override the OS lock.
            try {
                $read = [IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
                try { $record = Read-DevLockRecord $read } finally { $read.Dispose() }
            } catch { throw "cannot read the database lock holder identity: $($_.Exception.Message)" }
        }
        $owner = if ($record) {
            $age = [math]::Max(0, [math]::Floor(([DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse($record.acquired)).TotalSeconds))
            "$($record.holder) (PID $($record.pid)), held for $age s"
        } else { 'a holder publishing its identity' }
        if ($watch.Elapsed.TotalSeconds -ge $WaitSeconds) { throw "database lock wait exhausted after $WaitSeconds s: $owner; wait for that run to release it" }
        if ($watch.Elapsed.TotalSeconds -ge $nextNotice) {
            [Console]::Error.WriteLine("database lock: waiting for $owner (limit $WaitSeconds s)")
            $nextNotice = $watch.Elapsed.TotalSeconds + 30
        }
        Start-Sleep -Milliseconds 200
    }
}

function Exit-DevDatabaseLock([IO.FileStream] $Hold) {
    if ($Hold) {
        try { $Hold.SetLength(0); $Hold.Flush($true) } finally { $Hold.Dispose() }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $ErrorActionPreference = 'Stop'
    $PSNativeCommandUseErrorActionPreference = $false
    $hold = $null
    $code = 3
    try {
        $Holder = 'worker database run'
        $WaitSeconds = 1200
        $LockPath = ''
        $index = 0
        while ($index -lt $args.Count) {
            $option = [string]$args[$index]
            if ($option -eq '--') { $index++; break }
            if ($option -notin @('-Holder', '-WaitSeconds', '-LockPath')) { break }
            $index++
            if ($index -ge $args.Count) { throw "missing value for $option" }
            switch ($option) {
                '-Holder' { $Holder = [string]$args[$index] }
                '-WaitSeconds' { $WaitSeconds = [int]$args[$index] }
                '-LockPath' { $LockPath = [string]$args[$index] }
            }
            $index++
        }
        $Command = @($args | Select-Object -Skip $index)
        if (-not $Command) { throw 'give a foreground database-using command after the lock options' }
        $hold = Enter-DevDatabaseLock -Path $LockPath -Holder $Holder -WaitSeconds $WaitSeconds
        $program, $arguments = $Command[0], @($Command | Select-Object -Skip 1)
        $global:LASTEXITCODE = 0
        & $program @arguments
        $code = if ($?) { $LASTEXITCODE } else { if ($LASTEXITCODE) { $LASTEXITCODE } else { 1 } }
    } catch { [Console]::Error.WriteLine("database lock: not run: $($_.Exception.Message)") }
    finally { Exit-DevDatabaseLock $hold }
    exit $code
}
