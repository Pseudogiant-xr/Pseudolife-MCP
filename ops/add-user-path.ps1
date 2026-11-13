# Put one directory first on the Windows user PATH (HKCU\Environment only,
# never the machine PATH), keeping every other entry byte for byte: the raw
# value is read without expanding %VAR% entries and written back with its
# registry kind (REG_EXPAND_SZ stays REG_EXPAND_SZ). Prints "current" when
# the directory is already first, "changed" otherwise. ops/install.ps1 and
# ops/install.sh (under Git Bash) call it for the native client's launcher.
# Runs on Windows PowerShell 5.1 and PowerShell 7.
#
#   ops/add-user-path.ps1 -Dir <directory> [-KeyPath <HKCU subkey>]
#
# -KeyPath names another HKCU subkey holding a Path value (tests use a
# throwaway one); the settings-change broadcast happens only for the real
# Environment key.
param(
    [Parameter(Mandatory = $true)][string]$Dir,
    [string]$KeyPath = "Environment"
)
$ErrorActionPreference = "Stop"
$dir = $Dir.TrimEnd([char]92)
$key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($KeyPath)
try {
    $present = $key.GetValueNames() -contains "Path"
    $raw = if ($present) { [string]$key.GetValue("Path", "", [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) } else { "" }
    $kind = if ($present) { $key.GetValueKind("Path") } else { [Microsoft.Win32.RegistryValueKind]::ExpandString }
    if (($kind -ne [Microsoft.Win32.RegistryValueKind]::String) -and ($kind -ne [Microsoft.Win32.RegistryValueKind]::ExpandString)) {
        throw "the user Path value is $kind, not a string; it was left as it is"
    }
    $same = { param($entry) ([Environment]::ExpandEnvironmentVariables($entry).TrimEnd([char]92)) -ieq $dir }
    $entries = @($raw -split ";" | Where-Object { $_ })
    if (($entries.Count -gt 0) -and (& $same $entries[0])) {
        Write-Output "current"
        exit 0
    }
    $rest = @($entries | Where-Object { -not (& $same $_) })
    $key.SetValue("Path", ((@($dir) + $rest) -join ";"), $kind)
} finally {
    $key.Close()
}
if ($KeyPath -eq "Environment") {
    # New processes started by Explorer read the changed value only after
    # this broadcast (SetEnvironmentVariable sends the same message).
    if (-not ("PseudolifeUserPath.Native" -as [type])) {
        Add-Type -Namespace PseudolifeUserPath -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true, CharSet = System.Runtime.InteropServices.CharSet.Unicode)]
public static extern System.IntPtr SendMessageTimeout(System.IntPtr hWnd, uint Msg, System.UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out System.UIntPtr lpdwResult);
'@
    }
    $result = [System.UIntPtr]::Zero
    [void][PseudolifeUserPath.Native]::SendMessageTimeout([System.IntPtr]0xffff, 0x1A, [System.UIntPtr]::Zero, "Environment", 2, 5000, [ref]$result)
}
Write-Output "changed"
