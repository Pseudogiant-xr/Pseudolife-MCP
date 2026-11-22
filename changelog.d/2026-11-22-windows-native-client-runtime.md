### Fixed (2026-11-22 — native client startup on clean Windows) [#1874]
- The Windows native client includes its C runtime and starts without the Microsoft Visual C++ Redistributable. The build checks the DLL imports of the packaged executable.
- The native installers report the downloaded client's exit code when it fails to start. On Windows, the installers and native updater explain a missing system library and direct users to a current release.
