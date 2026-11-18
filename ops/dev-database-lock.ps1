# A machine-wide test PostgreSQL turn. Dot-source for the harness functions,
# or prefix a foreground worker command with this script (PowerShell 7).
# Parse wrapper options only up to --. PowerShell's parameter binder would
# otherwise consume child options such as python -c as this script's flags.

function Initialize-DevDatabaseProcessTree {
    if (-not $IsWindows) { return }
    if (-not ('DevDatabaseProcessTree' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class DevDatabaseProcessTree {
    [StructLayout(LayoutKind.Sequential)]
    struct BasicLimits {
        public long ProcessTime, JobTime;
        public uint Flags;
        public UIntPtr MinimumWorkingSet, MaximumWorkingSet;
        public uint ActiveProcesses;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct IoCounters {
        public ulong ReadOperations, WriteOperations, OtherOperations;
        public ulong ReadBytes, WriteBytes, OtherBytes;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct ExtendedLimits {
        public BasicLimits Basic;
        public IoCounters Io;
        public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, ExactSpelling=true, SetLastError=true)]
    static extern IntPtr CreateJobObjectW(IntPtr security, string name);
    [DllImport("kernel32.dll", SetLastError=true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    static extern bool SetInformationJobObject(IntPtr job, int kind, ref ExtendedLimits limits, uint size);
    [DllImport("kernel32.dll", SetLastError=true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError=true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    static extern bool QueryInformationJobObject(IntPtr job, int kind, IntPtr data, uint size, out uint written);
    [DllImport("kernel32.dll")]
    static extern uint GetCurrentProcessId();
    [DllImport("kernel32.dll")]
    static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    static extern bool CloseHandle(IntPtr handle);

    // Intentionally owned until process exit, not a finalizable SafeHandle:
    // closing this job while its owner runs would terminate the owner too.
    // The OS closes it on death and terminates the child tree.
    // Children do not inherit this handle.
    static IntPtr lifetimeJob;
    public static void Attach() {
        if (lifetimeJob != IntPtr.Zero) return;
        IntPtr job = CreateJobObjectW(IntPtr.Zero, null);
        if (job == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObject failed");
        var limits = new ExtendedLimits();
        limits.Basic.Flags = 0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        if (!SetInformationJobObject(job, 9, ref limits, (uint)Marshal.SizeOf<ExtendedLimits>())) {
            int error = Marshal.GetLastWin32Error();
            CloseHandle(job);
            throw new Win32Exception(error, "SetInformationJobObject failed");
        }
        if (!AssignProcessToJobObject(job, GetCurrentProcess())) {
            int error = Marshal.GetLastWin32Error();
            CloseHandle(job);
            throw new Win32Exception(error, "AssignProcessToJobObject failed");
        }
        lifetimeJob = job;
    }
    public static long[] RemainingChildren() {
        if (lifetimeJob == IntPtr.Zero) return new long[0];
        int capacity = 16;
        while (true) {
            int size = checked(8 + capacity * IntPtr.Size);
            IntPtr data = Marshal.AllocHGlobal(size);
            try {
                uint written;
                bool ok = QueryInformationJobObject(lifetimeJob, 3, data, (uint)size, out written);
                int error = ok ? 0 : Marshal.GetLastWin32Error();
                if (!ok && error != 234) throw new Win32Exception(error, "Cannot inspect owned run processes");
                int assigned = Marshal.ReadInt32(data, 0), count = Marshal.ReadInt32(data, 4);
                if (!ok || count < assigned) {
                    capacity = Math.Max(checked(capacity * 2), assigned);
                    continue;
                }
                var children = new System.Collections.Generic.List<long>();
                for (int i = 0; i < count; i++) {
                    long pid = Marshal.ReadIntPtr(data, 8 + i * IntPtr.Size).ToInt64();
                    if (pid != GetCurrentProcessId()) children.Add(pid);
                }
                return children.ToArray();
            } finally { Marshal.FreeHGlobal(data); }
        }
    }
}
'@
    }
    [DevDatabaseProcessTree]::Attach()
}

function Read-DevLockRecord([IO.Stream] $Stream) {
    $Stream.Position = 0
    $reader = [IO.StreamReader]::new($Stream, [Text.Encoding]::UTF8, $true, 1024, $true)
    try { $text = $reader.ReadToEnd() } finally { $reader.Dispose() }
    if (-not $text.Trim()) { return $null }
    try {
        $record = ConvertFrom-Json -InputObject $text -ErrorAction Stop
        # PowerShell converts ISO dates to DateTime. Format explicitly: implicit
        # conversion uses a different culture from DateTimeOffset.Parse on some
        # non-interactive Windows hosts (for example en-US versus en-AU).
        if ($record.acquired -is [DateTime]) { $record.acquired = $record.acquired.ToString('o') }
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
    try {
        if (-not $IsWindows -and $Record.process_start_ticks) {
            $stat = [IO.File]::ReadAllText("/proc/$($Record.pid)/stat")
            $parts = $stat.Substring($stat.LastIndexOf(')') + 2).Split(' ', [StringSplitOptions]::RemoveEmptyEntries)
            return $parts[19] -eq [string]$Record.process_start_ticks
        }
        return $process.StartTime.ToUniversalTime().Ticks.ToString() -eq [string]$Record.process_started
    }
    catch { throw 'cannot read the database lock holder process identity; retry once it can be verified' }
    finally { $process.Dispose() }
}

function Enter-DevDatabaseLock {
    param([string] $Path = '', [string] $Holder = 'database run',
          [ValidateRange(0, 86400)] [int] $WaitSeconds = 0)
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
                # The OS handle is the truth. No old record, including one
                # naming a still-live PID, may block a free handle.
                $record = @{ holder=$Holder; pid=$PID
                    process_started=[Diagnostics.Process]::GetCurrentProcess().StartTime.ToUniversalTime().Ticks.ToString()
                    acquired=[DateTimeOffset]::UtcNow.ToString('o') }
                if ($IsWindows) {
                    $record.owner = [Security.Principal.WindowsIdentity]::GetCurrent().Name
                } else {
                    $stat = [IO.File]::ReadAllText("/proc/$PID/stat")
                    $parts = $stat.Substring($stat.LastIndexOf(')') + 2).Split(' ', [StringSplitOptions]::RemoveEmptyEntries)
                    $record.process_start_ticks = $parts[19]
                    $turnUid = [regex]::Match([IO.File]::ReadAllText("/proc/$PID/status"), '(?m)^Uid:\s+(\d+)')
                    if (-not $turnUid.Success) { throw 'cannot verify the database turn owner' }
                    $record.owner = "uid:$($turnUid.Groups[1].Value)"
                }
                $bytes = [Text.Encoding]::UTF8.GetBytes(($record | ConvertTo-Json -Compress))
                $hold.Position = 0
                $hold.SetLength(0)
                $hold.Write($bytes, 0, $bytes.Length)
                $hold.Flush($true)
                return $hold
            } catch { $hold.Dispose(); throw }
        } else {
            # Metadata is observational only; it can never override the OS lock.
            try {
                $read = [IO.File]::Open($Path, 'Open', 'Read', 'ReadWrite')
                try { $record = Read-DevLockRecord $read } finally { $read.Dispose() }
                if ($record -and -not (Test-DevLockOwner $record)) { $record = $null }
            } catch { $record = $null }
        }
        $owner = if ($record) {
            $age = [math]::Max(0, [math]::Floor(([DateTimeOffset]::UtcNow - [DateTimeOffset]::Parse($record.acquired)).TotalSeconds))
            "$($record.holder) (PID $($record.pid)), held for $age s"
        } else {
            if ($IsWindows) { 'a live holder whose identity is unavailable; list it with Get-Process python,pwsh | Select-Object Id,ProcessName,StartTime' }
            else { 'a live holder whose identity is unavailable; list it with ps -eo pid,ppid,lstart,comm' }
        }
        if ($watch.Elapsed.TotalSeconds -ge $WaitSeconds) { throw "database lock wait exhausted after $WaitSeconds s: $owner; wait for that run to release it, or give an explicit -WaitSeconds bound" }
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
        $WaitSeconds = 0
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
        # Containment belongs to this owned foreground launcher, never to
        # a shell that only dot-sources or calls the lock functions.
        Initialize-DevDatabaseProcessTree
        $hold = Enter-DevDatabaseLock -Path $LockPath -Holder $Holder -WaitSeconds $WaitSeconds
        $program, $arguments = $Command[0], @($Command | Select-Object -Skip 1)
        $global:LASTEXITCODE = 0
        & $program @arguments
        $code = if ($?) { $LASTEXITCODE } else { if ($LASTEXITCODE) { $LASTEXITCODE } else { 1 } }
    } catch { [Console]::Error.WriteLine("database lock: not run: $($_.Exception.Message)") }
    finally { Exit-DevDatabaseLock $hold }
    exit $code
}
