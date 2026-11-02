"""Offline recovery from original captures only; never performs model dispatch."""
import hashlib
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path('/home/just_dak/projects/relaymind')
RUN = Path('/home/just_dak/relaymind-automated-277/run')
sys.path.insert(0, str(ROOT/'src'))
spec = importlib.util.spec_from_file_location('recovery_run', ROOT/'evaluation/assets/automated-host-277/run.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from relaymind.canonical import canonical_json
from relaymind.evaluation.asset_host_runner import _admit
from relaymind.evaluation.asset_run_journal import RunJournal
from relaymind.evaluation.asset_whole_task import plan_host_trial
from relaymind.recommendations.private_files import create_private_file_exclusive

os.umask(0o077)
read = lambda p: json.loads(p.read_bytes())
original_journal = (RUN/'journal.jsonl').read_bytes()
entries = [json.loads(line) for line in original_journal.splitlines()]
assert len(entries) == 2 and entries[1] == {'type':'started','key':'1'}
assert not (RUN/'journal.jsonl.lock').exists()
key = 'h001-native'
root = RUN/'evidence'
rowroot = root/'host-runs'/key
host = read(rowroot/'host-result.json')
dispatch = read(rowroot/'host-dispatch.json')
assert dispatch['task_id'] == 'h001' and dispatch['arm'] == 'native'
assert host['status'] == 'failed' and host['exit_code'] == 2
ledger = read(RUN/'ledger.json')
assert ledger['rows'][key] == {'state':'reserved','amount':250000}
refs = []
def reference(path, identity, provenance='observed'):
    payload=path.read_bytes()
    ref={'id':identity,'path':path.relative_to(root).as_posix(),'sha256':hashlib.sha256(payload).hexdigest(),'bytes':len(payload),'provenance':provenance}
    refs.append(ref)
    return ref
for path in sorted(rowroot.glob('*.json')):
    reference(path, f'host-dispatch:{key}' if path.name=='host-dispatch.json' else f'{key}:{path.name}')
folders=sorted((root/'upstream-originals').iterdir(),key=lambda p:read(p/'started.json')['started_at'])
assert len(folders)==4
calls=[]
for n, folder in enumerate(folders,1):
    result=read(folder/'result.json')
    assert dispatch['started_at'] <= result['started_at'] <= result['ended_at'] <= host['ended_at']
    calls.append(dict(result, response_status=result['status'],request_body=(folder/'request.bin').read_text(),response_body=(folder/'response.bin').read_text()))
    for name in ('request.bin','response.bin','started.json','result.json'):
        reference(folder/name,f'{key}:upstream-{n}:{name}')
recovery_id=f'{key}:offline-recovery.json'
attempts=module.AutomatedRun.gate_attempts(None,key,calls,recovery_id,{'model':'glm-5.3-flash'})
for n, attempt in enumerate(attempts,1):
    attempt['evidence_ids'].append(f'{key}:upstream-{n}:response.bin')
row=dict(task_id='h001',arm='native',status='failed',success=None,attempts=attempts,
    elapsed_ms=float(host['elapsed_ms']),active_ms=None,human_wait_ms=None,interactions=0,local_turns=0,
    cpu_ms=None,peak_bytes=None,recommendation_ids=[],host_session_id=host['report'].get('sessionId'),
    evidence_ids=sorted(ref['id'] for ref in refs),reason='host_failed')
recovery_root=root/'offline-recovery'/key
recovery_root.mkdir(parents=True,mode=0o700)
assert create_private_file_exclusive(recovery_root/'journal-before.jsonl',original_journal)
reference(recovery_root/'journal-before.jsonl',f'{key}:journal-before.jsonl')
record=dict(schema_version=1,kind='offline_row_recovery',recovered_at=datetime.now(timezone.utc).isoformat(),
    original_execution_commit='8f796b1b788457237e40125fe28be51b39bfc44d',
    cause='gated-calls aggregation exceeded the credential writer 1MiB bound after originals were durable',
    methodology='Recovered the native host failure and four generation attempts from original host-result and exact upstream request/response/result files. No request replayed.',
    budget='Original full 250000-token reservation remains charged; two missing upstream usages stay unknown. Ledger is unchanged.',
    limitations=['Original aggregate gated-calls file was never published; this is a later reconstruction, not that original.',
                 'Elapsed time is the original host elapsed_ms; evidence serialization failure is not model latency.'],
    source_files=[dict(ref) for ref in refs],recovered_row=row)
payload=(canonical_json(record)+'\n').encode()
assert create_private_file_exclusive(recovery_root/'recovery.json',payload)
reference(recovery_root/'recovery.json',recovery_id,'reported')
row['evidence_ids']=sorted(ref['id'] for ref in refs)
tasks=read(ROOT/'evaluation/assets/automated-host-277/host-tasks.json')
freeze=read(ROOT/'evaluation/assets/automated-host-277/host-freeze.json')
planned=plan_host_trial(tasks)[0]
config=next(c for c in freeze['configurations'] if c['arm']=='native')
row,refs=_admit(row,sorted(refs,key=lambda r:r['id']),planned,config,set(),{})
header={k:v for k,v in entries[0].items() if k!='type'}
with RunJournal(RUN/'journal.jsonl',header) as journal:
    assert journal.started=={'1'} and not journal.finished
    journal.finish('1',dict(row=row,evidence=refs))
assert read(RUN/'ledger.json')==ledger
print(json.dumps({'recovered_row':key,'status':row['status'],'attempts':len(attempts),'known_reported_tokens':sum((a['total_tokens'] or 0) for a in attempts),'unknown_usage_attempts':sum(a['total_tokens'] is None for a in attempts),'budget_reservation_retained':250000,'requests_replayed':0}))
