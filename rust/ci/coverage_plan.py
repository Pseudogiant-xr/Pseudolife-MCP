"""Named selections for the H5 row/control CI shards (no product changes)."""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE_MUTANTS = json.loads((HERE / 'source_mutants.json').read_text(encoding="utf-8"))
# Linux 2026-10-11: the slowest eight-control selection took 81s;
# the first cold compile added 65s. Keep that bound on each parallel job.
SOURCE_SHARDS = {f'{row}-{index // 8}': (row, ids[index:index + 8])
                 for row, ids in SOURCE_MUTANTS.items() for index in range(0, len(ids), 8)}
AUDIT_GOLDEN_CASES = json.loads((HERE / 'audit_golden_cases.json').read_text(encoding="utf-8"))
W1A = ('tokens', 'tokenless', 'pair-budget', 'config', 'extractor', 'db-down',
       'lease-held', 'reaper', 'dim-mismatch', 'stamped', 'seeded',
       'json-float-shortest-ties', 'trust-bind', 'null-embedding',
       'unconstrained-dims', 'db-lost', 'encodings', 'map-order', 'map-order-reversed')

# These are observed differences, not comparator relaxations. Each omitted
# selection remains named so the inventory cannot silently erase the gap.
EXCEPTIONS = {
    'w1a': {
        'tokens': 'Python-only retrieval_events writes and sequence differences; live Linux 2026-10-11.',
        'tokenless': 'Python-only retrieval_events writes and sequence differences; live Linux 2026-10-11.',
        'config': 'Python-only retrieval_events writes and sequence differences; live Linux 2026-10-11.',
        'seeded': 'Python-only retrieval_events writes and sequence differences; live Linux 2026-10-11.',
        'json-float-shortest-ties': 'Python-only retrieval_events writes and sequence differences; live Linux 2026-10-11.',
        'encodings': 'Three HTTP responses match, but hosted Linux run 38139355489 differs on the bank sequence catalog in live and golden modes; Windows run 38132737537 differs on eleven initialization bank entries. The auth-only scenario does not settle initialization. The separately passing auth-candidate-order control remains selected on both OSes.',
    },
    'daemon_mutants': {
        'del-unescaped': 'Its tokens witness has the named retrieval telemetry bank-state difference; the control baseline is not clean.',
        'serde-json-writer': 'Survived the clean extractor witness on Linux 2026-10-11; JSON float witnesses have the named telemetry difference.',
        'no-backoff': 'Lease-held mutation did not complete recovery and produced a scenario error (Linux 2026-10-11); an error is not a caught source control.',
        'route-405-as-404': 'Survived the clean pair-budget and security-open witnesses on Linux 2026-10-11; tokens/tokenless witnesses have the named telemetry difference.',
        'golden-float-token': 'Its json-float-shortest-ties witness has the named retrieval telemetry bank-state difference; the control baseline is not clean.',
    },
    'w1a_golden': {name: 'Existing timeline/live-only scenario has no golden replay; both arms execute live.'
                   for name in ('lease-held', 'reaper', 'stamped', 'null-embedding',
                                'unconstrained-dims', 'db-lost')},
    'w1a_platform': {
        'linux': {},
        'windows': {},
    },
    'w1a_selections': {
        'windows-map-order-unsettled-bank-init': {
            'platform': 'windows', 'mode': 'both', 'scenario': 'map-order',
            'reason': 'Hosted Windows run 38187846311 failed golden and run 38192913404 failed live: the one HTTP response matches, but bank differences (57 golden entries, 50 live entries) compare initialized Python state with an empty Rust catalog. The auth-only scenario has settle=False and snapshots unsettled initialization. Both Linux map-order modes, both reversed-map modes and auth-candidate-order remain selected.',
        },
    },
    'cli_mutants': {
        'linux': {
            'connect-config-not-private': 'Survived Linux 2026-10-11; the file is already created with private POSIX mode before make_private.',
            'connect-remote-keeps-spawn': 'Survived Linux 2026-10-11; the existing dry-run-remote fixture does not observe the changed empty NO_SPAWN branch.',
            'tl-script-lf': 'Survived Linux 2026-10-11 with its container witness; text_bytes already uses LF on POSIX.',
        },
        'windows': {
            'connect-remote-keeps-spawn': 'The existing dry-run-remote fixture does not observe the changed empty NO_SPAWN branch (Linux survivor; platform-neutral fixture).',
            'tl-container-db-ignored': 'Container-only witness needs the Linux Docker engine; hosted Windows uses Windows containers.',
            'tl-script-lf': 'Container-only witness needs the Linux Docker engine; hosted Windows uses Windows containers.',
            'tl-keep-staged-on-failure': 'Container-only witness needs the Linux Docker engine; hosted Windows uses Windows containers.',
        },
    },
    'audit_golden': 'Only the stable cases in audit_golden_cases.json replay across processes (29 measured on Linux 2026-10-11; 23 after #730 ported six deferral cases, whose successors are not yet measured for cross-process stability); the rest carry fresh IDs, hashes or redaction state. Every audit case runs live.',
}

DAEMON_CONTROLS = {
    'auth-candidate-order': ['encodings'],
    'skip-relation-seed': ['extractor'],
    'skip-lease-epoch': ['extractor'],
    'drop-alter-tail': ['extractor'],
    'pair-ignores-origin': ['security-open'],
    'health-omits-db': ['extractor'],
    'body-limit-off': ['body-limits'],
    'tokenless-maintainer-open': ['body-view-open'],
    'static-redirect': ['static-build'],
    'static-no-csp': ['static-build'],
    'static-traversal-open': ['static-paths'],
    'static-wrong-type': ['static-build'],
    'static-json-whitespace': ['static-paths'],
    'static-string-prefix': ['static-paths'],
    'body-limit-inclusive': ['body-limits'],
    'text-limit-control': ['body-text-window'],
    'agents-view-default': ['body-view-open'],
}
GRAPH_CONTROLS = {
    'store': ['graph-confidence', 'graph-origin', 'graph-revive', 'graph-bless',
              'graph-alias', 'graph-relink', 'graph-merge', 'graph-normalize'],
    'read': ['graph-read-alias', 'graph-read-degree', 'graph-read-transitive',
             'graph-read-inverse-closure', 'graph-read-provenance', 'graph-read-depth',
             'graph-read-path-order', 'graph-read-suggestions'],
}
BACKGROUND = ('session-reap', 'session-restart', 'session-resume-refusal',
              'session-sweep', 'session-tombstone-restart', 'sweep-recovery')
SUITES = {
    'h5-audit': [dict(kind='cli', row='audit', mode=mode,
                      cases=AUDIT_GOLDEN_CASES if mode != 'live' else [])
                 for mode in ('live', 'record', 'golden')],
    'h5-backup': [dict(kind='cli', row='backup', mode=mode, cases=[])
                  for mode in ('live', 'record', 'golden')],
    'h5-w1a': [dict(kind='daemon', scenario=name, mode=mode, record=mode == 'live')
                for name in W1A if name not in EXCEPTIONS['w1a']
                for mode in ('live', 'golden')],
    'h5-background': [dict(kind='daemon', scenario=name, mode='live') for name in BACKGROUND],
    'h5-daemon-mutants': [dict(kind='daemon', mutant=name, scenarios=scenarios, mode='mutants')
                          for name, scenarios in DAEMON_CONTROLS.items()],
    'h5-graph-mutants': [dict(kind='graph', mode='mutants', row=row) for row in GRAPH_CONTROLS],
}


# Platform-specific declarations are pinned separately from observed failures.
# Host runtime registration must match this roster before a shard can filter.
PLATFORM_EXCLUSIONS = {
    'linux': {
        'backup-dotfile-suffix-windows': 'Registered only by the Windows backup source branch.',
        'backup-member-mode-windows': 'Registered only by the Windows backup source branch.',
        'backup-top-order-windows': 'Registered only by the Windows backup source branch.',
        'backup-owner-name-windows': 'Declared Windows-only empty archive owner-name witness; POSIX names are resolved through NSS.',
        'doctor-json-depth-off-by-one': 'Registered only by the Windows doctor source branch.',
        'doctor-pathlib-keeps-dot-parts': 'Registered only by the Windows doctor source branch.',
        'doctor-which-skips-cwd': 'Registered only by the Windows doctor source branch.',
        'episode-home-drive-insensitive': 'Declared Windows-only drive spelling witness.',
        'hook-first-address-only': 'Declared Windows-only resolver witness.',
        'pair-cleanup-error-swallowed': 'Registered only by the Windows pairing source branch.',
        'pair-not-owner-only': 'Registered only by the Windows pairing source branch.',
        'tunnel-dangling-junction-answered': 'Registered only by the Windows junction source branch.',
    },
    'windows': {
        'backup-member-mode-posix': 'Registered only by the POSIX backup source branch.',
        'backup-omits-fifo': 'Declared Linux-only FIFO member witness.',
        'backup-raw-member-name': 'Declared Linux-only raw-byte member name witness.',
        'backup-discovery-generic-errno-class': 'Declared Linux-only POSIX permission fixtures exercise the Unix errno-to-exception-class table.',
        'pair-mode-not-owner-only': 'Registered only by the POSIX pairing source branch.',
    },
}
# These two controls have no selected OS witness; global gaps cannot arise
# merely by adding the same platform exception twice.
UNOBSERVED_CONTROLS = {
    'connect-remote-keeps-spawn': 'Linux witness survived; Windows is inferred from the platform-neutral dry-run fixture and has not been measured.',
    'tl-script-lf': 'Linux container witness survived because text_bytes already uses LF; hosted Windows lacks the Linux container witness.',
}
