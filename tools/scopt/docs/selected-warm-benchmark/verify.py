"""Verify publication checksums and reproduce the four-arm benchmark from CSV."""
import csv
import hashlib
import itertools
import json
import math
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1]))
from scopt.report.stats import paired_speedup
from scopt.run.warm_selection import _paired_evidence

ARMS = ('stock', 'nocache', 'warm', 'selected')
COMPARISONS = list(itertools.combinations(ARMS, 2))


def rows(filename):
    with (ROOT / filename).open(newline='') as stream:
        return list(csv.DictReader(stream))


def check_numbers(actual, expected):
    assert len(actual) == len(expected)
    assert all(math.isclose(float(a), float(b), rel_tol=1e-12) for a, b in zip(actual, expected)), (actual, expected)


def verify():
    for line in (ROOT / 'SHA256SUMS').read_text().splitlines():
        expected, filename = line.split('  ', 1)
        assert hashlib.sha256((ROOT / filename).read_bytes()).hexdigest() == expected, filename
    provenance = json.loads((ROOT / 'provenance.json').read_text())
    samples = rows('samples.csv')
    calibration = rows('calibration.csv')
    summary = {(r['firmware'], r['timing_scope']): r for r in rows('summary.csv')}
    suite = json.loads((ROOT / 'suite.json').read_text())
    assert (len(samples), len(calibration), len(summary)) == (576, 144, 12)
    ratios = {}
    for name, fw in provenance['firmwares'].items():
        pairs = []
        for record in (r for r in calibration if r['firmware'] == name):
            first = record['first']
            pairs.append(dict(first=first, second='warm' if first == 'candidate' else 'candidate',
                              warm=int(record['warm_ns']), candidate=int(record['candidate_ns'])))
        assert len(pairs) == 24 and sum(p['first'] == 'candidate' for p in pairs) == 12
        evidence = _paired_evidence(pairs)
        assert evidence['passes'] == (fw['deployed_choice'] == 'candidate')
        assert evidence['wins'] == fw['calibration_wins_out_of_24']
        check_numbers([evidence['p_value']], [fw['calibration_p']])
        blocks = {}
        for record in (r for r in samples if r['firmware'] == name):
            block = blocks.setdefault(int(record['block']), {})
            arm = record['arm']
            assert arm not in block and arm in ARMS
            block[arm] = record
            assert int(record['guest_ns']) > 0 and int(record['wall_ns']) > 0
            assert int(record['return_value'], 0) == int(fw['expected_r0'], 0)
            assert record['executable_sha256'] == provenance['executables_sha256']['stock' if arm == 'stock' else 'qemu']
            if arm in ('warm', 'selected'):
                expected = fw['cache_sha256']['ordinary' if arm == 'warm' else 'deployed']
                assert record['cache_sha256'] == expected
                assert record['replay_verdict'] == 'PASS' and record['appended'] == '0'
            if arm == 'selected':
                assert record['selection_receipt_sha256'] == fw['selection_receipt_sha256']
        assert set(blocks) == set(range(24))
        orders = [tuple(sorted(block, key=lambda a: int(block[a]['position']))) for block in blocks.values()]
        assert set(orders) == set(itertools.permutations(ARMS))
        for scope in ('guest', 'wall'):
            expected = summary[name, scope]
            values = {arm: [int(blocks[i][arm][scope + '_ns']) for i in range(24)] for arm in ARMS}
            check_numbers([statistics.median(values[a]) / 1e6 for a in ARMS],
                          [expected[a + '_median_ms'] for a in ARMS])
            for a, b in COMPARISONS:
                key = a + '_to_' + b
                measured = paired_speedup(values[a], values[b], reps=20000,
                                          seed=fw['timing_order_and_pointwise_bootstrap_seed'])
                check_numbers([measured['median'], *measured['ci']],
                              [expected[key + suffix] for suffix in ('_median_speedup', '_ci95_low', '_ci95_high')])
                ratios.setdefault((scope, key), []).append(measured['ratios'])
    try:
        import numpy as np
    except ImportError:
        np = None
    for (scope, key), by_firmware in ratios.items():
        expected = suite[scope][key]
        check_numbers([statistics.geometric_mean(statistics.median(x) for x in by_firmware)], [expected['median']])
        if np is not None:
            rng = np.random.default_rng(20261120)
            draws = [np.median(rng.choice(x, size=(20000, len(x))), axis=1) for x in by_firmware]
            check_numbers(np.quantile(np.exp(np.mean(np.log(draws), axis=0)), [.025, .975]), expected['ci'])
    print('Verified 6 selections, 576 measurements, 288 warm replays, 72 firmware comparisons and 12 suite estimates.')
    print('Verified 12 suite intervals.' if np is not None else 'Install NumPy to also check the 12 suite intervals.')


if __name__ == '__main__':
    verify()
