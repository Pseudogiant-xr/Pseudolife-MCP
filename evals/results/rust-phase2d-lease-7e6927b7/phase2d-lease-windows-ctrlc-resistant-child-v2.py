"""Deterministic child survives CTRL_C_EVENT until its lease parent stops it."""
import json
import os
import pathlib
import signal
import sys
import time

signal.signal(signal.SIGINT, signal.SIG_IGN)
pathlib.Path(sys.argv[1]).write_text(json.dumps(dict(pid=os.getpid(), parent_pid=os.getppid()))+'\n', encoding='utf-8')
pathlib.Path(sys.argv[2]).write_bytes(os.environ.get('PSEUDOLIFE_LEASES_HELD', '').encode())
time.sleep(60)
