### Fixed (2026-11-27 — the plugin hooks accept a CRLF credential file and send only id-shaped session fields)
- The bash hooks read a credential file saved with a Windows line end
  (Notepad, `Set-Content`) as its token instead of reporting the file
  invalid; a carriage return or newline anywhere else in the token still
  refuses it.
- `session-start.sh`, `session-end.sh` and `coordination-start.sh` put the
  session id and start source into requests only when they use the character
  set `coordination-prompt.sh` already required.
- Under Git Bash, `session-start.sh` and `coordination-prompt.sh` find the
  native launcher where the Windows installers put it (`%LOCALAPPDATA%`), so
  update notices name it by its drive path (forward slashes, which run as
  typed in Git Bash and PowerShell) instead of a bare command.
- `lifecycle.ps1` refuses a credential or Codex connection file below a
  symlink or junction unless the native client's `private-path-check` trusts
  it, as the bash hooks already did.
