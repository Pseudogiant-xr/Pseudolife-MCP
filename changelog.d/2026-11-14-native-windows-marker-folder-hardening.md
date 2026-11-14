### Fixed (2026-11-14 — Windows board wakes and private folders hold up in more layouts)

- On Windows the native client now also checks the folder a board marker is read from: an account that can delete entries in that folder could suppress a wake whatever the marker's own access list says, so such a folder is refused with one line naming it and the fix. The default digest folder in the user's profile passes.
- Folders the client creates for private state are owner-only from the moment they exist, so a folder another task creates inside one meanwhile (the handshake cache's) keeps its access. A handshake cache that cannot be saved now says so once, with the cause, instead of failing silently.
- The fix printed for a refused folder or marker names accounts by SID, works when pasted in cmd, PowerShell 5.1 and PowerShell 7 (a path a shell would expand gets a PowerShell form, and says so), and an access-list entry the client cannot read gets a fix that removes it.
