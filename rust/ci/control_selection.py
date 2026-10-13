"""Fail closed when runtime CLI registration differs from the CI roster."""

from pathlib import Path
import sys

from coverage_plan import EXCEPTIONS, PLATFORM_EXCLUSIONS, SOURCE_MUTANTS

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def runtime_controls(row, platform):
    from rust.cli_harness import rows

    controls = rows.mutants(row)
    registered = [m.id for m in controls]
    assert len(registered) == len(set(registered)), 'duplicate runtime source-control registration'
    declared = set(SOURCE_MUTANTS[row])
    assert set(registered) <= declared, 'unknown runtime source-control registration'
    eligible = {m.id for m in controls if platform in m.platforms}
    expected = declared - set(PLATFORM_EXCLUSIONS[platform])
    assert eligible == expected, ('runtime source-control eligibility drift', row, platform,
                                  sorted(expected - eligible), sorted(eligible - expected))
    return eligible


def select_controls(row, selected, platform):
    eligible = runtime_controls(row, platform)
    excluded = set(EXCEPTIONS['cli_mutants'][platform]) | set(PLATFORM_EXCLUSIONS[platform])
    assert set(selected) <= eligible | excluded, 'unaccounted runtime source-control selection'
    return [name for name in selected if name not in excluded]
