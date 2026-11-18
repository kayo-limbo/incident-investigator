"""Frozen small DEVELOPMENT query-policy experiment; never a heldout benchmark."""
import argparse
import asyncio
import hashlib
import json
import math
import zipfile
from pathlib import Path

from backend.investigation.active_investigation import ActiveInvestigation
from backend.investigation.claim_verification import verify_claim
from backend.investigation.failure_evidence import investigate_503
from backend.investigation.active_policies import ActivePlanModel, ReactQueryModel, PolicyCancelled, investigate_policy, fixed_query, wrap_query
from backend.investigation.model import ModelConfig
from backend.investigation.paired_evidence import mask_sources
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.usage import Usage
from backend.investigation.budget import ObservedTokenBudget
from evals.collect_paired_windows import rebuild_pair, localization_expectation

ROOT = Path(__file__).resolve().parents[1]
CASES = ('stable', 'downstream', 'capacity', 'arrival', 'restart', 'compound')
LABELS = dict(stable='no_anomaly_observed', restart='no_anomaly_observed', downstream='downstream_change',
              capacity='capacity_change', arrival='arrival_change', compound='competing_changes')
BUDGET = dict(budget=6, timeout_seconds=120, max_total_tokens=40000)
VARIANTS = {'react': ('react', True), 'active': ('active', True), 'active_no_scoring': ('active', False)}


def dump(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sources():
    paths = list((ROOT / 'backend/investigation').glob('*.py'))
    paths += list((ROOT / 'lab').rglob('*.py'))
    paths += [Path(__file__), ROOT / 'evals/collect_paired_windows.py', ROOT / 'evals/paired_plan.py',
              ROOT / 'requirements.txt', ROOT / 'requirements-graph.txt']
    return {p.relative_to(ROOT).as_posix(): p.read_bytes() for p in sorted(paths)}


def settings(config):
    return {k: getattr(config, k) for k in ('base_url', 'model', 'timeout_seconds', 'max_output_tokens')}


def freeze(root, destination, config):
    source_plan = json.loads((root / 'plan.private.json').read_text(encoding='utf-8'))
    raw = (root / 'collection.private.json').read_bytes()
    manifest = json.loads(raw)
    if source_plan['split'] != 'development' or not manifest['completed'] or not manifest['recovery']['verified']:
        raise ValueError('development_collection_required')
    source_store = SnapshotStore(root / 'snapshots')
    samples, payloads = [], {}
    mixed = source_plan.get('input_plan', {}).get('localization') == 'mixed_slowest'
    for name in (('downstream',) if mixed else CASES):
        entries = [c for c in manifest['cases'] if c['name'] == name and c['repeat'] == 0]
        if len(entries) != 1 or not entries[0]['valid'] or not entries[0]['feasibility']['ready']:
            raise ValueError('preselected_case_unavailable')
        case = entries[0]
        rebuilt = rebuild_pair(case['windows'], source_plan['limits']).model_dump(mode='json')
        if rebuilt != source_store.read(case['snapshot_id']):
            raise ValueError('source_provenance_mismatch')
        views = {'complete': case['snapshot_id']}
        if name == 'compound':
            views.update(case['source_ablations'])
            if set(views) != {'complete', 'trace_only', 'runtime_only'}:
                raise ValueError('invalid_ablation_membership')
        for view, key in views.items():
            payload = source_store.read(key)
            expected_payload = rebuilt if view == 'complete' else mask_sources(rebuilt,
                trace=view != 'runtime_only', runtime=view != 'trace_only')
            if payload != expected_payload:
                raise ValueError('ablation_provenance_mismatch')
            samples.append(dict(name=name, view=view, snapshot_id=key,
                expected=LABELS[name] if view == 'complete' else 'insufficient_evidence'))
            if mixed:
                expectation = localization_expectation(case['windows'], rebuild_pair(case['windows'], source_plan['limits']))
                if expectation['stage'] != 'mixed':
                    raise ValueError('mixed_localization_not_observed')
                samples[-1]['expected_localization'] = expectation
            payloads[key] = payload
    destination.mkdir(parents=True, exist_ok=False)
    store = SnapshotStore(destination / 'snapshots')
    for key, payload in payloads.items():
        if store.freeze(payload) != key:
            raise ValueError('snapshot_hash_mismatch')
    source = sources()
    plan = dict(protocol='active-comparison-development-v1', split='development', rounds=1,
        suite='mixed-localization' if mixed else 'original',
        samples=samples, variants=VARIANTS, budget=BUDGET, model=settings(config),
        source_manifest_sha256=digest(raw), source_plan_sha256=digest((root / 'plan.private.json').read_bytes()),
        source_hashes={p: digest(b) for p, b in source.items()})
    dump(destination / 'plan.private.json', plan)
    (destination / 'plan.sha256').write_text(digest((destination / 'plan.private.json').read_bytes()), encoding='ascii')
    with zipfile.ZipFile(destination / 'protocol-sources.zip', 'x') as archive:
        for name, content in source.items():
            archive.writestr(name, content)
    return validate(destination, config)


def validate(directory, config=None):
    raw = (directory / 'plan.private.json').read_bytes()
    if digest(raw) != (directory / 'plan.sha256').read_text(encoding='ascii'):
        raise ValueError('plan_digest_mismatch')
    plan = json.loads(raw)
    if (plan['protocol'] != 'active-comparison-development-v1' or plan['split'] != 'development' or
            plan['rounds'] != 1 or plan['budget'] != BUDGET or
            plan['variants'] != {k: list(v) for k, v in VARIANTS.items()} or
            plan['source_hashes'] != {p: digest(b) for p, b in sources().items()} or
            (config is not None and plan['model'] != settings(config))):
        raise ValueError('frozen_protocol_changed')
    suite = plan.get('suite', 'original')
    if suite not in ('original', 'mixed-localization'):
        raise ValueError('unknown_suite')
    membership = [('downstream', 'complete')] if suite == 'mixed-localization' else (
        [(n, 'complete') for n in CASES] + [('compound', 'trace_only'), ('compound', 'runtime_only')])
    if [(s['name'], s['view']) for s in plan['samples']] != membership:
        raise ValueError('sample_membership_changed')
    store = SnapshotStore(directory / 'snapshots')
    for sample in plan['samples']:
        expected = LABELS[sample['name']] if sample['view'] == 'complete' else 'insufficient_evidence'
        if sample['expected'] != expected:
            raise ValueError('expected_label_changed')
        if suite == 'mixed-localization' and sample.get('expected_localization', {}).get('stage') != 'mixed':
            raise ValueError('missing_localization_expectation')
        ActiveInvestigation(store, sample['snapshot_id'], BUDGET['budget'])
    with zipfile.ZipFile(directory / 'protocol-sources.zip') as archive:
        if {p: digest(archive.read(p)) for p in archive.namelist()} != plan['source_hashes']:
            raise ValueError('archive_mismatch')
    return plan


def score(report, sample, store, variant):
    """Reconstruct accepted evidence/state; labels are evaluator-only, not a semantic judge."""
    strategy, scoring = ('fixed', True) if variant == 'fixed' else VARIANTS[variant]
    if (report['protocol'] != 'active-query-policies-v3' or report['snapshot_id'] != sample['snapshot_id'] or
            report['strategy'] != strategy or report['scoring'] != (scoring if strategy == 'active' else None)):
        raise ValueError('report_identity_mismatch')
    run = ActiveInvestigation(store, sample['snapshot_id'], BUDGET['budget'])
    origin = 'fixed_policy' if strategy == 'fixed' else 'model' if strategy == 'active' else 'executor_from_query'
    for step in report['history']:
        if strategy == 'fixed' and step['plan'] != wrap_query(fixed_query(run)).model_dump():
            raise ValueError('fixed_order_mismatch')
        run.execute(step['plan'])
        expected = {**run.history[-1], 'plan_origin': origin}
        if step != expected:
            raise ValueError('history_mismatch')
    if report['state'] != run.state() or report['evidence'] != run.evidence or report['tool_calls'] != len(run.evidence):
        raise ValueError('evidence_state_mismatch')
    usage = Usage.model_validate(report['model_usage'])
    if (len(usage.calls) != report['model_calls'] or any(c.kind != 'model' for c in usage.calls) or
            not 0 <= report['model_calls'] <= 5 or (strategy == 'fixed' and report['model_calls'])):
        raise ValueError('model_usage_mismatch')
    if strategy != 'fixed' and (not len(run.history) <= report['model_calls'] <= len(run.history) + 1 or
            (report['termination'] == 'completed' and report['model_calls'] != len(run.history))):
        raise ValueError('decision_count_mismatch')
    if report['token_budget'] != ObservedTokenBudget(BUDGET['max_total_tokens']).snapshot(usage).model_dump():
        raise ValueError('token_accounting_mismatch')
    accepted = report['termination'] == 'completed' and run.state()['stop'] != 'continue' and run.state()['code'] == sample['expected']
    expected_local = sample.get('expected_localization')
    localization_accepted = None if expected_local is None else (
        run.state()['localization']['status'] == 'observed' and
        all(run.state()['localization'].get(k) == v for k, v in expected_local.items()))
    accepted = accepted and localization_accepted is not False
    return dict(accepted=accepted, termination=report['termination'], stop=run.state()['stop'],
        sufficiency=run.state()['sufficiency'],
        localization=run.state()['localization'],
        localization_accepted=localization_accepted,
        tool_calls=len(run.evidence), model_calls=report['model_calls'],
        total_tokens=usage.totals()['total_tokens'], unknown_usage_calls=usage.totals()['unknown_usage_calls'],
        elapsed_ms=usage.elapsed_ms, repeated_query_rejected=report['failure'] == 'repeated_query',
        invalid_action=report['termination'] == 'invalid_action')


async def evaluate(directory, config=None):
    plan = validate(directory, config)
    variants = list(VARIANTS) if config is not None else ['fixed']
    store = SnapshotStore(directory / 'snapshots')
    reports = directory / ('model-reports' if config is not None else 'fixed-reports')
    reports.mkdir(exist_ok=True)
    for index, sample in enumerate(plan['samples']):
        for variant in variants:
            validate(directory, config)
            name = f'{index}-{variant}'
            output, started = reports / (name + '.json'), reports / (name + '.started.json')
            if output.exists():
                saved = json.loads(output.read_text(encoding='utf-8'))
                score(saved, sample, store, variant)
                continue  # Completed failures are never retried either.
            dump(started, dict(snapshot_id=sample['snapshot_id'], variant=variant))
            # Existing started marker without report raises FileExistsError: billing is uncertain.
            if config is None:
                report = await investigate_policy(store, sample['snapshot_id'], **BUDGET)
            else:
                strategy, scoring = VARIANTS[variant]
                adapter = ReactQueryModel if strategy == 'react' else ActivePlanModel
                try:
                    async with adapter(config) as model:
                        report = await investigate_policy(store, sample['snapshot_id'], model,
                            strategy=strategy, scoring=scoring, **BUDGET)
                except PolicyCancelled as exc:
                    dump(output, exc.report)
                    raise
            dump(output, report)
            score(report, sample, store, variant)
    return summarize(directory)


def summarize(directory):
    plan = validate(directory)
    store, rows = SnapshotStore(directory / 'snapshots'), []
    for folder, variants in (('fixed-reports', ['fixed']), ('model-reports', list(VARIANTS))):
        for i, sample in enumerate(plan['samples']):
            for variant in variants:
                path = directory / folder / f'{i}-{variant}.json'
                marker = directory / folder / f'{i}-{variant}.started.json'
                row = dict(sample=i, name=sample['name'], view=sample['view'], variant=variant)
                if path.exists():
                    row.update(status='reported', **score(json.loads(path.read_text(encoding='utf-8')), sample, store, variant))
                else:
                    row['status'] = 'unknown_interrupted' if marker.exists() else 'not_started'
                rows.append(row)
    return dict(protocol=plan['protocol'], rows=rows, limitations='开发集查询策略协议验收；不是模型自主诊断或泛化成绩。')


def freeze_claims(root, destination):
    """Preselect repeat=1 of the six original DEVELOPMENT collections.

    Claims are constructed mechanism probes, not natural model outputs. Labels
    are computed by independent sorting, never by the verifier under evaluation.
    """
    raw = (root / 'collection.private.json').read_bytes()
    source_plan = json.loads((root / 'plan.private.json').read_text(encoding='utf-8'))
    manifest = json.loads(raw)
    if source_plan['split'] != 'development' or not manifest['completed'] or not manifest['recovery']['verified']:
        raise ValueError('development_collection_required')
    source_store = SnapshotStore(root / 'snapshots')
    samples, payloads = [], {}
    for name in CASES:
        entries = [c for c in manifest['cases'] if c['name'] == name and c['repeat'] == 1]
        if len(entries) != 1 or not entries[0]['valid'] or not entries[0]['feasibility']['ready']:
            raise ValueError('preselected_case_unavailable')
        case = entries[0]
        payload = rebuild_pair(case['windows'], source_plan['limits']).model_dump(mode='json')
        key = case['snapshot_id']
        if payload != source_store.read(key):
            raise ValueError('source_provenance_mismatch')
        claims = []
        for metric in ('orders_ms', 'inventory_ms', 'pre_send_ms'):
            values = sorted(r[metric] for r in payload['incident']['trace']['requests'] if r[metric] is not None)
            if len(values) < 5 or payload['incident']['trace']['quality'] != 'observed':
                raise ValueError('statistics_not_observed')
            expected = values[math.ceil(.95 * len(values)) - 1]
            base = dict(snapshot_id=key, evidence_id='e1', window='incident', metric=metric,
                        statistic='p95', population='observed_non_null', method='nearest_rank',
                        value=expected, unit='ms')
            for kind, change, label in (
                ('correct', {}, 'supported'),
                ('wrong_value', {'value': expected + 1.0}, 'contradicted'),
                ('population_overreach', {'population': 'all_traffic'}, 'insufficient_evidence')):
                claims.append(dict(id=metric + ':' + kind, origin='constructed', claim={**base, **change},
                                   expected_status=label, oracle_value=expected))
        samples.append(dict(name=name, repeat=1, snapshot_id=key, claims=claims))
        payloads[key] = payload
    destination.mkdir(parents=True, exist_ok=False)
    store = SnapshotStore(destination / 'snapshots')
    for key, payload in payloads.items():
        assert store.freeze(payload) == key
    source = sources()
    plan = dict(protocol='active-claims-development-v1', split='development', samples=samples,
                variants=['reference_only', 'statistical'], budget=BUDGET,
                source_manifest_sha256=digest(raw),
                source_plan_sha256=digest((root / 'plan.private.json').read_bytes()),
                source_hashes={p: digest(b) for p, b in source.items()},
                limitations='Constructed probes on previously inspected development collections; no heldout or model-quality claim.')
    dump(destination / 'plan.private.json', plan)
    (destination / 'plan.sha256').write_text(digest((destination / 'plan.private.json').read_bytes()), encoding='ascii')
    with zipfile.ZipFile(destination / 'protocol-sources.zip', 'x') as archive:
        for name, content in source.items():
            archive.writestr(name, content)
    return dict(protocol=plan['protocol'], collections=len(samples), claims=sum(len(s['claims']) for s in samples))


def compare_claims(directory, *, audit=False):
    raw = (directory / 'plan.private.json').read_bytes()
    if digest(raw) != (directory / 'plan.sha256').read_text(encoding='ascii'):
        raise ValueError('plan_digest_mismatch')
    plan = json.loads(raw)
    if (plan['protocol'] != 'active-claims-development-v1' or plan['split'] != 'development' or
            plan['budget'] != BUDGET or plan['variants'] != ['reference_only', 'statistical'] or
            [(s['name'], s['repeat']) for s in plan['samples']] != [(n, 1) for n in CASES] or
            plan['source_hashes'] != {p: digest(b) for p, b in sources().items()}):
        raise ValueError('frozen_protocol_changed')
    with zipfile.ZipFile(directory / 'protocol-sources.zip') as archive:
        if {p: digest(archive.read(p)) for p in archive.namelist()} != plan['source_hashes']:
            raise ValueError('archive_mismatch')
    store, rows = SnapshotStore(directory / 'snapshots'), []
    for sample in plan['samples']:
        run = ActiveInvestigation(store, sample['snapshot_id'], BUDGET['budget'])
        run.execute(wrap_query(dict(tool='get_window_summary', window='incident')))
        before = run.report()
        for item in sample['claims']:
            claim = item['claim']
            # A deliberately limited reference-validity baseline: checks acquired
            # source, window and field existence, not numeric entailment/population.
            refs = [e for e in run.evidence if e['id'] == claim['evidence_id']]
            linked = (claim['snapshot_id'] == run.snapshot_id and len(refs) == 1 and
                      refs[0].get('query', {}).get('window') == claim['window'] and
                      claim['metric'] in (refs[0]['data'].get('statistics') or {}))
            checked = verify_claim(claim, snapshot_id=run.snapshot_id, evidence=run.evidence)
            for variant, accepted in (('reference_only', linked), ('statistical', checked['status'] == 'supported')):
                rows.append(dict(sample=sample['name'], claim_id=item['id'], origin=item['origin'],
                                 expected_status=item['expected_status'], variant=variant, accepted=accepted,
                                 verification=checked if variant == 'statistical' else None))
        if run.report() != before:
            raise ValueError('verification_mutated_state')
    metrics = {}
    for variant in plan['variants']:
        subset = [r for r in rows if r['variant'] == variant]
        good = [r for r in subset if r['expected_status'] == 'supported']
        bad = [r for r in subset if r['expected_status'] == 'contradicted']
        unknown = [r for r in subset if r['expected_status'] == 'insufficient_evidence']
        metrics[variant] = dict(
            false_accepts=dict(count=sum(r['accepted'] for r in bad), total=len(bad)),
            false_rejections=dict(count=sum(not r['accepted'] for r in good), total=len(good)),
            unsupported_accepts=dict(count=sum(r['accepted'] for r in unknown), total=len(unknown)),
            supported_answers=dict(count=sum(r['accepted'] for r in good), total=len(good)),
            answered_claims=dict(count=sum(r['accepted'] for r in subset), total=len(subset)),
            model_calls=0, additional_tool_calls=0)
    result = dict(protocol=plan['protocol'], plan_sha256=digest(raw), rows=rows, metrics=metrics,
                  collections=len(plan['samples']), shared_acquisition_tool_calls=2 * len(plan['samples']),
                  limitations=plan['limitations'])
    output = directory / 'claims-result.private.json'
    if audit:
        if json.loads(output.read_text(encoding='utf-8')) != result:
            raise ValueError('claim_result_mismatch')
    else:
        dump(output, result)
    return dict(protocol=result['protocol'], collections=result['collections'], metrics=metrics, audited=audit)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('freeze', 'fixed', 'models', 'audit', 'freeze-claims', 'claims', 'audit-claims',
                                         'compare-503'))
    parser.add_argument('directory', type=Path)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--snapshot-id', action='append', help='compare-503：每个采集一次，可重复指定')
    parser.add_argument('--authorized-model-calls', action='store_true', help='仅在用户明确授权本批观测外发与费用后使用')
    args = parser.parse_args()
    if args.mode == 'compare-503':
        ids = args.snapshot_id or []
        if not ids or len(set(ids)) != len(ids) or not args.directory.is_dir():
            parser.error('compare-503 requires an existing store and distinct --snapshot-id values')
        store = SnapshotStore(args.directory)
        result = {}
        for variant, by_status in [('slowest', False), ('http_503', True)]:
            reports = [investigate_503(store, key, budget=2, by_status=by_status) for key in ids]
            result[variant] = dict(reports=reports, supported=sum(
                r['code'] == 'inspect_orders_connection_pool' for r in reports),
                total=len(reports), tool_calls=sum(r['tool_calls'] for r in reports))
    elif args.mode == 'freeze-claims':
        if args.source is None:
            parser.error('freeze-claims requires --source')
        result = freeze_claims(args.source, args.directory)
    elif args.mode in ('claims', 'audit-claims'):
        result = compare_claims(args.directory, audit=args.mode == 'audit-claims')
    elif args.mode == 'freeze':
        if args.source is None:
            parser.error('freeze requires --source')
        result = freeze(args.source, args.directory, ModelConfig.from_env())
    elif args.mode == 'models':
        if not args.authorized_model_calls:
            parser.error('models requires explicit batch authorization')
        result = asyncio.run(evaluate(args.directory, ModelConfig.from_env()))
    elif args.mode == 'fixed':
        result = asyncio.run(evaluate(args.directory))
    else:
        result = summarize(args.directory)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
