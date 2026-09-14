#!/usr/bin/env python3
"""Read-only RTL-OPT import, formal qualification and paired global-route PPA.

Run with PyArrow available, e.g. PYTHONPATH=/tmp/nebula-calibration-deps
python3 timing_automation/calibrate_pairs.py --jobs 2. No generation endpoint is used.
Each pair has an isolated directory and an atomic, resumable result checkpoint.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import math
import os
from pathlib import Path

try:
    from equivalence_terms import EquivalenceVerdict
except ImportError:
    from timing_automation.equivalence_terms import EquivalenceVerdict
import re
import signal
import subprocess
import time

from physical_recipe import build_physical_recipe, PhysicalRecipe, frozen_physical_variables

try:
    from nebula_env import flow_dir as resolve_flow_dir, yosys as resolve_yosys, openroad as resolve_openroad
except ImportError:
    from timing_automation.nebula_env import flow_dir as resolve_flow_dir, yosys as resolve_yosys, openroad as resolve_openroad


ROOT = resolve_flow_dir()
YOSYS = resolve_yosys(flow=ROOT, required=False)
OPENROAD = resolve_openroad(flow=ROOT, required=False)
SEED_RECIPE = ROOT / 'timing_automation/physical_recipes/calibration_seed.json'
PORTABLE_PHYSICAL_RECIPE_DIR = ROOT / 'timing_automation/physical_recipes'
PORTABLE_PDN = PORTABLE_PHYSICAL_RECIPE_DIR / 'nebula_nangate45_pdn.tcl'
PORTABLE_REPAIR_BUDGET = PORTABLE_PHYSICAL_RECIPE_DIR / 'nebula_nangate45_repair_budget.tcl'
POLICY = {
    'area_improvement_fraction': .01, 'power_improvement_fraction': .01,
    'setup_slack_improvement_ns': .010,
    'area_regression_fraction': .005, 'power_regression_fraction': .005,
    'setup_slack_regression_ns': .005, 'hold_slack_regression_ns': .005,
    'require_reference_setup_and_hold_clean': True,
    'power_model': 'OpenSTA vectorless estimate; identical default activity',
    'repeat_policy': 'repeat both sides for provisional keep; both comparisons must pass',
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, allow_nan=False) + '\n')
    tmp.replace(path)


def run(command, log, timeout=600):
    """Timeout the whole process group, including make's EDA children."""
    env = dict(os.environ)
    env['PATH'] = f'{YOSYS.parent}:{OPENROAD.parent}:' + env['PATH']
    env['OMP_NUM_THREADS'] = '2'
    started = time.time()
    with Path(log).open('w') as out:
        proc = subprocess.Popen([str(x) for x in command], cwd=ROOT, env=env,
                                stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            code = 124
    return {'exit_code': code, 'elapsed_s': round(time.time() - started, 3),
            'log': str(log), 'command': [str(x) for x in command]}


def family(name):
    if name in {'fsm', 'fsm_encode', 'gray', 'ticket_machine'}:
        return 'fsm_state_encoding'
    if name.startswith('comparator'): return 'comparator_boolean_decomposition'
    if name.startswith('decoder'): return 'decoder_shift_vs_equality'
    if name.startswith('divider'): return 'division_algorithm_restructuring'
    if name.startswith('sub_') or name in {'adder_carry', 'add_sub'}:
        return 'arithmetic_carry_borrow_decomposition'
    return {
        'adder': 'operand_isolation_removal', 'adder_select': 'mux_before_arithmetic_sharing',
        'addr_calcu': 'mux_before_arithmetic_sharing',
        'alu_8bit': 'arithmetic_resource_sharing', 'alu_64bit': 'arithmetic_resource_sharing',
        'calculation': 'arithmetic_common_subexpression', 'mul_const': 'constant_multiply_strength_reduction',
        'mul_subexpression': 'balanced_adder_tree', 'mul': 'rounding_width_restructuring',
        'mac': 'pipeline_stage_removal', 'saturating_add': 'state_removal_saturation_decode',
        'selector': 'arithmetic_to_lookup_decode', 'register': 'register_file_decode_cleanup',
        'mux_dead': 'dead_logic_hierarchy', 'mux_encode': 'binary_to_onehot_selection',
        'mult_if': 'priority_late_control_restructuring',
    }.get(name, 'mux_restructuring')


def prepare(out):
    import pyarrow.parquet as pq
    corpus = json.loads((ROOT / 'timing_automation/rtl_opt_corpus.json').read_text())
    payload = ROOT / corpus['source']['payload']
    if digest(payload) != corpus['source']['payload_sha256']:
        raise ValueError('Corpus payload hash changed')
    rows = {r['file_name']: r['text'] for r in pq.read_table(payload).to_pylist()}
    pairs = []
    for old in corpus['pairs']:
        pair = dict(old)
        directory = out / pair['id']
        directory.mkdir(parents=True, exist_ok=True)
        sides = {}
        for side in ('baseline', 'reference'):
            text = rows[pair[f'{side}_file']]
            if hashlib.sha256(text.encode()).hexdigest() != pair[f'{side}_text_sha256']:
                raise ValueError(f'Source hash mismatch: {pair["id"]}/{side}')
            source = directory / pair[f'{side}_file']
            if source.exists() and source.read_text() != text:
                raise ValueError(f'Extracted source modified: {source}')
            source.write_text(text)
            stripped = re.sub(r'/\*.*?\*/|//[^\n]*', '', text, flags=re.S)
            top = re.search(r'\bmodule\s+(\w+)', stripped).group(1)
            sides[side] = {'source': str(source), 'sha256': digest(source), 'top': top,
                           'edge_signals': sorted(set(re.findall(r'(?:pos|neg)edge\s+(\w+)', stripped)))}
        sequential = any(s['edge_signals'] for s in sides.values())
        pair.update(sides)
        pair['top_module'] = {s: sides[s]['top'] for s in sides}
        pair['transformation_family'] = family(pair['id'])
        pair['verification_method'] = {
            'backend': 'yosys', 'mode': 'sequential_equivalence' if sequential else 'combinational_cec',
            'contract': 'same-cycle outputs from sample 2, default parameters, unrestricted data; reset asserted at sample 1 when present; async reset sampled using async2sync',
            'algorithm': 'SAT miter; sequential base case plus temporal induction',
            'max_induction_steps': 24, 'timeout_s': 120,
            'latency_aware_required': pair['id'] == 'mac',
        }
        pair['timing_profile'] = {
            'name': 'single_clock_0p6ns' if sequential else 'virtual_clock_0p6ns',
            'period_ns': .6, 'clock_port': 'clk' if sequential else None,
            'uncertainty_ns': .02, 'input_delay_min_ns': 0., 'input_delay_max_ns': .1,
            'output_delay_min_ns': 0., 'output_delay_max_ns': .1,
            'late_inputs': {'control': .3} if pair['id'] == 'addr_calcu' else
                           {'CTRL_is_late_arriving': .3} if pair['id'] == 'mult_if' else {},
            'exceptions': [], 'reset_policy': 'reset inputs timed as ordinary inputs; no false paths',
            'parameters': 'source defaults only',
        }
        pair['status'] = 'pending'
        pair['calibration_policy'] = POLICY
        pair['runner_sha256'] = digest(__file__)
        pairs.append(pair)
    manifest = {'schema': 'nebula_pair_calibration/1', 'source': corpus['source'],
                'policy': POLICY, 'pair_count': len(pairs), 'pairs': pairs}
    write_json(out / 'pair_manifest.json', manifest)
    return manifest


def elaborate(pair, directory):
    modules = {}
    for side in ('baseline', 'reference'):
        info = pair[side]
        script = directory / f'{side}.elaborate.ys'
        netlist = directory / f'{side}.json'
        script.write_text(f'read_verilog -sv {info["source"]}\n'
                          f'hierarchy -check -top {info["top"]}\n'
                          f'prep -flatten -top {info["top"]}\ncheck -assert\nwrite_json {netlist}\n')
        result = run([YOSYS, '-s', script], directory / f'{side}.elaborate.log', 120)
        pair[side]['elaboration'] = result
        if result['exit_code']: return None
        modules[side] = json.loads(netlist.read_text())['modules'][info['top']]
    ports = lambda m: {n: (p['direction'], len(p['bits'])) for n, p in m['ports'].items()}
    if ports(modules['baseline']) != ports(modules['reference']):
        pair['qualification_reason'] = 'Port names, directions or widths differ'
        return None
    for side, module in modules.items():
        if any('latch' in c['type'].lower() for c in module['cells'].values()):
            pair['qualification_reason'] = f'{side}: inferred latches unsupported'
            return None
        clocks = {tuple(c['connections']['CLK']) for c in module['cells'].values()
                  if 'CLK' in c['connections']}
        expected = tuple(module['ports'].get('clk', {}).get('bits', []))
        if clocks and clocks != {expected}:
            pair['qualification_reason'] = f'{side}: generated/multiple clocks need explicit contract'
            return None
    return modules['baseline']


def verify(pair, directory):
    lines = []
    for side, alias in [('baseline', 'gold'), ('reference', 'gate')]:
        info = pair[side]
        lines += ['design -reset-vlog', f'read_verilog -sv {info["source"]}',
                  f'prep -flatten -top {info["top"]}', 'memory_map', 'opt_clean',
                  f'rename {info["top"]} {alias}', f'design -stash {alias}']
    lines += ['design -copy-from gold -as gold gold', 'design -copy-from gate -as gate gate']
    lines += ['miter -equiv -make_assert -make_outputs -flatten gold gate miter', 'prep -top miter']
    if pair['verification_method']['mode'] == 'combinational_cec':
        lines += ['sat -verify -prove-asserts -show-inputs -show-outputs miter']
    else:
        ports = json.loads((directory / 'baseline.json').read_text())['modules'][pair['baseline']['top']]['ports']
        resets = ''.join(f' -set-at 1 in_{name} {0 if name == "rst_n" else 1}'
                         for name in ('rst_n', 'reset', 'clear') if name in ports)
        lines += ['async2sync', 'opt -full',
                  'sat -verify -prove-asserts -seq 1 -tempinduct -maxsteps 24'
                  + resets + ' -show-inputs -show-outputs miter']
    script = directory / 'proof.ys'
    script.write_text('\n'.join(lines) + '\n')
    result = run([YOSYS, '-s', script], directory / 'proof.log', 120)
    result['status'] = 'passed' if result['exit_code'] == 0 else 'timeout' if result['exit_code'] == 124 else 'unproven'
    result['script_sha256'] = digest(script)
    return result


def freeze(pair, directory, module):
    profile = pair['timing_profile']
    clk = profile['clock_port']
    sdc = directory / 'constraint.sdc'
    lines = ['# Calibration profile: ns; all I/O timed; no timing exceptions.',
             'create_clock -name core_clock -period 0.6' + (' [get_ports {clk}]' if clk else ''),
             'set_clock_uncertainty 0.02 [get_clocks core_clock]']
    for name, port in module['ports'].items():
        if name == clk: continue
        direction = port['direction']
        if direction not in ('input', 'output'): raise ValueError('inout port unsupported')
        delay = profile['late_inputs'].get(name, .1) if direction == 'input' else .1
        lines += [f'set_{direction}_delay -min 0 -clock core_clock [get_ports {{{name}}}]',
                  f'set_{direction}_delay -max {delay} -clock core_clock [get_ports {{{name}}}]']
    sdc.write_text('\n'.join(lines) + '\n')
    spec = json.loads(SEED_RECIPE.read_text())
    # Historical recipes contain machine-local paths. Use project-local frozen
    # copies so a checkout can move without changing the physical experiment.
    for entry in spec['files']:
        if entry['role'] == 'pdn':
            entry['path'] = str(PORTABLE_PDN)
        if entry['role'] == 'timing_repair_hook':
            entry['path'] = str(PORTABLE_REPAIR_BUDGET)
    spec['flow_variables'].update(PDN_TCL=str(PORTABLE_PDN),
                                  PRE_CTS_TCL=str(PORTABLE_REPAIR_BUDGET))
    top = pair['baseline']['top']
    nickname = 'rtlopt_' + pair['id']
    spec['recipe_id'] = nickname + '_calibration'
    spec['design'] = {'design_id': nickname, 'top': top, 'platform': 'nangate45'}
    # Fixed a priori, never selected from reference area. Room for the entire corpus.
    variables = spec['flow_variables']
    variables.update(DIE_AREA='0 0 120 120', CORE_AREA='2.09 2.8 117.8 117.2', NUM_CORES=2)
    config = directory / 'config.mk'
    settings = {'PLATFORM': 'nangate45', 'DESIGN_NAME': top, 'DESIGN_NICKNAME': nickname,
                'SDC_FILE': str(sdc), **variables}
    config.write_text('\n'.join(f'export {k} = {v}' for k, v in settings.items()) + '\n')
    for entry in spec['files']:
        if entry['role'] == 'design_config': entry['path'] = str(config)
        if entry['role'] == 'constraints': entry['path'] = str(sdc)
    spec['toolchain'] = {'orfs_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                         'yosys_revision': subprocess.check_output([YOSYS, '-V'], text=True).strip(),
                         'openroad_revision': subprocess.check_output([OPENROAD, '-version'], text=True).strip()}
    # Include flow scripts and platform files, including local modifications.
    for folder in ('scripts', 'util', 'platforms/nangate45'):
        for path in sorted((ROOT / folder).rglob('*')):
            if path.is_file() and path.suffix in {'.tcl', '.mk', '.lef', '.lib', '.v', '.py'}:
                spec['files'].append({'role': 'flow_dependency', 'path': str(path)})
    for path in [ROOT / 'Makefile', ROOT / 'scripts/variables.yaml']:
        spec['files'].append({'role': 'flow_dependency', 'path': str(path)})
    spec['metadata'] = {'timing_profile': profile, 'geometry_origin': 'a priori 120um calibration die, identical for both sides',
                        'seed_recipe': str(SEED_RECIPE), 'policy': POLICY}
    recipe = build_physical_recipe(spec, base_dir=ROOT)
    frozen_physical_variables(recipe)
    recipe.write_json(directory / 'physical_recipe.json')
    pair['physical_recipe'] = {'path': str(directory / 'physical_recipe.json'),
                               'fingerprint_sha256': recipe.fingerprint_sha256}
    # Only rename the reference top for physical-flow identity; formal uses originals.
    reference = Path(pair['reference']['source']).read_text()
    reference, count = re.subn(r'(\bmodule\s+)' + re.escape(pair['reference']['top']) + r'\b',
                               lambda m: m[1] + top, reference, count=1)
    if count != 1: raise ValueError('Top normalization failed')
    normalized = directory / 'reference_physical.v'
    normalized.write_text(reference)
    pair['reference']['physical_source'] = str(normalized)
    pair['reference']['physical_source_sha256'] = digest(normalized)
    return recipe


def physical(pair, directory, side, recipe, repeat=0):
    if not recipe.verify_frozen_files()['ok']: raise ValueError('Frozen recipe mutated')
    info = pair[side]
    source = info.get('physical_source', info['source'])
    expected = info.get('physical_source_sha256', info['sha256'])
    if digest(source) != expected: raise ValueError('Physical source mutated')
    variant = f'calibration_{directory.parent.name}_{side}_{repeat}'
    result = run(['make', f'DESIGN_CONFIG={directory / "config.mk"}', f'FLOW_VARIANT={variant}',
                  f'VERILOG_FILES={source}', 'grt'], directory / f'{side}.ppa{repeat}.log', 900)
    result['recipe_fingerprint_sha256'] = recipe.fingerprint_sha256
    result['source_sha256'] = expected
    metrics_path = ROOT / 'logs/nangate45' / ('rtlopt_' + pair['id']) / variant / '5_1_grt.json'
    result['metrics_path'] = str(metrics_path)
    if result['exit_code'] == 0 and metrics_path.exists():
        raw = json.loads(metrics_path.read_text())
        keys = {'area_um2': 'design__instance__area', 'power_w': 'power__total',
                'setup_ws_ns': 'timing__setup__ws', 'hold_ws_ns': 'timing__hold__ws',
                'setup_tns_ns': 'timing__setup__tns', 'hold_tns_ns': 'timing__hold__tns'}
        metrics = {k: raw.get('globalroute__' + v) for k, v in keys.items()}
        if all(isinstance(v, (int, float)) and math.isfinite(v) for v in metrics.values()):
            result['metrics'] = metrics
            result['metrics_sha256'] = digest(metrics_path)
    result['status'] = 'passed' if 'metrics' in result else 'failed'
    return result


def assess(baseline, reference, proof):
    if proof['status'] != 'passed': return {'keep': False, 'reason': EquivalenceVerdict.NOT_PROVEN.value}
    if any(s.get('status') != 'passed' for s in (baseline, reference)):
        return {'keep': False, 'reason': 'physical_flow_failed'}
    if baseline['recipe_fingerprint_sha256'] != reference['recipe_fingerprint_sha256']:
        return {'keep': False, 'reason': 'recipe_mismatch'}
    b, r = baseline['metrics'], reference['metrics']
    if b['area_um2'] <= 0 or b['power_w'] <= 0:
        return {'keep': False, 'reason': 'degenerate_baseline'}
    area = (b['area_um2'] - r['area_um2']) / b['area_um2']
    power = (b['power_w'] - r['power_w']) / b['power_w']
    slack = r['setup_ws_ns'] - b['setup_ws_ns']
    measurable = area >= .01 or power >= .01 or slack >= .01
    clean = r['setup_ws_ns'] >= 0 and r['hold_ws_ns'] >= 0 and r['setup_tns_ns'] >= 0 and r['hold_tns_ns'] >= 0
    nonregressing = area >= -.005 and power >= -.005 and slack >= -.005 and r['hold_ws_ns'] >= b['hold_ws_ns'] - .005
    keep = measurable and clean and nonregressing
    return {'keep': keep, 'reason': 'measurable_headroom' if keep else 'reference_timing_violations' if not clean
            else 'ppa_tradeoff' if not nonregressing else 'below_measurement_threshold',
            'area_gain_pct': 100 * area, 'power_gain_pct': 100 * power,
            'setup_slack_gain_ns': slack}


def calibrate(pair, out):
    directory = out / pair['id']
    checkpoint = directory / 'result.json'
    if checkpoint.exists() and json.loads(checkpoint.read_text()).get('status') != 'error':
        previous = json.loads(checkpoint.read_text())
        if previous.get('runner_sha256') != pair['runner_sha256']:
            raise ValueError('Runner changed; use a new --output experiment directory')
        if previous.get('calibration_policy') != POLICY:
            raise ValueError('Calibration policy changed; use a new experiment directory')
        if previous.get('physical_recipe'):
            recipe = PhysicalRecipe.read_json(Path(previous['physical_recipe']['path']))
            if not recipe.verify_frozen_files()['ok']: raise ValueError('Cannot resume changed recipe')
            for side in ('baseline', 'reference'):
                info = previous[side]
                if digest(info['source']) != info['sha256']:
                    raise ValueError('Cannot resume changed source')
                if info.get('physical_source') and digest(info['physical_source']) != info['physical_source_sha256']:
                    raise ValueError('Cannot resume changed normalized source')
            if digest(directory / 'proof.ys') != previous['verification']['script_sha256']:
                raise ValueError('Cannot resume changed proof')
            for runs in (previous.get('ppa', {}), previous.get('repeat_ppa', {})):
                for record in runs.values():
                    if record.get('metrics_sha256') and digest(record['metrics_path']) != record['metrics_sha256']:
                        raise ValueError('Cannot resume changed metrics')
        return previous
    try:
        module = elaborate(pair, directory)
        if module is None:
            pair['status'] = 'unsupported'
            pair['selection'] = {'keep': False, 'reason': pair.get('qualification_reason', 'elaboration_failed')}
        else:
            pair['verification'] = verify(pair, directory)
            recipe = freeze(pair, directory, module)
            pair['ppa'] = {}
            # Measure both original sides before any search, even when proof is inconclusive.
            for side in ('baseline', 'reference'):
                print(f'{pair["id"]}: measuring {side}', flush=True)
                pair['ppa'][side] = physical(pair, directory, side, recipe)
            pair['selection'] = assess(pair['ppa']['baseline'], pair['ppa']['reference'], pair['verification'])
            if pair['selection']['keep']:
                pair['repeat_ppa'] = {s: physical(pair, directory, s, recipe, 1) for s in ('baseline', 'reference')}
                repeat = assess(pair['repeat_ppa']['baseline'], pair['repeat_ppa']['reference'], pair['verification'])
                pair['repeat_selection'] = repeat
                if not repeat['keep']: pair['selection'] = {'keep': False, 'reason': 'repeat_did_not_confirm'}
            pair['status'] = 'kept' if pair['selection']['keep'] else 'excluded'
    except Exception as exc:
        pair['status'] = 'error'
        pair['selection'] = {'keep': False, 'reason': f'{type(exc).__name__}: {exc}'}
    write_json(checkpoint, pair)
    return pair


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'ai_runs/rtl_opt_calibration_v1')
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--pairs', nargs='*')
    parser.add_argument('--manifest-only', action='store_true')
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    manifest = prepare(out)
    if args.manifest_only: return
    selected = [p for p in manifest['pairs'] if not args.pairs or p['id'] in args.pairs]
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(calibrate, p, out) for p in selected]
        results = {}
        for future in as_completed(futures):
            result = future.result()
            results[result['id']] = result
            print(f'{result["id"]}: {result["status"]}: {result["selection"]["reason"]}', flush=True)
            manifest['pairs'] = [results.get(p['id'], p) for p in manifest['pairs']]
            write_json(out / 'pair_manifest.json', manifest)
            write_json(out / 'retained_pairs.json', {**{k: manifest[k] for k in ('schema', 'source', 'policy')},
                       'pairs': [p for p in manifest['pairs'] if p.get('selection', {}).get('keep')]})
    counts = {s: sum(p['status'] == s for p in manifest['pairs']) for s in ('kept', 'excluded', 'unsupported', 'error', 'pending')}
    write_json(out / 'summary.json', counts)
    print(json.dumps(counts), flush=True)


if __name__ == '__main__':
    main()
