| Input/output | Producer | Declared native substitution |
|---|---|---|
| `wait-mail-native-temporary-collisions`: Temporary-file collisions | Native atomic marker/listener writes. | Keep `.tmp-[a-z0-9_]{8}.seen`, exclusive creation, private permissions and cleanup of owned files. Sixteen collisions exhaust the native budget with `temporary file creation exhausted after 16 collisions`, without overwriting a colliding file/directory. The copied CPython platform retry count and exhaustion exception are removed. |
