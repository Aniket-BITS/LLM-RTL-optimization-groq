#!/usr/bin/env python3
"""Report checkpointed RTL-OPT calibration without modifying source data."""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('experiment', type=Path)
a = p.parse_args()
out = a.experiment.resolve()
root = Path(__file__).resolve().parents[1]
m = json.loads((out / 'pair_manifest.json').read_text())
rows = []
pairs = []
for imported in m['pairs']:
    cp = out / imported['id'] / 'result.json'
    pair = json.loads(cp.read_text()) if cp.exists() else imported
    pair['inferred_class'] = {s: 'sequential' if pair[s]['edge_signals'] else 'combinational' for s in ('baseline', 'reference')}
    pair['orientation'] = {'baseline': 'dataset non-reference RTL', 'reference': 'dataset expert-optimized _ref RTL; benefit subject to screening'}
    pairs.append(pair)
    row = {'pair': pair['id'], 'status': pair['status'], 'language': pair['language'],
           'equivalence': pair.get('verification', {}).get('status', 'not_run'),
           'selection_reason': pair.get('selection', {}).get('reason', 'pending'),
           'transformation_family_hypothesis': pair['transformation_family']}
    for side in ('baseline', 'reference'):
        row[side + '_top'] = pair[side]['top']
        row[side + '_class'] = pair['inferred_class'][side]
        pp = pair.get('ppa', {}).get(side, {})
        row[side + '_ppa'] = pp.get('status', 'not_run')
        for key in ('area_um2', 'power_w', 'setup_ws_ns', 'hold_ws_ns', 'setup_tns_ns', 'hold_tns_ns'):
            row[side + '_' + key] = pp.get('metrics', {}).get(key)
    b = pair.get('ppa', {}).get('baseline', {}).get('metrics', {})
    r = pair.get('ppa', {}).get('reference', {}).get('metrics', {})
    for metric in ('area_um2', 'power_w'):
        row[metric + '_gain_pct'] = 100 * (b[metric] - r[metric]) / b[metric] if b.get(metric, 0) > 0 and metric in r else None
    row['setup_slack_gain_ns'] = r['setup_ws_ns'] - b['setup_ws_ns'] if 'setup_ws_ns' in b and 'setup_ws_ns' in r else None
    rows.append(row)
integrity = []
for line in (out / 'corpus_before.sha256').read_text().splitlines():
    expected, name = line.split('  ', 1)
    path = root / name
    actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    integrity.append({'path': name, 'expected': expected, 'actual': actual, 'unchanged': actual == expected})
summary = {'pairs': len(pairs), 'statuses': dict(Counter(p['status'] for p in pairs)),
           'equivalence': dict(Counter(r['equivalence'] for r in rows)),
           'physical_sides': dict(Counter(r[s + '_ppa'] for r in rows for s in ('baseline', 'reference'))),
           'corpus_files_checked': len(integrity), 'corpus_unchanged': bool(integrity) and all(r['unchanged'] for r in integrity)}
for name, value in [('screening_manifest.json', {**m, 'pairs': pairs}), ('screening_summary.json', summary), ('corpus_integrity.json', integrity)]:
    (out / name).write_text(json.dumps(value, indent=2) + '\n')
with (out / 'screening.csv').open('w', newline='') as handle:
    writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
lines = ['# NEBULA RTL-OPT screening', '', '## Coverage', '', '```json', json.dumps(summary, indent=2), '```', '',
'## Method and limits', '',
'- Pinned MIT corpus. Baseline is the non-reference file; reference is the dataset _ref file. The label alone establishes neither equivalence nor physical improvement. Extracted and normalized copies are stored only in the experiment directory.',
'- Yosys elaboration checks ports, inferred latches and clocks. Default parameters only. Combinational SAT miter or sequential base case plus temporal induction, with reset assumptions recorded per pair. Unproven does not establish inequivalence. No latency-aware verification is claimed.',
'- Nangate45 global route with the existing qualified NEBULA PDN and repair hooks, density 0.6 and two cores. Fixed 120 x 120 um die; core 2.09,2.8 to 117.8,117.2 um, identical across each pair. Calibration geometry differs from the mapper experiment.',
'- Real or virtual clock: 0.60 ns; uncertainty 0.02 ns; I/O min 0 ns and max 0.10 ns. addr_calcu/control and mult_if/CTRL_is_late_arriving have 0.30 ns input max. Reset inputs are timed, with no false paths.',
'- Power is vectorless with default activity. Results are global-route estimates, not workload power or signoff measurements.',
'- Retention requires proof, successful paired PPA, clean reference setup and hold, measurable gain and bounded regressions; provisional wins require a second paired run. Exact thresholds are recorded in the manifest.',
'- Physical results for unproven pairs are diagnostic only. Transformation families are hypotheses from the runner, not independently validated classifications.', '',
'## Pair results', '',
'| Pair | Proof | Baseline PPA | Reference PPA | Area gain % | Power gain % | Setup gain ns | Decision |',
'|---|---|---|---|---:|---:|---:|---|']
def fmt(value):
    return '-' if value is None else f'{value:.4f}'
for row in rows:
    values = [row['pair'], row['equivalence'], row['baseline_ppa'], row['reference_ppa'], fmt(row['area_um2_gain_pct']), fmt(row['power_w_gain_pct']), fmt(row['setup_slack_gain_ns']), row['status'] + ': ' + row['selection_reason']]
    lines.append('| ' + ' | '.join(values) + ' |')
lines += ['', 'Detailed source hashes, recipe fingerprints, commands and log paths are in screening_manifest.json. screening.csv includes absolute metrics. Existing v2 results were reused only after the runner checked its identity and the saved recipe, sources, proof script and metrics.', '']
(out / 'REPORT.md').write_text('\n'.join(lines))
print(json.dumps(summary, indent=2))
