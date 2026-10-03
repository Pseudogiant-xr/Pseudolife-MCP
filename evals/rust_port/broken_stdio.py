"""Owned forwarding candidate that deliberately corrupts one stdio response."""
import re
import subprocess
import sys
import threading


def main():
    control = sys.argv[1]
    if control not in {"identity", "wrong-protocol", "duplicate-key"}:
        raise ValueError("unknown broken stdio control")
    child = subprocess.Popen([sys.executable, "-m", "pseudolife_memory.cli"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    def input_pump():
        try:
            for line in sys.stdin.buffer:
                child.stdin.write(line)
                child.stdin.flush()
        finally:
            child.stdin.close()
    def errors():
        for line in iter(child.stderr.readline, b""):
            sys.stderr.buffer.write(line)
            sys.stderr.buffer.flush()
    readers = [threading.Thread(target=input_pump), threading.Thread(target=errors)]
    for reader in readers:
        reader.start()
    mutated = False
    for line in iter(child.stdout.readline, b""):
        if not mutated and control != "identity":
            if control == "duplicate-key":
                line = b'{"jsonrpc":"2.0",' + line[1:]
            else:
                line = re.sub(rb'"protocolVersion":"[^"\r\n]+"',
                              b'"protocolVersion":"synthetic-invalid"', line, count=1)
            mutated = True
        sys.stdout.buffer.write(line)
        sys.stdout.buffer.flush()
    result = child.wait(timeout=15)
    for reader in readers:
        reader.join(timeout=3)
        if reader.is_alive():
            raise RuntimeError("broken stdio forwarding reader survived")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
