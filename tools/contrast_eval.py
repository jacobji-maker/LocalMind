"""Freeze actual messages for fair local-model contrasts, then score raw runs.

Preparation uses fixture assistant outputs (explicitly synthetic) and actual MCP
subprocesses. Only the separate inference runner calls a real model. No expected
answer is placed in a model request. Run prepare once before reading outputs.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random
import selectors
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from localmind.adapters import ReplayAdapter
from localmind.context import Compiler, SYSTEM_RULES, to_messages
from localmind.evaluation import evaluate_answer
from localmind.memory import ForgottenSource, ScopeDenied, Store
from localmind.runtime import Runtime

ARMS = ('no_memory', 'recent_history', 'full_valid_history', 'localmind_mcp', 'localmind_context')
BUDGET = 12288


class MCPClient:
    def __init__(self, database, scope):
        self.process = subprocess.Popen([
            sys.executable, str(ROOT / 'tools/run_mcp.py'), '--db', str(database),
            '--project', scope['project_id'], '--task', scope['task_id'],
            '--session', scope['logical_session_id'], 'mcp'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.next_id = 0
        self.calls = []

    def request(self, method, params=None):
        self.next_id += 1
        request = {'jsonrpc':'2.0', 'id':self.next_id, 'method':method, 'params':params or {}}
        self.process.stdin.write(json.dumps(request, ensure_ascii=False) + '\n')
        self.process.stdin.flush()
        if not self.selector.select(timeout=5):
            raise RuntimeError('MCP_RESPONSE_TIMEOUT')
        response = json.loads(self.process.stdout.readline())
        if response.get('id') != self.next_id or 'error' in response:
            raise RuntimeError('MCP_RPC_ERROR')
        return response['result']

    def start(self):
        self.request('initialize', {'protocolVersion':'2025-06-18', 'capabilities':{},
                                   'clientInfo':{'name':'localmind-effect-evaluation', 'version':'1'}})
        self.process.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.process.stdin.flush()

    def tool(self, name, arguments):
        started = time.monotonic()
        result = self.request('tools/call', {'name':name, 'arguments':arguments})
        self.calls.append({'tool':name, 'arguments':arguments, 'result':result,
                           'latency_ms':round((time.monotonic()-started)*1000, 3)})
        if result.get('isError'):
            raise RuntimeError('MCP_TOOL_UNAVAILABLE')
        return result['structuredContent']

    def close(self):
        self.selector.close()
        self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        self.process.stdout.close()


def seed_case(store, case):
    records = []
    for index, item in enumerate(case['history']):
        scope = {**case['scope'], 'logical_session_id':item.get('session_id', 'history')}
        for key in ('project_id', 'task_id'):
            scope[key] = item.get(key, scope[key])
        store.register_project(scope['project_id'])
        store.register_task(scope['project_id'], scope['task_id'])
        adapter = ReplayAdapter([item.get('assistant', '[fixture has no assistant response]')])
        if 'assistant' in item:
            result = Runtime(store, adapter).run(scope, item['user'], 'fixture-%d' % index,
                                               changes=item.get('changes'))
            records.append((result['packet']['original_request']['event_id'],
                            result['completion']['event_id']))
        else:
            turn = store.receive(scope, item['user'], 'fixture-%d' % index, changes=item.get('changes'))
            store.finish_capture(turn['turn_id'])
            records.append((turn['input_event_id'], None))
    scope = {**case['scope'], 'logical_session_id':'new-evaluation-session'}
    for operation in case.get('operations', []):
        if operation['kind'] != 'forget':
            raise ValueError('Unknown fixture operation')
        store.forget(scope, records[operation['history_index']][0])
    surviving = []
    for pair in records:
        messages = []
        for event_id in pair:
            if event_id is None:
                continue
            try:
                event = store.get_event(scope, event_id)
            except (ForgottenSource, ScopeDenied):
                continue
            messages.append({'role':event['actor'], 'content':event['raw_content']})
        if messages:
            surviving.append(messages)
    return scope, surviving


def prepare(fixture_path, output, bounded_recall=False):
    raw_fixture = fixture_path.read_bytes()
    fixture = json.loads(raw_fixture)
    shared = SYSTEM_RULES + '\n' + fixture['shared_instruction']
    jobs, case_audit = [], []
    for case in fixture['cases']:
        with tempfile.TemporaryDirectory(prefix='localmind-effect-') as directory:
            database = Path(directory) / 'state.sqlite'
            with Store(database) as store:
                scope, history = seed_case(store, case)
            question = case['question']
            final = {'role':'user', 'content':question + '\n输出 JSON 结构：' +
                     json.dumps(case['response_schema'], ensure_ascii=False, separators=(',', ':'))}
            common = [{'role':'system', 'content':shared}]
            arms = {
                'no_memory':common+[final],
                'recent_history':common+[m for turn in history[-2:] for m in turn]+[final],
                'full_valid_history':common+[m for turn in history for m in turn]+[final],
            }
            retrieval_start = time.monotonic()
            client = MCPClient(database, scope)
            try:
                client.start()
                if bounded_recall:
                    material = client.tool('localmind_recall', {'query':question, 'budget_bytes':8192})
                else:
                    state = client.tool('localmind_state', {})
                    search = client.tool('localmind_search', {'query':question, 'limit':6})
                    sources = {item['event_id'] for item in state['evidence'] + state['proposal_references']}
                    if state.get('last_assistant_result'):
                        sources.add(state['last_assistant_result']['event_id'])
                    expansions = []
                    for item in search['results']:
                        if item['event_id'] not in sources:
                            expansions.append(client.tool('localmind_expand', {'event_id':item['event_id']}))
                            sources.add(item['event_id'])
                    material = {'state':state, 'search':search, 'expanded_sources':expansions}
                arms['localmind_mcp'] = common + [{'role':'user', 'content':json.dumps(
                    {'source':'actual local MCP historical data', 'results':material},
                    ensure_ascii=False, separators=(',', ':'))}] + [final]
                mcp_error = None
            except (ValueError, RuntimeError, OSError) as exc:
                mcp_error = type(exc).__name__
            finally:
                client.close()
            retrieval_ms = round((time.monotonic()-retrieval_start)*1000, 3)
            with Store(database) as store:
                turn = store.receive(scope, final['content'], 'evaluation-question')
                start = time.monotonic()
                try:
                    packet = Compiler(store).compile(scope, turn['turn_id'], budget_bytes=BUDGET)
                    messages = to_messages(packet)
                    messages[0] = common[0]
                    arms['localmind_context'] = messages
                    context_error = None
                except (ValueError, RuntimeError) as exc:
                    context_error = type(exc).__name__
                context_ms = round((time.monotonic()-start)*1000, 3)
            case_audit.append({'case_id':case['id'], 'capture_mode':case['capture_mode'],
                               'surviving_history_turns':len(history), 'mcp_calls':client.calls,
                               'mcp_host_policy':('one budgeted recall(8192)' if bounded_recall else
                                                  'state, query search(limit6), expand unique sources missing in state'),
                               'mcp_latency_ms':retrieval_ms, 'compiler_latency_ms':context_ms})
            for arm in ARMS:
                messages = arms.get(arm)
                size = len(json.dumps(messages, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) if messages else None
                error = (mcp_error if arm == 'localmind_mcp' else context_error if arm == 'localmind_context' else None)
                if size is not None and size > BUDGET:
                    error = 'BUDGET_INSUFFICIENT'
                jobs.append({'case_id':case['id'], 'category':case['category'], 'capture_mode':case['capture_mode'],
                             'arm':arm, 'messages':messages, 'response_schema':case['response_schema'],
                             'utf8_bytes':size, 'preparation_error':error,
                             'retrieval_latency_ms':retrieval_ms if arm=='localmind_mcp' else context_ms if arm=='localmind_context' else 0})
    random.Random(20261021).shuffle(jobs)
    report = {'kind':'frozen-synthetic-contrast-requests', 'fixture_sha256':hashlib.sha256(raw_fixture).hexdigest(),
              'input_budget_bytes':BUDGET, 'seed':20261021, 'arms':ARMS, 'cases':case_audit, 'jobs':jobs,
              'model_calls_in_preparation':0, 'fixture_assistant_replies':'synthetic, not real inference',
              'mcp_policy':'bounded_recall' if bounded_recall else 'state_search_expand',
              'native_host_acceptance':False, 'model_autonomous_tool_choice':False}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'jobs':len(jobs), 'preparation_failures':sum(bool(j['preparation_error']) for j in jobs),
                      'mcp_subprocess_cases':len(case_audit), 'output':str(output)}))


def score(fixture_path, requests_path, responses_path, output):
    fixture = json.loads(fixture_path.read_text())
    requests = json.loads(requests_path.read_text())
    responses = json.loads(responses_path.read_text())
    if hashlib.sha256(fixture_path.read_bytes()).hexdigest() != requests['fixture_sha256']:
        raise ValueError('Fixture changed after request preparation')
    cases = {case['id']:case for case in fixture['cases']}
    raw = {(item['case_id'], item['arm']):item for item in responses.get('results', [])}
    if len(raw) != len(responses.get('results', [])):
        raise ValueError('Duplicate model result identity')
    expected_keys = {(job['case_id'], job['arm']) for job in requests['jobs']}
    if set(raw) - expected_keys:
        raise ValueError('Unexpected model result identity')
    records, groups = [], defaultdict(list)
    for job in requests['jobs']:
        case = cases[job['case_id']]
        response = raw.get((job['case_id'],job['arm']), {})
        error = job.get('preparation_error') or response.get('error')
        if error or not response:
            judgment = {'passed':False, 'parsed':None, 'errors':[error or 'MISSING_RESPONSE']}
        else:
            judgment = evaluate_answer(response.get('text',''),case['expected'],case.get('accepted_values'),
                                       truncated=response.get('done_reason')=='length')
        record = {**{k:job[k] for k in ('case_id','arm','category','capture_mode','utf8_bytes','retrieval_latency_ms')},
                  'answer':response.get('text'), 'expected':case['expected'], 'judgment':judgment,
                  'model_latency_ms':response.get('latency_ms'), 'usage':response.get('usage')}
        records.append(record)
        groups[job['arm']].append(record)
    summaries = {}
    for arm, items in groups.items():
        tokens_in = [i['usage']['prompt_eval_count'] for i in items if isinstance(i.get('usage'),dict)
                     and isinstance(i['usage'].get('prompt_eval_count'),int)]
        tokens_out = [i['usage']['eval_count'] for i in items if isinstance(i.get('usage'),dict)
                      and isinstance(i['usage'].get('eval_count'),int)]
        times = [i['model_latency_ms']+i['retrieval_latency_ms'] for i in items if i['model_latency_ms'] is not None]
        summaries[arm] = {'passed':sum(i['judgment']['passed'] for i in items),'total':len(items),
                          'failed_cases':[i['case_id'] for i in items if not i['judgment']['passed']],
                          'input_tokens':sum(tokens_in) if tokens_in else None,
                          'output_tokens':sum(tokens_out) if tokens_out else None,
                          'median_end_to_end_ms':round(statistics.median(times),2) if times else None,
                          'explicit_state':{'passed':sum(i['judgment']['passed'] for i in items if i['capture_mode']=='explicit_state'),
                                            'total':sum(i['capture_mode']=='explicit_state' for i in items)},
                          'capture_only':{'passed':sum(i['judgment']['passed'] for i in items if i['capture_mode']=='capture_only'),
                                          'total':sum(i['capture_mode']=='capture_only' for i in items)}}
    report = {'kind':'real-local-model-development-contrast', 'model':responses.get('model'),
              'fixture_sha256':requests['fixture_sha256'], 'summary':summaries, 'records':records,
              'accounting':responses.get('accounting'), 'warmup':responses.get('warmup'),
              'cleanup':responses.get('cleanup'),
              'limits':['11 public synthetic development probes; not a held-out benchmark or generalized success rate',
                        'MCP calls selected by deterministic host; no autonomous host-tool-selection acceptance',
                        'Same schema/system/budget/model for all arms; state metadata only where fixture explicitly supplies it',
                        'Warm model keep-alive differs from the production adapters default unloading behavior',
                        'Cloud Codex and this current chat were not used as model backends',
                        'These are short-history probes, not a long-context benchmark',
                        'Token totals may cover unequal model-send counts because preparation failures remain in denominators']}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'summary':summaries, 'output':str(output)},ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command',choices=('prepare','score'))
    parser.add_argument('--fixture',type=Path,default=ROOT/'eval/contrast_cases.json')
    parser.add_argument('--requests',type=Path,default=ROOT/'docs/contrast-requests.json')
    parser.add_argument('--responses',type=Path,default=ROOT/'docs/contrast-responses.json')
    parser.add_argument('--output',type=Path,default=ROOT/'docs/contrast-report.json')
    parser.add_argument('--bounded-recall',action='store_true')
    args = parser.parse_args()
    if args.command == 'prepare':
        prepare(args.fixture,args.requests,args.bounded_recall)
    else:
        score(args.fixture,args.requests,args.responses,args.output)


if __name__=='__main__':
    main()
