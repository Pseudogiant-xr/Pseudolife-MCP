"""Private hidden-console launch reused by small and owned-board cells."""
import base64
import hashlib
import json
import pathlib
import os
import subprocess
import sys
import time

P = pathlib.Path(__file__).parent

def capture(label, command, commands, source, home, evidence, url, pre_files, no_board=False):
    from evals.rust_port import cli_process
    cli_process.reset_home(home)
    env = cli_process.fixture_env(home, commands, url)
    env['PSEUDOLIFE_LEASES_HELD'] = 'outer'
    for name, data in pre_files.items():
        path = cli_process.file_path(home, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data))
    ready = evidence/(label+'-ready.json')
    config = evidence/(label+'-private-config.json')
    out = evidence/(label+'-console.json')
    log = evidence/(label+'-console.log')
    argv = [*command, 'lease', 'run', 'ctrlc-proof']
    if no_board:
        argv.append('--no-board')
    argv += ['--', sys._base_executable, '-B', str(P/'phase2d-lease-windows-ctrlc-resistant-child-v2.py'), str(ready), str(home/'ctrlc-held.txt')]
    config.write_text(json.dumps(dict(argv=argv, cwd=str(source), environment=env, ready=str(ready), board_probe=not no_board)), encoding='utf-8')
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    launched = time.time()
    helper = None
    helper_env = dict(os.environ)
    helper_env['PYTHONPATH'] = str(pathlib.Path(sys.prefix)/'Lib/site-packages')
    try:
        with log.open('xb') as output:
            helper = subprocess.Popen([sys._base_executable, '-B', str(P/'phase2d-lease-windows-ctrlc-console-helper-v3.py'), '--config', str(config), '--out', str(out)], cwd=source,
                                      stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                      env=helper_env, startupinfo=startup, creationflags=subprocess.CREATE_NEW_CONSOLE)
            exit_code = helper.wait(timeout=65)
        observed = json.loads(out.read_text(encoding='utf-8'))
        observed.update(helper_exit_code=exit_code, helper_launch_time_unix=launched,
                        first_output_within_two_minutes=log.stat().st_size > 0 and time.time()-launched < 120,
                        argv=argv, cwd=str(source), console_log_sha256=hashlib.sha256(log.read_bytes()).hexdigest(),
                        post_files_b64=cli_process.snapshot(home),
                        ready_fixture_is_external_to_home=True)
        return observed
    finally:
        config.unlink(missing_ok=True)
        if helper is not None and helper.poll() is None:
            # The console helper owns its target; this timeout is recorded as a
            # failed launch, never interpreted as target cleanup success.
            helper.kill()
            helper.wait(timeout=10)
