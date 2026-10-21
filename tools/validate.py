"""Run synthetic invariant tests and save a machine-readable development report."""
from __future__ import annotations

from datetime import datetime, timezone
import io
import json
from pathlib import Path
import platform
import sqlite3
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from test_ids(item)
        else:
            yield item.id()


def main():
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), top_level_dir=str(ROOT))
    identifiers = list(test_ids(suite))
    stream = io.StringIO()
    started = time.monotonic()
    result = unittest.TextTestRunner(stream=stream, verbosity=1).run(suite)
    elapsed = time.monotonic() - started
    parsed = json.loads((ROOT / 'contracts/runtime.schema.json').read_text())
    from localmind.cli import run_demo
    replay = run_demo()
    (ROOT / 'docs/replay-demo.json').write_text(json.dumps(replay, ensure_ascii=False, indent=2))
    report = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'kind': 'synthetic-development-validation', 'protocol_version': '0.3',
        'application_version': '0.5.0',
        'mcp': {'protocol_version': '2025-06-18', 'transport': 'real-python-subprocess-stdio',
                'native_codex_host_acceptance': 'not_run', 'automatic_capture_hook': 'not_implemented'},
        'runtime': {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
                    'third_party_runtime_dependencies': []},
        'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
        'skipped': len(result.skipped), 'passed': result.wasSuccessful(),
        'elapsed_s': round(elapsed, 4), 'test_ids': identifiers,
        'schema': {'json_parse': True, 'definitions': list(parsed['$defs']),
                   'standard_json_schema_engine': 'not_installed',
                   'semantic_validator': 'localmind.context.validate_packet'},
        'replay': {'kind': replay['kind'], 'turn_count': len(replay['turns']),
                   'correction_visible': replay['correction_visible'],
                   'decision_removed': replay['decision_removed'], 'actual_transport': 'simulated'},
        'old_seed_cases': {'count': 42, 'status': 'semantic_fixtures_not_executed_as_suite'},
        'cloud_model_calls': 0, 'held_out_product_acceptance': 'not_run',
        'notes': ['Tests use synthetic data. Live model results are separate files.',
                  'Passing development tests is not a production release acceptance result.']}
    (ROOT / 'docs/validation-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(stream.getvalue(), end='')
    print(json.dumps({'passed': report['passed'], 'tests_run': result.testsRun,
                      'report': 'docs/validation-report.json'}, ensure_ascii=False))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
