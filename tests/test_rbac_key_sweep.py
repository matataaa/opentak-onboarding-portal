"""
Which permission key owns which route or field: built-in <x>_admin roles hold every key of their module,
so tests/rbac_golden.json cannot tell a route guarded with the wrong key of the right module.
See key_sweep() in tests/rbac_matrix.py.
"""
import json

import pytest

from app.permissions import ASSIGNABLE_PERMISSIONS
from tests.rbac_matrix import SWEEP_PATH, Matrix, case_module, key_sweep

KEYS_WITHOUT_ROUTE = {'api_docs.view'}  # UI only: /api/docs has no role check


@pytest.fixture(scope='module')
def run():
    with Matrix() as matrix:
        yield matrix, key_sweep(matrix)


def test_matches_the_frozen_sweep(run):
    with open(SWEEP_PATH) as f:
        frozen = json.load(f)
    current = run[1]
    differences = [f'{name}: {frozen.get(name)} -> {current.get(name)}'
                   for name in sorted(set(frozen) | set(current)) if frozen.get(name) != current.get(name)]
    assert not differences, f'{len(differences)} differences:\n' + '\n'.join(differences)


def test_every_key_owns_something(run):
    owned = {label.split(' next to ')[0] for keys in run[1].values() for label in keys}
    assert ASSIGNABLE_PERMISSIONS - owned == KEYS_WITHOUT_ROUTE


def test_every_guarded_module_route_is_in_the_sweep(run):
    matrix, sweep = run
    for case in matrix.cases:
        if case_module(case):
            assert case.name in sweep
