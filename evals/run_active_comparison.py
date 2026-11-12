"""Frozen small DEVELOPMENT query-policy experiment; never a heldout benchmark."""
import argparse
import asyncio
import hashlib
import json
import zipfile
from pathlib import Path

from backend.investigation.active_investigation import ActiveInvestigation
from backend.investigation.active_policies import ActivePlanModel, ReactQueryModel, PolicyCancelled, investigate_policy, ORDER
from backend.investigation.model import ModelConfig
from backend.investigation.paired_evidence import mask_sources
from backend.investigation.snapshot_store import SnapshotStore
from backend.investigation.usage import Usage
from backend.investigation.budget import ObservedTokenBudget
from evals.collect_paired_windows import rebuild_pair

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
    for name in CASES:
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
            payloads[key] = payload
    destination.mkdir(parents=True, exist_ok=False)
    store = SnapshotStore(destination / 'snapshots')
    for key, payload in payloads.items():
        if store.freeze(payload) != key:
            raise ValueError('snapshot_hash_mismatch')
    source = sources()
    plan = dict(protocol='active-comparison-development-v1', split='development', rounds=1,
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
    membership = [(n, 'complete') for n in CASES] + [('compound', 'trace_only'), ('compound', 'runtime_only')]
    if [(s['name'], s['view']) for s in plan['samples']] != membership:
        raise ValueError('sample_membership_changed')
    store = SnapshotStore(directory / 'snapshots')
    for sample in plan['samples']:
        expected = LABELS[sample['name']] if sample['view'] == 'complete' else 'insufficient_evidence'
        if sample['expected'] != expected:
            raise ValueError('expected_label_changed')
        ActiveInvestigation(store, sample['snapshot_id'], BUDGET['budget'])
    with zipfile.ZipFile(directory / 'protocol-sources.zip') as archive:
        if {p: digest(archive.read(p)) for p in archive.namelist()} != plan['source_hashes']:
            raise ValueError('archive_mismatch')
    return plan


def score(report, sample, store, variant):
    """Reconstruct accepted evidence/state; labels are evaluator-only, not a semantic judge."""
    strategy, scoring = ('fixed', True) if variant == 'fixed' else VARIANTS[variant]
    if (report['protocol'] != 'active-query-policies-v1' or report['snapshot_id'] != sample['snapshot_id'] or
            report['strategy'] != strategy or report['scoring'] != (scoring if strategy == 'active' else None)):
        raise ValueError('report_identity_mismatch')
    run = ActiveInvestigation(store, sample['snapshot_id'], BUDGET['budget'])
    origin = 'fixed_policy' if strategy == 'fixed' else 'model' if strategy == 'active' else 'executor_from_query'
    for step in report['history']:
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
    if strategy == 'fixed' and [h['plan']['gap'] for h in run.history] != ORDER[:len(run.history)]:
        raise ValueError('fixed_order_mismatch')
    if report['token_budget'] != ObservedTokenBudget(BUDGET['max_total_tokens']).snapshot(usage).model_dump():
        raise ValueError('token_accounting_mismatch')
    accepted = report['termination'] == 'completed' and run.state()['stop'] != 'continue' and run.state()['code'] == sample['expected']
    return dict(accepted=accepted, termination=report['termination'], stop=run.state()['stop'],
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('freeze', 'fixed', 'models', 'audit'))
    parser.add_argument('directory', type=Path)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--authorized-model-calls', action='store_true', help='仅在用户明确授权本批观测外发与费用后使用')
    args = parser.parse_args()
    if args.mode == 'freeze':
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
