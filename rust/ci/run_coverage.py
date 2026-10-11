"""Execute named H5 selections and retain their timing/count receipts."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from coverage_plan import EXCEPTIONS, PLATFORM_EXCLUSIONS, SOURCE_SHARDS, SUITES, UNOBSERVED_CONTROLS
from control_selection import select_controls

ROOT = Path(__file__).resolve().parents[2]


def source_environment(row, platform, home, environment):
    environment = dict(environment)
    if row == 'test_login' and platform == 'linux':
        # pg0 supplies libicu with PostgreSQL; initdb's child must find it
        # on hosts whose system ICU has a different major version.
        versions = sorted(p for p in (home / '.pg0/installation').iterdir() if (p / 'bin').is_dir())
        library = str(versions[-1] / 'lib')
        previous = environment.get('LD_LIBRARY_PATH')
        environment['LD_LIBRARY_PATH'] = library + (':' + previous if previous else '')
    return environment


def command(action, binaries, debug, output):
    suffix = '.exe' if os.name == 'nt' else ''
    prefix = [sys.executable]
    kind, mode = action['kind'], action['mode']
    if kind == 'cli':
        argv = prefix + ['rust/cli_harness', '--row', action['row'], '--candidate',
                         str(binaries / ('pseudolife-stdio' + suffix)), '-v']
        if mode != 'live':
            argv += ['--' + mode]
        if mode in ('record', 'golden'):
            argv += ['--golden-dir', str(output.parent / 'goldens')]
        for case in action.get('cases', []):
            argv += ['--case', case]
        for mutant in action.get('mutants', []):
            argv += ['--mutant', mutant]
    elif kind == 'graph':
        argv = prefix + ['rust/daemon/harness/graph_store.py', mode, '--row', action['row'],
                         '--candidate', str(binaries / ('graph-contract' + suffix))]
    else:
        scenarios = action.get('scenarios') or [action['scenario']]
        binary = debug if 'trust-bind' in scenarios else binaries
        argv = prefix + ['rust/daemon/harness/run.py', mode, '--fixture', '--rust-bin',
                         str(binary / ('pseudolife-daemon' + suffix)), '--only', *scenarios]
        if mode != 'mutants':
            argv += ['--golden-dir', str(output.parent / 'goldens')]
        if action.get('record'):
            argv += ['--record']
        if action.get('mutant'):
            argv += ['--mutants', action['mutant']]
            if action['mutant'] == 'health-omits-db':
                argv += ['--settle-with-search']
    return argv + ['--out', str(output)]


def main():
    parser = argparse.ArgumentParser()
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument('--suite', choices=SUITES)
    selector.add_argument('--source-shard', choices=SOURCE_SHARDS)
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--candidate-dir', type=Path, default=ROOT / 'rust/target/release')
    parser.add_argument('--debug-dir', type=Path, default=ROOT / 'rust/target/debug')
    parser.add_argument('--keep-going', action='store_true', help='measure all selections even after a failure')
    args = parser.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    os.environ['PL_HARNESS_SCRATCH'] = str(args.out_dir.resolve() / 'scratch')
    platform = 'windows' if os.name == 'nt' else 'linux'
    if args.source_shard:
        row, selected = SOURCE_SHARDS[args.source_shard]
        ids = select_controls(row, selected, platform)
        assert ids, 'no source mutants selected'
        actions = [dict(kind='cli', mode='mutants', row=row, mutants=ids)]
        selection = 'source-' + args.source_shard
        environment = source_environment(row, platform, Path.home(), os.environ)
    else:
        selection, actions = args.suite, SUITES[args.suite]
        environment = dict(os.environ)
    receipts = []
    for index, action in enumerate(actions):
        name = action.get('mutant') or action.get('scenario') or action['row']
        output = args.out_dir / f'{selection}-{index}-{name}-{action["mode"]}.json'
        argv = command(action, args.candidate_dir.resolve(), args.debug_dir.resolve(), output)
        print(f'{selection}: {name} {action["mode"]}', flush=True)
        started = time.monotonic()
        code = subprocess.run(argv, cwd=ROOT, env=environment).returncode
        receipt = dict(selection=action, seconds=time.monotonic() - started, exit=code,
                       output=output.name, status='fail' if code else 'pass')
        if not code and action['mode'] == 'golden' and action.get('scenario') in EXCEPTIONS['w1a_golden']:
            receipt.update(status='skip', skip_reason=EXCEPTIONS['w1a_golden'][action['scenario']])
        receipts.append(receipt)
        omitted = ({name: (EXCEPTIONS['cli_mutants'][platform].get(name)
                          or PLATFORM_EXCLUSIONS[platform][name])
                    for name in selected if name not in ids} if args.source_shard else {})
        (args.out_dir / f'{selection}-timings.json').write_text(
            json.dumps(dict(platform=platform, receipts=receipts, exceptions=EXCEPTIONS,
                            omitted_controls=omitted, unobserved_controls=UNOBSERVED_CONTROLS), indent=2),
            encoding='utf-8')
        if code and not args.keep_going:
            return code
    return int(any(receipt['exit'] for receipt in receipts))


if __name__ == '__main__':
    raise SystemExit(main())
