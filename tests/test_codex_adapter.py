import json
import os
from pathlib import Path
import tempfile
import textwrap
import time
import unittest

from localmind.adapters import AdapterError
from localmind.codex_adapter import CodexAppServerAdapter, discover_codex, probe_codex


class CodexDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def executable(self, body):
        path = self.root / "codex"
        path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
        path.chmod(0o700)
        return str(path)

    def test_directory_never_mistaken_for_executable(self):
        self.assertIsNone(discover_codex(str(self.root)))
        self.assertEqual(probe_codex(str(self.root))["outcome"], "executable_not_found")

    def test_protocol_auth_category_only_no_credentials_and_no_cloud_calls(self):
        capture = self.root / "methods.jsonl"
        path = self.executable('''
            import sys, json
            if '--version' in sys.argv:
                print('codex-cli 0.163.0-test')
                sys.exit(0)
            for line in sys.stdin:
                request=json.loads(line)
                with open(CAPTURE, 'a') as out:
                    out.write(json.dumps(request)+'\\n')
                if request.get('id') == 1:
                    print(json.dumps({'id':1,'result':{'userAgent':'client'}}),flush=True)
                elif request.get('id') == 2:
                    print(json.dumps({'id':2,'result':{'account':{'type':'chatgpt','email':'SECRET_EMAIL','token':'SECRET_TOKEN'}}}),flush=True)
        '''.replace("CAPTURE", repr(str(capture))))
        report = probe_codex(path)
        self.assertEqual(report["initialize"], "confirmed")
        self.assertEqual(report["auth_kind"], "chatgpt")
        self.assertEqual(report["cloud_acceptance"], "blocked")
        self.assertEqual(report["tool_boundary"], "unknown")
        self.assertNotIn("SECRET", json.dumps(report))
        requests = [json.loads(x) for x in capture.read_text().splitlines()]
        self.assertEqual([x["method"] for x in requests], ["initialize", "initialized", "account/read"])
        self.assertFalse(requests[-1]["params"]["refreshToken"])

    def test_stuck_server_is_bounded_and_reaped(self):
        pid_path = self.root / "pid"
        path = self.executable('''
            import sys, time, os
            if '--version' in sys.argv:
                print('codex-cli 0.163.0-test')
                sys.exit(0)
            open(PIDPATH,'w').write(str(os.getpid()))
            time.sleep(20)
        '''.replace("PIDPATH", repr(str(pid_path))))
        start = time.monotonic()
        report = probe_codex(path, timeout=1.0)
        self.assertEqual(report["outcome"], "timeout")
        self.assertLess(time.monotonic()-start, 4)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_path.read_text()), 0)

    def test_server_action_request_is_never_approved(self):
        path = self.executable('''
            import sys, json, time
            if '--version' in sys.argv:
                print('codex-cli 0.163.0-test')
                sys.exit(0)
            sys.stdin.readline()
            print(json.dumps({'id':9,'method':'item/commandExecution/requestApproval','params':{}}),flush=True)
            time.sleep(20)
        ''')
        self.assertEqual(probe_codex(path)["outcome"], "unexpected_server_request")

    def test_unverified_adapter_never_sends_even_after_protocol_probe(self):
        with self.assertRaisesRegex(AdapterError, "CODEX_TOOL_BOUNDARY_UNVERIFIED"):
            CodexAppServerAdapter("/does/not/exist").send([{"role":"user","content":"secret"}], "request")

    def test_timeout_validation(self):
        for timeout in (True, 0, -1, 6, float("nan"), "4"):
            with self.assertRaises(ValueError):
                probe_codex(timeout=timeout)
