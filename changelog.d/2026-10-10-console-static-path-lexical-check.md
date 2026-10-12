### Security (2026-10-10 — Console static files refuse escaping paths before touching the filesystem)

- The daemon's `/ui/` static route now decides from the path string alone
  whether a request stays inside the Console's static folder, and answers
  403 before any filesystem call when it does not. Responses are unchanged:
  those paths were already refused with 403. A path that climbs above the
  folder and comes back (`../static/index.html`) is refused as well, since
  resolving it would visit the outside directory on the way.
- Under Windows path rules the same check also refuses a component that
  names a device (`NUL`, `CON`, `COM1.txt` and the rest, which Windows
  maps to devices in every directory) or an alternate data stream
  (`index.html:stream`); a same-drive `C:name` still serves as before.
- The resolved target, a directory's `index.html` and the root
  `index.html` used as the SPA fallback now share one containment check
  after the string check; each is still served only if it resolves inside
  the static folder, and refused with 403 otherwise.
- `tests/test_web.py` pins it: escaping `/ui/` paths (traversal, absolute,
  and on Windows UNC, pipe, `\\?\`, other-drive, device and stream forms)
  return 403 without any path being resolved (the test's resolver touches
  nothing, so a regression fails without opening the named path), and the
  containment check is exercised under both Windows and POSIX path rules,
  including each platform's forms under the other's rules, on every
  platform.
