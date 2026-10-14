import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from pathlib import Path

from prism import entire, github_context, models, pipeline, results, tracing

ROOT = Path('/Users/bhavya/.ao/data/worktrees/prisme/prisme-50')
OUT = ROOT / 'data/results/entire-benchmark.json'
github_context.CACHE = Path('/private/tmp/prisme-entire-gh-cache')
CAP = {'claude': 2.0, 'openai': 2.0}
PLANS = [
    ('flask', 'pallets/flask', 'ad68a12645d96fe51558da13e18544ba458d7af0', [(6133,3193),(6096,6093),(5962,5961),(5917,5916),(5899,5816),(5898,5895)]),
    ('requests', 'psf/requests', '70298332899f25826e35e42f8d83425124f755a5', [(7433,7432),(7427,4795),(7376,7375),(7310,6810),(7244,5436),(7220,7219)]),
    ('click', 'pallets/click', '047adef258fc25566163ffc3efd14effc0ef7352', [(3818,3802),(3781,2869),(3780,2582),(3769,3571),(3819,3135),(3820,1054)]),
    ('pytest', 'pytest-dev/pytest', '3b17fa79f8ad28bbda7aa468a750605be9c24553', [(15150,12223),(15149,12175),(15058,6750),(15053,15052),(15044,15043),(15040,11783)]),
    ('pydantic', 'pydantic/pydantic', 'f512b087202f58ec90a9d38c9102858567d72440', [(13980,13971),(13978,13976),(13972,13965),(13967,13919),(13964,13957),(13951,13950)]),
]


def credentials():
    env = Path('/Users/bhavya/Developer/side-projects/hakathons/Prisme/.env')
    for line in env.read_text().splitlines():
        key, sep, value = line.partition('=')
        if sep and key in ('NEATLOGS_API_KEY', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'GITHUB_TOKEN'):
            os.environ.setdefault(key, value.strip().strip('"\''))
    os.environ['ENTIRE_TOKEN'] = Path('/private/tmp/entire-token').read_text().strip()
    os.environ.pop('PRISM_ENTIRE_TRANSPORT', None)
    assert all(os.getenv(key) for key in ('NEATLOGS_API_KEY', 'ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'ENTIRE_TOKEN'))


def check_snapshots():
    for name, _, sha, _ in PLANS:
        repo = f'gh/prism-benchmarks/{name}'
        remote = github_context.api_v2(f'repos/prism-benchmarks/{name}/commits/main')['sha']
        assert remote == sha, f'{repo} main moved from snapshot'
        entire.ensure_ready(repo)


def prepare():
    cases = []
    for name, upstream, sha, pairs in PLANS:
        root = 'repos/' + upstream
        paths, _ = github_context._tree(github_context.api_v2, root, sha)
        for pr_num, issue_num in pairs:
            pr = github_context.api_v2(f'{root}/pulls/{pr_num}')
            issue = github_context.api_v2(f'{root}/issues/{issue_num}')
            before = github_context._parent_sha(github_context.api_v2, root, pr)
            assert pr['merged_at'] and issue['state'] == 'closed'
            assert issue['created_at'] <= pr['created_at'] and pr['created_at'] > '2026-01-01'
            truth = github_context.true_files(pr['html_url'], paths)
            assert truth, f'no changed existing files for {upstream}#{issue_num}'
            cases.append({'id': f'{name}#{issue_num}', 'issue_url': issue['html_url'],
                          'pr_url': pr['html_url'], 'snapshot_repo': f'https://github.com/prism-benchmarks/{name}',
                          'entire_repo': f'gh/prism-benchmarks/{name}', 'snapshot_sha': sha,
                          'pr_pre_merge_sha': before, 'actual_files': sorted(truth), 'results': {}})
    assert len(cases) == 30
    return cases


def save(run):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    results.write_json(OUT, run)


def context(case):
    ctx = github_context.build_context(case['issue_url'], entire_repo=case['entire_repo'],
                                       pr_url=case['pr_url'], pre_merge_sha=case['pr_pre_merge_sha'])
    owner_repo = case['issue_url'].split('/issues/')[0].removeprefix('https://github.com/')
    paths, repo_map = github_context._tree(github_context.api_v2, 'repos/'+owner_repo, case['snapshot_sha'])
    assert set(case['actual_files']) <= paths
    ctx.known_paths = paths
    ctx.bundle.repo_map = repo_map
    ctx.bundle.path_hints = [p for p in ctx.bundle.path_hints if p in paths]
    ctx.search_commit = None  # Entire index is the frozen snapshot; no git grep or checkout.
    ctx.search_checkout = None
    ctx.search_source = 'entire'
    assert ctx.target_pr == int(case['pr_url'].rsplit('/',1)[1])
    return ctx


def trace_id(span):
    value = getattr(getattr(span, 'get_span_context', lambda: None)(), 'trace_id', None)
    return format(value, '032x') if value is not None else None


def readback(tid):
    url = f'https://ingest.neatlogs.com/api/traces/v3/{tid}'
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        req = urllib.request.Request(url, headers={'x-api-key': os.environ['NEATLOGS_API_KEY']})
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                data = json.load(response)
            found = data.get('_id') or data.get('trace_id') or data.get('traceId')
            if found == tid:
                return True
        except urllib.error.HTTPError as error:
            if error.code not in (404, 202):
                raise RuntimeError(f'NeatLogs readback HTTP {error.code}') from None
        time.sleep(2)
    return False


async def invoke(ctx, provider, ledger):
    original = models.call
    async def limited(which, stage, prompt, schema, max_output_tokens=models.MAX_TOKENS, system=models.SYSTEM):
        assert which == provider
        # Input tokens cannot exceed UTF-8 bytes; reserve 10k for API framing and schema.
        input_ceiling = len(prompt.encode()) + len(system.encode()) + len(json.dumps(schema).encode()) + 10_000
        call_ceiling = (input_ceiling * 2.5 + max_output_tokens * 10) / 1_000_000
        if ledger[which] + call_ceiling > CAP[which]:
            raise RuntimeError(f'{which} stopped before call: conservative bound would cross ${CAP[which]:.2f} cap')
        try:
            answer, usage = await original(which, stage, prompt, schema, max_output_tokens, system)
        except models.BilledError as error:
            ledger[which] += models.estimate_cost(which, error.usage)
            raise
        except Exception:
            ledger[which] = None  # Unknown billed usage: stop this provider.
            raise
        ledger[which] += models.estimate_cost(which, usage)
        assert ledger[which] <= CAP[which], f'{which} cap exceeded'
        return answer, usage
    models.call = limited
    try:
        return await pipeline.predict(ctx, provider, max_extra=1)
    finally:
        models.call = original
        await models.close()


def one(run, case, provider, result_key="results"):
    ledger = run['estimated_spend_usd']
    if ledger[provider] is None:
        return
    ctx = context(case)
    tracing.init()
    prediction = None
    err = None
    tid = None
    try:
        with tracing.workflow('entire-snapshot-issue-to-files') as span:
            tid = trace_id(span)
            try:
                prediction = asyncio.run(invoke(ctx, provider, ledger))
            except Exception as error:
                err = error
    finally:
        tracing.shutdown()
    if prediction is not None:
        record = asdict(prediction)
        actual = set(case['actual_files'])
        found = set(prediction.predicted)
        record.update({'precision': len(found & actual)/len(found) if found else 0,
                       'recall': len(found & actual)/len(actual), 'exact_match': found == actual,
                       'neatlogs_trace_id': tid})
        record['discovery']['snapshot_commit'] = case['snapshot_sha']
        assert record['discovery']['source'] == 'entire' and record['discovery']['command'] == 'entire search --code'
        assert tid and len(tid) == 32
    else:
        record = {'status': 'failed', 'error': results.error_details(err), 'neatlogs_trace_id': tid}
    case.setdefault(result_key, {})[provider] = record
    save(run)
    print(case['id'], provider, 'recall='+str(record.get('recall')), 'cost='+str(record.get('estimated_cost_usd')),
          'source='+str((record.get('discovery') or {}).get('source')), 'trace='+str(tid),
          'cumulative='+str(ledger[provider]), flush=True)
    if err:
        raise err
    return tid


def main():
    credentials()
    check_snapshots()
    if OUT.exists():
        run = json.loads(OUT.read_text())
    else:
        run = {'date': '2026-10-14', 'source_commit': 'e043046584a97b38c7618ba9fbb46c8f2ff3c3c9',
               'method': 'Entire code search on one frozen default-branch snapshot per project; issue context cut off at fix PR creation; ground truth is modified existing files at snapshot',
               'cost_basis': 'token-based list-price estimate, not provider invoice',
               'caps_usd': CAP, 'estimated_spend_usd': {'claude': 0.0, 'openai': 0.0},
               'cases': prepare()}
        save(run)
    phase = sys.argv[1]
    if phase == 'trial':
        case = run['cases'][0]
        for provider in ('claude','openai'):
            if provider not in case['results']:
                tid = one(run, case, provider)
                if not readback(tid):
                    raise RuntimeError(f'NeatLogs trace {tid} did not arrive; stopped')
                print('NeatLogs readback confirmed',tid,flush=True)
    elif phase == 'full':
        for case in run['cases']:
            for provider in ('claude','openai'):
                if provider not in case['results'] and run['estimated_spend_usd'][provider] is not None:
                    one(run,case,provider)
    elif phase == 'repeat2':
        assert all(all(p in c['results'] and c['results'][p]['discovery']['source'] == 'entire'
                       for p in ('claude','openai')) for c in run['cases'])
        for case in run['cases']:
            for provider in ('claude','openai'):
                if provider not in case.setdefault('repeat_2', {}) and run['estimated_spend_usd'][provider] is not None:
                    one(run,case,provider,'repeat_2')
    else:
        raise ValueError(phase)

if __name__ == '__main__':
    main()
