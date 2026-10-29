| Episode site | Producer/input class | Substitution or retained reason |
|---|---|---|
| `episode.rs:54/72/85/95` OS string conversion | OS paths / user string cwd | Keep host UTF-16/surrogateescape conversion because actual filenames can contain those units and change filesystem lookup and body bytes. |
