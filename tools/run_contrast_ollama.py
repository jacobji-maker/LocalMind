"""Run already-frozen contrast requests against one installed local Ollama model.

No requests are prepared here and no answers are judged here. Jobs retain the
fixture's randomized order. Durable pending markers prevent an interrupted
model request from being silently retried on resume. This runner intentionally
keeps the model warm for evaluation; production adapter defaults differ.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from localmind.adapters import local_endpoint, post_json


SETTINGS = {"stream": False, "think": False, "keep_alive": "5m",
            "options": {"temperature": 0, "seed": 20261021, "num_predict": 96, "num_ctx": 8192}}
USAGE_KEYS = ("prompt_eval_count", "eval_count", "load_duration", "total_duration",
              "prompt_eval_duration", "eval_duration")
MAX_FILE_BYTES = 32 * 1024 * 1024


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read_json(path):
    with path.open("rb") as source:
        raw = source.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("INPUT_FILE_TOO_LARGE")
    return raw, json.loads(raw)


def _job_key(job):
    values = tuple(job.get(key) for key in ("case_id", "arm"))
    if any(not isinstance(value, str) or not value or len(value) > 128 for value in values):
        raise ValueError("INVALID_JOB_IDENTITY")
    return values


def _validate_jobs(jobs):
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= 1000:
        raise ValueError("INVALID_JOBS")
    identities = set()
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("INVALID_JOB")
        key = _job_key(job)
        if key in identities:
            raise ValueError("DUPLICATE_JOB")
        identities.add(key)
        if job.get("preparation_error"):
            continue
        messages = job.get("messages")
        if (not isinstance(messages, list) or not messages or
                any(not isinstance(item, dict) or set(item) != {"role", "content"} or
                    item["role"] not in ("system", "user", "assistant") or
                    not isinstance(item["content"], str) for item in messages) or
                not isinstance(job.get("response_schema"), dict)):
            raise ValueError("INVALID_PREPARED_MESSAGES")
        if len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) > 65536:
            raise ValueError("PREPARED_MESSAGES_TOO_LARGE")
    return identities


def _usage(raw):
    values = {}
    for key in USAGE_KEYS:
        value = raw.get(key)
        values[key] = value if (type(value) in (int, float) and value >= 0 and
                                (type(value) is int or math.isfinite(value))) else None
    return values


def _accounting(report):
    calls = ([report["warmup"]] if report.get("warmup") else []) + report["results"]
    calls = [item for item in calls if item.get("model_called") is True]
    prompt = [item.get("usage", {}).get("prompt_eval_count")
              if isinstance(item.get("usage"), dict) else None for item in calls]
    generated = [item.get("usage", {}).get("eval_count")
                 if isinstance(item.get("usage"), dict) else None for item in calls]
    return {"model_calls_attempted_including_warmup": len(calls),
            "completed_transport_calls": sum(item.get("error") is None and item.get("status") == "completed"
                                             for item in calls),
            "calls_with_unknown_usage": sum(left is None or right is None
                                             for left, right in zip(prompt, generated)),
            "known_input_tokens": sum(value for value in prompt if value is not None),
            "known_output_tokens": sum(value for value in generated if value is not None),
            "input_tokens": sum(prompt) if all(value is not None for value in prompt) else None,
            "output_tokens": sum(generated) if all(value is not None for value in generated) else None,
            "warmup_included": bool(report.get("warmup")),
            "jobs_planned": report["jobs_planned"], "job_results_recorded": len(report["results"]),
            "cloud_model_calls_by_runner": 0,
            "note": "Unknown usage remains unknown; known_* totals are partial when any call is unknown."}


def _save(path, report):
    report["updated_at"] = _now()
    report["accounting"] = _accounting(report)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as target:
            temporary = Path(target.name)
            json.dump(report, target, ensure_ascii=False, indent=2, allow_nan=False)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("LOCAL_MODEL_REDIRECT_REJECTED")


def _installed_model(base_url, model, timeout):
    # Discovery is metadata only. No pull/download, authentication files or
    # provider account APIs are used. Ambient proxies cannot forward loopback.
    request = Request(base_url + "/api/tags", headers={"Accept": "application/json"})
    with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=timeout) as response:
        body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise ValueError("MODEL_METADATA_TOO_LARGE")
    raw = json.loads(body)
    models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(models, list):
        raise ValueError("MODEL_METADATA_INVALID")
    item = next((entry for entry in models if isinstance(entry, dict) and
                 model in (entry.get("name"), entry.get("model"))), None)
    if item is None or item.get("remote_host") or item.get("remote_model"):
        raise ValueError("INSTALLED_LOCAL_MODEL_NOT_CONFIRMED")
    digest = item.get("digest")
    return {"installed_model": "confirmed",
            "model_digest": digest if isinstance(digest, str) and re.fullmatch("[a-f0-9]{64}", digest) else None,
            "inference_locality": "Installed model on loopback; server-side cloud disabling must be verified separately."}


def _invoke(base_url, payload, timeout, result):
    started = time.monotonic()
    try:
        raw = post_json(base_url + "/api/chat", payload, timeout)
        result["usage"] = _usage(raw)
        message = raw.get("message")
        reason = raw.get("done_reason")
        result["done_reason"] = reason if isinstance(reason, str) and re.fullmatch("[A-Za-z_]{1,64}", reason) else "unknown"
        if not isinstance(message, dict) or message.get("tool_calls"):
            result.update(status="failed", error="MODEL_MESSAGE_OR_TOOL_CALL_INVALID")
        elif raw.get("done") is not True or not isinstance(message.get("content"), str) or not message["content"].strip():
            result.update(status="failed", error="INCOMPLETE_MODEL_MESSAGE")
        else:
            result.update(status="completed", text=message["content"], error=None)
    except (OSError, ValueError, RuntimeError) as exc:
        # Do not leak exception text, request bodies, server diagnostics or
        # credentials. An unknown remote outcome is recorded and never retried.
        result.update(status="outcome_unknown", error=type(exc).__name__)
    finally:
        result["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
        result["finished_at"] = _now()


def _pending(case_id=None, arm=None):
    result = {"status": "pending", "model_called": True, "text": None,
              "done_reason": None, "usage": None, "latency_ms": None,
              "error": "OUTCOME_UNKNOWN_PENDING", "started_at": _now()}
    if case_id is not None:
        result.update(case_id=case_id, arm=arm)
    return result


def run(requests_path, output_path, model, base_url, timeout):
    raw, requests = _read_json(requests_path)
    if not isinstance(requests, dict):
        raise ValueError("INVALID_REQUEST_FILE")
    jobs = requests.get("jobs")
    identities = _validate_jobs(jobs)
    digest = hashlib.sha256(raw).hexdigest()
    if output_path.exists():
        _, report = _read_json(output_path)
        if (not isinstance(report, dict) or report.get("requests_sha256") != digest or
                report.get("model") != model or report.get("settings") != SETTINGS or
                report.get("ollama_origin") != base_url or not isinstance(report.get("results"), list)):
            raise ValueError("RESUME_PROVENANCE_MISMATCH")
        existing = set()
        for item in report["results"]:
            if not isinstance(item, dict):
                raise ValueError("INVALID_EXISTING_RESULT")
            key = _job_key(item)
            if key not in identities or key in existing:
                raise ValueError("EXISTING_RESULT_IDENTITY_MISMATCH")
            existing.add(key)
        for item in ([report["warmup"]] if report.get("warmup") else []) + report["results"]:
            if item.get("status") == "pending":
                item.update(status="outcome_unknown", error="OUTCOME_UNKNOWN_AFTER_INTERRUPTION")
    else:
        existing = set()
        report = {"kind": "real-local-model-development-contrast-responses",
                  "created_at": _now(), "requests_sha256": digest,
                  "fixture_sha256": requests.get("fixture_sha256"),
                  "model": model, "ollama_origin": base_url, "settings": SETTINGS,
                  "jobs_planned": len(jobs), "results": [], "warmup": None,
                  "native_host_acceptance": False, "automatic_retry": False}
    _save(output_path, report)
    if len(existing) == len(jobs):
        return report
    started = time.monotonic()
    report["preflight"] = _installed_model(base_url, model, min(timeout, 10))
    report["preflight"]["latency_ms"] = round((time.monotonic() - started) * 1000, 3)
    _save(output_path, report)
    if report.get("warmup") is None:
        warmup = _pending()
        report["warmup"] = warmup
        _save(output_path, report)  # Persist BEFORE starting any model request.
        payload = {**SETTINGS, "model": model, "messages": [{"role": "user", "content": "只回复：就绪"}],
                   "options": {**SETTINGS["options"], "num_predict": 16}}
        _invoke(base_url, payload, timeout, warmup)
        _save(output_path, report)
    if report["warmup"].get("status") != "completed":
        raise ValueError("WARMUP_FAILED_NO_AUTOMATIC_RETRY")
    for job in jobs:
        key = _job_key(job)
        if key in existing:
            continue
        result = _pending(*key)
        report["results"].append(result)
        if job.get("preparation_error"):
            code = job["preparation_error"]
            code = code if isinstance(code, str) and re.fullmatch("[A-Za-z0-9_:-]{1,128}", code) else "PREPARATION_FAILED"
            result.update(status="preparation_failed", model_called=False, error=code, latency_ms=0)
            _save(output_path, report)
        else:
            _save(output_path, report)
            payload = {**SETTINGS, "model": model, "messages": job["messages"],
                       "format": job["response_schema"]}
            _invoke(base_url, payload, timeout, result)
            _save(output_path, report)
        existing.add(key)
        print(json.dumps({"case_id": key[0], "arm": key[1], "status": result["status"],
                          "latency_ms": result["latency_ms"]}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=Path, default=ROOT / "docs/contrast-requests.json")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/contrast-responses.json")
    parser.add_argument("--model", default="qwen3.5:9b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11435")
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args()
    try:
        if (not isinstance(args.model, str) or not args.model or len(args.model) > 128 or
                "cloud" in args.model.casefold() or not 0 < args.timeout <= 45):
            raise ValueError("INVALID_MODEL_OR_TIMEOUT")
        report = run(args.requests, args.output, args.model, local_endpoint(args.ollama_url), args.timeout)
        print(json.dumps({"results": len(report["results"]), "accounting": report["accounting"]}))
        return 0
    except KeyboardInterrupt:
        print("INTERRUPTED: pending requests will remain outcome-unknown, without retry.", file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        # Error messages are program codes only. Never print exception details.
        print(type(exc).__name__ + ": RUN_NOT_COMPLETED", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
