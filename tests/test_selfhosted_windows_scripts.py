"""The Windows parity runner's setup script and job-started hook
(ops/selfhosted/setup-windows-runner.ps1, windows-job-started.ps1)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parent.parent / "ops" / "selfhosted"
SETUP = OPS / "setup-windows-runner.ps1"
HOOK = OPS / "windows-job-started.ps1"
PWSH = shutil.which("pwsh")
needs_pwsh = pytest.mark.skipif(PWSH is None, reason="PowerShell 7 (pwsh) is not installed")


def pwsh(command: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, timeout=120, **kwargs)


@needs_pwsh
@pytest.mark.parametrize("script", [SETUP, HOOK], ids=lambda path: path.name)
def test_the_script_parses(script):
    result = pwsh("$errors = $null; [System.Management.Automation.Language.Parser]::ParseFile("
                  f"'{script}', [ref]$null, [ref]$errors) | Out-Null; $errors | ForEach-Object {{ $_.ToString() }}")
    assert result.returncode == 0 and result.stdout.strip() == "", result.stdout + result.stderr


@needs_pwsh
def test_the_egress_rule_names_every_range():
    # In PowerShell ',' binds tighter than '+': unparenthesised, the split
    # private ranges joined into one string that the firewall refused.
    text = SETUP.read_text(encoding="utf-8")
    block = re.search(r"^\$blocked = @\(.*?\)\n", text, re.S | re.M).group(0)
    result = pwsh(block + "$blocked.Count; $blocked -join ' '")
    count, ranges = result.stdout.strip().splitlines()
    assert int(count) == 9, result.stdout
    # Split like the script's own list: the identifier guards flag private
    # addresses. All of IPv6, as on Linux (the firewall takes it as two
    # halves, not ::/0): a LAN's global prefix is no private range.
    assert ranges.split() == ["10.0" ".0.0/8", "172.16" ".0.0/12", "192.168" ".0.0/16", "169.254.0.0/16",
                              "100.64.0.0/10", "224.0.0.0/4", "255.255.255.255", "::/1", "8000::/1"]


@pytest.mark.skipif(os.name != "nt" or PWSH is None, reason="the hook's egress check reads Windows routes")
def test_the_hook_refuses_a_job_for_an_account_the_firewall_does_not_bind(tmp_path):
    # This test's own account has no egress rule, so it can reach (or at
    # least is not refused by the firewall at) the LAN gateway: the hook
    # must fail the job before any cleanup runs.
    env = dict(os.environ, USERPROFILE=str(tmp_path), HOME=str(tmp_path))
    env.pop("PSEUDOLIFE_PARITY_RUNNER_DIR", None)
    result = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(HOOK)],
                            capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "so its firewall rule does not bind; the job is refused" in result.stdout


@needs_pwsh
def test_the_runner_password_survives_cmd():
    # config.cmd is a batch file: cmd.exe strips ^ and expands % and !, so a
    # password holding one registers the service with a different password.
    text = SETUP.read_text(encoding="utf-8")
    function = re.search(r"^function New-RunnerPassword \{.*?^\}\n", text, re.S | re.M).group(0)
    result = pwsh(function + "1..300 | ForEach-Object { New-RunnerPassword }")
    passwords = result.stdout.split()
    assert len(passwords) == 300 and len(set(passwords)) == 300, result.stderr
    assert all(re.fullmatch(r"[A-Za-z0-9]{40}", password) for password in passwords)


def test_the_runner_config_is_readable_by_the_account_after_its_rename():
    # A renamed file keeps its source directory's permissions: staged in an
    # administrators-only directory, .env and .path were unreadable to the
    # runner account (and so was the hook setting) until read was granted
    # before the rename. Registration writes them before config.cmd starts
    # the service.
    text = SETUP.read_text(encoding="utf-8")
    body = re.search(r"^function Write-RunnerConfig \{.*?^\}\n", text, re.S | re.M).group(0)
    assert body.index('Grant $staged @(\'/grant\', "*${sid}:(R)")') < body.index("[IO.File]::Move(")
    # The Windows runner reads .env but not .path, so PATH lives in .env.
    assert '"PATH=$path",' in body
    # That PATH replaces the service's own, so Git Bash must be on it, ahead
    # of System32's WSL bash.exe (Codex review of 3de68d9c).
    text_path = body[body.index("$path = @("):body.index("-join ';'")]
    assert text_path.index("Git\\bin") < text_path.index("System32")
    registration = text[text.index("Expand-Archive -LiteralPath $zip"):text.index("& .\config.cmd")]
    assert "Write-RunnerConfig" in registration


def test_the_runner_finds_python_in_a_seeded_tool_cache():
    # actions/setup-python's installer needs administrator rights the account
    # lacks (second trial dispatch, run 40748991561), so setup seeds the
    # cache it looks in, and the .env points the runner at that cache.
    text = SETUP.read_text(encoding="utf-8")
    body = re.search(r"^function Write-RunnerConfig \{.*?^\}\n", text, re.S | re.M).group(0)
    assert '"RUNNER_TOOL_CACHE=$toolcache",' in body
    assert '"AGENT_TOOLSDIRECTORY=$toolcache",' in body
    seeding = text[text.index("$toolcache = Join-Path $Root 'toolcache'"):text.index("& $uv tool install")]
    assert '$seed = Join-Path $toolcache "Python\\$full\\x64"' in seeding
    assert 'New-Item -ItemType File -Path "$seed.complete"' in seeding
    # Read and execute only: one job cannot change the interpreter the next uses.
    assert "Grant $toolcache @('/grant', \"*${sid}:(OI)(CI)(RX)\")" in seeding
    assert text.index("$toolcache = Join-Path") < text.index("function Write-RunnerConfig")


FILTER = OPS / "setup-vm-egress-filter.sh"
PROOF = OPS / "windows-egress-proof.ps1"


@needs_pwsh
def test_the_egress_proof_parses():
    result = pwsh("$errors = $null; [System.Management.Automation.Language.Parser]::ParseFile("
                  f"'{PROOF}', [ref]$null, [ref]$errors) | Out-Null; $errors | ForEach-Object {{ $_.ToString() }}")
    assert result.returncode == 0 and result.stdout.strip() == "", result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")
def test_the_hypervisor_filter_script_parses():
    # On stdin: a Windows path does not survive every bash's argument handling.
    # Bytes, so Windows' text mode adds no carriage returns.
    result = subprocess.run(["bash", "-n"], input=FILTER.read_bytes(), capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


def test_the_hypervisor_filter_touches_only_the_vms_taps_and_keeps_no_state():
    # Stateless and keyed to the VM's taps: a bridge-family ct match would
    # conntrack every guest's frames, and the Proxmox firewall would put every
    # guest through the host's iptables (2026-10-29). Prerouting, so frames
    # the bridge delivers to the hypervisor itself are filtered too (review
    # finding: a forward hook missed them).
    text = FILTER.read_text(encoding="utf-8")
    block = text[text.index('cat > "$file.new" <<NFT'):text.index("\nNFT\n")]
    ruleset = "\n".join(line for line in block.splitlines() if not line.strip().startswith("#"))
    assert "type filter hook prerouting priority -300; policy accept;" in ruleset
    assert 'iifname "tap${vmid}i*" jump from_vm' in ruleset and "hook forward" not in ruleset
    assert " ct " not in ruleset and "reject" not in ruleset
    # Only the VM's own MACs, and nothing but ARP, ND and plain IPv4 passes.
    assert "ether saddr != { $macs } counter drop" in ruleset
    assert "arp saddr ether != { $macs } counter drop" in ruleset
    assert ruleset.index("ether type ip6 counter drop") < ruleset.index("ether type != ip counter drop")
    # sshd replies only, never a SYN; DHCP to the broadcast address or the server.
    assert "tcp sport 22 tcp flags & (syn | ack) != syn accept" in ruleset
    assert "ip daddr { 255.255.255.255, $dhcp_server } udp sport 68 udp dport 67 accept" in ruleset
    assert "ip daddr { $private } counter drop" in ruleset
    ranges = re.search(r'^private="(.*)"$', text, re.M).group(1).replace('""', "")
    assert ranges.split(", ") == ["10.0" ".0.0/8", "172.16" ".0.0/12", "192.168" ".0.0/16", "169.254.0.0/16",
                                  "100.64.0.0/10"]
    # A stop never unfilters (the rollback is --remove), and the VM refuses
    # to start without its table.
    assert "ExecStop=" not in text.split("No ExecStop:")[0].split("[Service]")[-1]
    assert '! /usr/sbin/nft list table bridge $table' in text and 'qm set "$vmid" --hookscript' in text
