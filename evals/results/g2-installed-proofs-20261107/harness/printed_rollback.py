"""Extract the rollback commands an engine update printed, verbatim."""
import re
import sys

lines = open(sys.argv[1], encoding="utf-8", errors="replace").read().splitlines()
start = max(i for i, line in enumerate(lines) if "rollback commands:" in line or line.strip() == "to roll back:")
for line in lines[start + 1:]:
    cmd = re.sub(r"^\s*(==>\s*)?", "", line)
    if cmd.startswith(("unset PSEUDOLIFE", "docker compose ", "if [ -n")):
        print(cmd)
