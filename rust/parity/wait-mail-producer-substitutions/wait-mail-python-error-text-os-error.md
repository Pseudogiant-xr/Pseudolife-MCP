| Input/output | Producer | Declared native substitution |
|---|---|---|
| `wait-mail-python-error-text`: OS error diagnostics | Native I/O errors at the CLI boundary. | The mappings in `wait_mail.rs::error_text` retain Python-shaped error text (including platform errno/WinError and quoted paths); they are deliberate text substitutions, not Python exceptions or tracebacks. |
