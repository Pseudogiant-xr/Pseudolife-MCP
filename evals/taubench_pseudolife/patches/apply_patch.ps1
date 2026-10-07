# Apply the Pseudolife hooks to a taubench checkout. Run from the checkout root
# (the directory containing src\tau2). The checkout was unpacked from a tarball
# and has no .git, so GNU patch is the primary tool; it ships with Git for
# Windows at <git>\usr\bin\patch.exe.
$ErrorActionPreference = 'Stop'
$patchFile = Join-Path $PSScriptRoot 'tau2_pseudolife.patch'
$patchExe = (Get-Command patch -ErrorAction SilentlyContinue).Source
if (-not $patchExe) {
    $git = (Get-Command git -ErrorAction SilentlyContinue).Source
    if ($git) {
        $candidate = Join-Path (Split-Path (Split-Path $git)) 'usr\bin\patch.exe'
        if (Test-Path $candidate) { $patchExe = $candidate }
    }
}
if (-not $patchExe) { throw "GNU patch not found (install Git for Windows, or apply with: git apply -p1 $patchFile)" }
& $patchExe -p1 -i $patchFile
