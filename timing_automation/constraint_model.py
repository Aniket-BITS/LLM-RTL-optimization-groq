"""Explicit multi-clock timing constraints, validation, and deterministic SDC.

No implicit clocks, IO delays, exceptions, or reset false paths are invented.
Selectors name exact STA objects; emitted SDC rejects empty collections.
"""
import argparse
import json
import math
import re
from pathlib import Path


class ConstraintError(ValueError):
    pass


def number(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConstraintError(f'{label} must be finite numeric')
    if positive and value <= 0:
        raise ConstraintError(f'{label} must be positive')
    return value


def token(value):
    # Braced Tcl literal, with no wildcards or substitution/escape ambiguity.
    if not isinstance(value, str) or not value or re.search(r'[\s{}\\;*$?]', value):
        raise ConstraintError(f'invalid exact STA object name: {value!r}')
    return '{' + value + '}'


def selector(value):
    if not isinstance(value, dict) or set(value) != {'kind', 'names'}:
        raise ConstraintError('selector requires exactly kind and names')
    kind = value['kind']
    if kind not in ('ports', 'pins', 'clocks'):
        raise ConstraintError(f'unsupported selector kind: {kind}')
    names = value['names']
    if not isinstance(names, list) or not names or len(set(names)) != len(names):
        raise ConstraintError('selector names must be nonempty and unique')
    return '[nebula_objects ' + kind + ' [list ' + ' '.join(token(n) for n in names) + ']]'


def validate_model(clocks, constraints, ports=None):
    clocks = [c.to_dict() if hasattr(c, "to_dict") else c for c in clocks]
    errors = []
    try:
        if constraints.get('schema') != 'nebula_constraints/1':
            raise ConstraintError('CONSTRAINT_MODEL_MISSING: explicit nebula_constraints/1 required')
        allowed = {'schema', 'generated_clocks', 'asynchronous_clock_groups', 'input_delays',
                   'output_delays', 'max_delays', 'false_paths', 'multicycle_paths'}
        unknown = set(constraints) - allowed
        if unknown:
            raise ConstraintError(f'unsupported constraint fields: {sorted(unknown)}')
        seen, clock_ports = set(), set()
        for clock in clocks:
            token(clock['name'])
            if clock['name'] in seen:
                raise ConstraintError('duplicate clock name')
            seen.add(clock['name'])
            number(clock['period_ns'], 'clock period', positive=True)
            if clock.get('virtual') is True:
                if clock.get('port'):
                    raise ConstraintError('virtual clock cannot have a port')
            else:
                token(clock['port'])
                if clock['port'] in clock_ports:
                    raise ConstraintError('multiple master clocks on one port are not supported')
                clock_ports.add(clock['port'])
                if ports is not None and (clock['port'] not in ports or ports[clock['port']]['direction'] != 'input' or ports[clock['port']].get('width', len(ports[clock['port']].get('bits', []))) != 1):
                    raise ConstraintError(f'IO_CONSTRAINT_INVALID: clock port {clock["port"]}')
            if clock.get('waveform_ns'):
                waveform = clock['waveform_ns']
                if len(waveform) != 2 or not 0 <= number(waveform[0], 'rise') < number(waveform[1], 'fall') < clock['period_ns']:
                    raise ConstraintError('waveform must satisfy 0 <= rise < fall < period')
            uncertainty = number(clock.get('uncertainty_ns', 0), 'clock uncertainty')
            if uncertainty < 0:
                raise ConstraintError('clock uncertainty must be nonnegative')
        for clock in constraints.get('generated_clocks', []):
            token(clock['name'])
            if clock['name'] in seen or clock['master_clock'] not in seen:
                raise ConstraintError('generated clock must name a previously defined master and have a unique name')
            selector(clock['source']); selector(clock['target'])
            if clock['source']['kind'] == 'clocks' or clock['target']['kind'] == 'clocks':
                raise ConstraintError('generated clock source/target must be ports or pins')
            ratio = clock['divide_by']
            if type(ratio) is not int or ratio < 1:
                raise ConstraintError('generated clock divide_by must be a positive integer')
            seen.add(clock['name'])
        grouped = set()
        for groups in constraints.get('asynchronous_clock_groups', []):
            if len(groups) < 2:
                raise ConstraintError('async relation requires at least two groups')
            local = set()
            for group in groups:
                if not group or not set(group) <= seen or local.intersection(group):
                    raise ConstraintError('async clock groups must be nonempty, disjoint, and reference declared clocks')
                local.update(group)
            grouped.update(local)
        covered = {'input': set(), 'output': set()}
        for direction in covered:
            for delay in constraints.get(direction + '_delays', []):
                if delay['clock'] not in seen:
                    raise ConstraintError('IO delay references unknown clock')
                lo = number(delay['min_ns'], 'IO minimum'); hi = number(delay['max_ns'], 'IO maximum')
                if lo > hi:
                    raise ConstraintError('IO min_ns exceeds max_ns')
                selector({'kind': 'ports', 'names': delay['ports']})
                for name in delay['ports']:
                    if name in clock_ports:
                        raise ConstraintError('IO delay cannot target a master clock port')
                    if ports is not None and (name not in ports or ports[name]['direction'] != direction):
                        raise ConstraintError(f'IO_CONSTRAINT_INVALID: {direction} port {name}')
                    if name in covered[direction]:
                        raise ConstraintError('multiple IO timing windows per port are not supported')
                    covered[direction].add(name)
        for kind in ('max_delays', 'false_paths', 'multicycle_paths'):
            for exception in constraints.get(kind, []):
                selector(exception['from']); selector(exception['to'])
                for endpoint in ('from', 'to'):
                    spec = exception[endpoint]
                    if spec['kind'] == 'clocks' and not set(spec['names']) <= seen:
                        raise ConstraintError('exception references unknown clock')
                    if spec['kind'] == 'ports' and ports is not None and not set(spec['names']) <= set(ports):
                        raise ConstraintError('IO_CONSTRAINT_INVALID: exception references unknown port')
                if kind == 'max_delays':
                    number(exception['ns'], 'max delay', positive=True)
                    # Pure combinational designs use explicit input-to-output budgets.
                    if not clocks and not constraints.get('generated_clocks'):
                        for endpoint, direction in [('from', 'input'), ('to', 'output')]:
                            spec = exception[endpoint]
                            if spec['kind'] == 'ports':
                                if ports is not None and any(ports[n]['direction'] != direction for n in spec['names']):
                                    raise ConstraintError('combinational max-delay direction is invalid')
                                covered[direction].update(spec['names'])
                else:
                    if not isinstance(exception.get('reason'), str) or not exception['reason'].strip():
                        raise ConstraintError('timing exceptions require a documented reason')
                if kind == 'multicycle_paths':
                    if type(exception['setup_cycles']) is not int or exception['setup_cycles'] < 2:
                        raise ConstraintError('multicycle setup_cycles must be >=2')
                    if exception.get('hold_cycles') != exception['setup_cycles'] - 1:
                        raise ConstraintError('multicycle hold_cycles must explicitly equal setup_cycles-1')
        if ports is not None:
            missing = {d: sorted(n for n, p in ports.items() if p['direction'] == d and n not in covered[d] and n not in clock_ports) for d in covered}
            if any(missing.values()):
                raise ConstraintError(f'CONSTRAINT_COVERAGE_INCOMPLETE: {missing}')
            if any(p['direction'] == 'inout' for p in ports.values()):
                raise ConstraintError('IO_CONSTRAINT_INVALID: bidirectional timing is not supported')
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(str(exc))
    return {'ok': not errors, 'errors': errors}


def render_sdc(clocks, constraints):
    clocks = [c.to_dict() if hasattr(c, "to_dict") else c for c in clocks]
    result = validate_model(clocks, constraints)
    if not result['ok']:
        raise ConstraintError('; '.join(result['errors']))
    lines = ['# Generated by nEBULA constraint_model.py; units: ns.',
             'proc nebula_objects {kind names} {',
             '  set result {}',
             '  foreach name $names {',
             '    set objects [get_$kind $name]',
             '    if {[llength $objects] == 0} {error "Unresolved timing object: $kind $name"}',
             '    foreach object $objects {lappend result $object}',
             '  }', '  return $result', '}']
    for clock in clocks:
        line = f'create_clock -name {token(clock["name"])} -period {clock["period_ns"]}'
        if clock.get('waveform_ns'):
            line += ' -waveform {' + ' '.join(str(v) for v in clock['waveform_ns']) + '}'
        if not clock.get('virtual'):
            line += ' ' + selector({'kind': 'ports', 'names': [clock['port']]})
        lines.append(line)
        lines.append(f'set_clock_uncertainty {clock.get("uncertainty_ns", 0)} ' + selector({'kind': 'clocks', 'names': [clock['name']]}))
    for clock in constraints.get('generated_clocks', []):
        lines.append(f'create_generated_clock -name {token(clock["name"])} -master_clock {token(clock["master_clock"])} -source {selector(clock["source"])} -divide_by {clock["divide_by"]} {selector(clock["target"])}')
    for groups in constraints.get('asynchronous_clock_groups', []):
        lines.append('set_clock_groups -asynchronous ' + ' '.join('-group ' + selector({'kind': 'clocks', 'names': group}) for group in groups))
    for direction in ('input', 'output'):
        for delay in constraints.get(direction + '_delays', []):
            for bound in ('min', 'max'):
                lines.append(f'set_{direction}_delay -{bound} {delay[bound + "_ns"]} -clock {token(delay["clock"])} ' + selector({'kind': 'ports', 'names': delay['ports']}))
    for kind in ('max_delays', 'false_paths', 'multicycle_paths'):
        for item in constraints.get(kind, []):
            endpoints = f'-from {selector(item["from"])} -to {selector(item["to"])}'
            if kind == 'max_delays':
                lines.append(f'set_max_delay {item["ns"]} {endpoints}')
            elif kind == 'false_paths':
                lines.append(f'set_false_path {endpoints}')
            else:
                lines.append(f'set_multicycle_path -setup {item["setup_cycles"]} {endpoints}')
                lines.append(f'set_multicycle_path -hold {item["hold_cycles"]} {endpoints}')
    return '\n'.join(lines) + '\n'


def verify_frozen_constraints(manifest, recipe):
    """Bind the checked manifest timing model to the exact frozen physical SDC."""
    files = [item for item in recipe.files if item.role == "constraints"]
    if len(files) != 1:
        raise ConstraintError("exactly one generated constraint file must be frozen")
    path = Path(files[0].logical_path)
    if not path.is_absolute():
        path = Path(recipe.source_base_dir) / path
    expected = render_sdc(manifest.clocks, manifest.constraints).encode("utf-8")
    if path.read_bytes() != expected:
        raise ConstraintError("frozen SDC does not match qualified manifest timing model; rebuild baseline")


def run_self_test() -> None:
    base_constraints = {
        'schema': 'nebula_constraints/1',
    }

    # 1. Valid single-clock model emits an exact braced clock name.
    clocks = [{'name': 'core', 'port': 'clk', 'period_ns': 2.0}]
    sdc = render_sdc(clocks, dict(base_constraints))
    assert 'create_clock -name {core} -period 2.0' in sdc

    # 2. Valid multi-clock model emits one create_clock per master and does
    #    not invent clock relationships.
    clocks2 = [
        {'name': 'core', 'port': 'clk', 'period_ns': 2.0},
        {'name': 'aux', 'port': 'aux_clk', 'period_ns': 4.0},
    ]
    sdc2 = render_sdc(clocks2, dict(base_constraints))
    assert sdc2.count('create_clock -name ') == 2
    assert 'set_clock_groups' not in sdc2

    # 3. Empty selector names fail closed before any Tcl is emitted.
    bad_selector = {
        'schema': 'nebula_constraints/1',
        'generated_clocks': [{
            'name': 'div2',
            'master_clock': 'core',
            'divide_by': 2,
            'source': {'kind': 'ports', 'names': []},
            'target': {'kind': 'pins', 'names': ['u_div/q']},
        }],
    }
    try:
        render_sdc(clocks, bad_selector)
    except ConstraintError:
        pass
    else:
        raise AssertionError('empty selector unexpectedly accepted')

    # 4. Tcl/wildcard-prone clock names fail closed.
    for bad_name in ('bad name', 'bad{name', 'bad}name', 'bad*name', 'bad$name', 'bad?name'):
        try:
            render_sdc([{'name': bad_name, 'port': 'clk', 'period_ns': 2.0}], dict(base_constraints))
        except ConstraintError:
            pass
        else:
            raise AssertionError(f'invalid clock name unexpectedly accepted: {bad_name!r}')

    # 5. Periods must be positive and finite.
    for bad_period in (-1.0, 0.0, float('nan'), float('inf')):
        try:
            render_sdc([{'name': 'core', 'port': 'clk', 'period_ns': bad_period}], dict(base_constraints))
        except ConstraintError:
            pass
        else:
            raise AssertionError(f'invalid period unexpectedly accepted: {bad_period!r}')

    # 6. Duplicate clock names fail closed.
    try:
        render_sdc([
            {'name': 'core', 'port': 'clk', 'period_ns': 2.0},
            {'name': 'core', 'port': 'clk2', 'period_ns': 3.0},
        ], dict(base_constraints))
    except ConstraintError:
        pass
    else:
        raise AssertionError('duplicate clock name unexpectedly accepted')

    print('constraint_model.py self-test: PASS')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--self-test', action='store_true', help='Run deterministic constraint-model regression tests.')
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
        return

    missing = []
    if args.manifest is None:
        missing.append('--manifest')
    if args.output is None:
        missing.append('--output')
    if missing:
        parser.error('the following arguments are required unless --self-test is used: ' + ', '.join(missing))

    manifest = json.loads(args.manifest.read_text())
    args.output.write_text(render_sdc(manifest.get('clocks', []), manifest.get('constraints', {})))


if __name__ == '__main__':
    main()
