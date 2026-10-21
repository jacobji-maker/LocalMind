"""Project-local CLI. No credentials, global config, model installation, or cloud billing."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile

from .adapters import OllamaAdapter, ReplayAdapter
from .memory import Store
from .models import OllamaPlanner
from .runtime import Runtime


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def scope_for(args):
    return {"project_id": args.project, "task_id": args.task,
            "logical_session_id": args.session}


def change(kind, raw, level="task"):
    if "=" not in raw:
        raise ValueError("Use subject=value; value must appear in the original input")
    key, value = raw.split("=", 1)
    if not key.strip() or not value.strip():
        raise ValueError("subject and value must be nonempty")
    return {"subject_key": key.strip(), "value": value, "scope_level": level, "kind": kind}


def adapter_for(args):
    if args.backend == "replay":
        return ReplayAdapter(["[模拟回答] 本轮受控输入已记录；这不是实际模型答复。"] * 1000)
    return OllamaAdapter(args.model, base_url=args.ollama_url, timeout=args.timeout, num_predict=args.output_tokens)


def run_demo(backend="replay", model="qwen3.5:9b", timeout=45, base_url="http://127.0.0.1:11434"):
    """Synthetic end-to-end replay; the optional Ollama path uses real local inference."""
    with tempfile.TemporaryDirectory(prefix="localmind-demo-") as tmp:
        db = str(Path(tmp) / "demo.sqlite")
        store = Store(db)
        scope = {"project_id": "synthetic-demo", "task_id": "cache", "logical_session_id": "s1"}
        store.register_project(scope["project_id"], tmp)
        store.register_task(scope["project_id"], scope["task_id"])
        if backend == "replay":
            adapter = ReplayAdapter([
                "方案一：SQLite。方案二：文件。方案三：内存。",
                "已选择方案二：文件。缓存有效期24小时。",
                "按2小时继续。",
                "仍是文件方案，当前缓存有效期2小时。",
                "已删除来源；无法据此恢复旧决定。"])
        else:
            adapter = OllamaAdapter(model, base_url=base_url, timeout=timeout, num_predict=192)
        runtime = Runtime(store, adapter)
        first = runtime.run(scope, "目标是做缓存。缓存有效期24小时。请按此顺序给三个方案：方案一SQLite；方案二文件；方案三内存。每个方案只写一行名称，不解释。",
                            "demo-1", changes=[
                                {"subject_key": "goal", "value": "做缓存", "scope_level": "task", "kind": "goal"},
                                {"subject_key": "cache_ttl", "value": "24小时", "scope_level": "task", "kind": "requirement"}])
        selected = runtime.run(scope, "选第二个。只回复选中方案名称和缓存有效期。", "demo-2", changes=[
            {"subject_key": "backend", "value": "第二个", "scope_level": "task", "kind": "decision"}])
        corrected = runtime.run(scope, "缓存有效期改成2小时，按新的继续。只回复方案名称和当前有效期。", "demo-3", changes=[
            {"subject_key": "cache_ttl", "value": "2小时", "scope_level": "task", "kind": "requirement"}])
        store.close()
        store = Store(db)
        scope["logical_session_id"] = "s2"
        recovered = Runtime(store, adapter).run(scope, "继续。之前选了什么，当前缓存有效期是多少？请一句话回答，不解释。", "demo-4")
        current = store.state(scope)
        decision = next(s for s in current["subjects"] if s["subject_key"] == "backend")
        deletion = store.forget(scope, decision["source_event_ids"][0])
        after_forget = Runtime(store, adapter).run(scope, "之前的后端决定还有依据吗？只根据仍存在的确认记录简短回答，不猜测方案。", "demo-5")
        report = {"kind": "simulated-replay" if backend == "replay" else "real-local-inference",
                  "cloud_integration": "not_executed", "backend": backend, "model": model if backend != "replay" else None,
                  "turns": [{"turn_id": r["turn_id"], "answer": r["answer"], "messages": r["messages"],
                             "usage": r["usage"]} for r in (first, selected, corrected, recovered, after_forget)],
                  "correction_visible": any(s["subject_key"] == "cache_ttl" and s["value"] == "2小时" for s in current["subjects"]),
                  "restart_retained_state": True, "deletion": deletion,
                  "decision_removed": not any(s["subject_key"] == "backend" for s in store.state(scope)["subjects"])}
        store.close()
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(prog="localmind", description="LocalMind 0.5 本地上下文运行时")
    parser.add_argument("--db", default=".localmind/state.sqlite")
    parser.add_argument("--project", default="localmind")
    parser.add_argument("--task", default="main")
    parser.add_argument("--session", default="default")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="注册当前项目及任务")
    init.add_argument("--workspace", default=str(Path.cwd()))
    commands.add_parser("status")
    commands.add_parser("inspect")
    commands.add_parser("pause")
    commands.add_parser("resume")
    forget = commands.add_parser("forget")
    forget.add_argument("event_id")
    commands.add_parser("mcp", help="启动固定项目范围的只读 stdio MCP 服务")
    config = commands.add_parser("plugin-config", help="输出可审查的插件接入配置")
    config.add_argument("--format", choices=("codex", "json"), default="codex")
    diagnostic = commands.add_parser("codex-check", help="限时核验 Codex 客户端，不发送模型回合")
    diagnostic.add_argument("--executable", default=None)
    diagnostic.add_argument("--timeout", type=float, default=4)
    remember = commands.add_parser("remember", help="记录用户原话及明确事项，不调用模型")
    remember.add_argument("text")
    remember.add_argument("--dedupe-key", required=True)
    remember.add_argument("--require", action="append", default=[])
    remember.add_argument("--decide", action="append", default=[])
    remember.add_argument("--goal", default=None)
    for name in ("chat", "ask"):
        p = commands.add_parser(name)
        p.add_argument("--backend", choices=("ollama", "replay"), default="ollama")
        p.add_argument("--model", default="qwen3.5:9b")
        p.add_argument("--ollama-url", default="http://127.0.0.1:11434")
        p.add_argument("--planner-model", default=None)
        p.add_argument("--planner-timeout", type=float, default=5)
        p.add_argument("--timeout", type=float, default=45)
        p.add_argument("--output-tokens", type=int, default=256)
        p.add_argument("--budget-bytes", type=int, default=12288)
        if name == "ask":
            p.add_argument("text")
            p.add_argument("--dedupe-key", default=None)
            p.add_argument("--require", action="append", default=[])
            p.add_argument("--decide", action="append", default=[])
            p.add_argument("--goal", default=None)
    demo = commands.add_parser("demo")
    demo.add_argument("--backend", choices=("replay", "ollama"), default="replay")
    demo.add_argument("--model", default="qwen3.5:9b")
    demo.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    demo.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            emit(run_demo(args.backend, args.model, args.timeout, args.ollama_url))
            return 0
        if args.command == "plugin-config":
            from .plugin import configuration
            print(configuration(args.db, scope_for(args), args.format))
            return 0
        if args.command == "mcp":
            from .mcp_server import serve
            return serve(args.db, scope_for(args))
        if args.command == "codex-check":
            from .codex_adapter import probe_codex
            report = probe_codex(args.executable, args.timeout)
            emit(report)
            return 0 if report.get("outcome") == "completed" else 2
        db = Path(args.db).expanduser()
        db.parent.mkdir(parents=True, exist_ok=True)
        store = Store(str(db))
        scope = scope_for(args)
        try:
            if args.command == "init":
                store.register_project(args.project, str(Path(args.workspace).resolve()))
                store.register_task(args.project, args.task)
                emit({"registered": scope, "database": str(db.resolve())})
            elif args.command == "status":
                emit(store.status())
            elif args.command == "inspect":
                emit({"state": store.state(scope), "events": store.events(scope, limit=30)})
            elif args.command == "pause":
                emit(store.pause())
            elif args.command == "resume":
                emit(store.resume())
            elif args.command == "forget":
                emit(store.forget(scope, args.event_id))
            elif args.command == "remember":
                changes = ([change("requirement", s) for s in args.require] +
                           [change("decision", s) for s in args.decide])
                if args.goal:
                    changes.append({"subject_key": "goal", "value": args.goal,
                                    "scope_level": "task", "kind": "goal"})
                turn = store.receive(scope, args.text, args.dedupe_key, changes=changes)
                if turn["phase"] == "received":
                    turn = store.finish_capture(turn["turn_id"])
                emit({"captured": turn.get("outcome") == "captured", "turn_id": turn["turn_id"],
                      "event_id": turn["input_event_id"], "phase": turn["phase"], "model_called": False})
            else:
                planner = OllamaPlanner(args.planner_model, base_url=args.ollama_url,
                                        timeout=args.planner_timeout) if args.planner_model else None
                runtime = Runtime(store, adapter_for(args), planner)
                if args.command == "ask":
                    changes = [change("requirement", s) for s in args.require] + [change("decision", s) for s in args.decide]
                    if args.goal:
                        changes.append({"subject_key": "goal", "value": args.goal, "scope_level": "task", "kind": "goal"})
                    result = runtime.run(scope, args.text, args.dedupe_key, changes=changes, budget_bytes=args.budget_bytes)
                    emit(result)
                else:
                    print("LocalMind：/require key=value、/correct key=value、/decide key=value、/goal 内容、/inspect、/pause、/resume、/forget event_id、/quit。")
                    print("普通输入会保留原话；显式事项命令用于确认状态，当前版本不保证自动理解任意自然语言纠正。")
                    while True:
                        try:
                            text = input("你 > ")
                        except EOFError:
                            break
                        if text == "/quit":
                            break
                        try:
                            if text == "/pause":
                                emit(store.pause()); continue
                            if text == "/resume":
                                emit(store.resume()); continue
                            if text == "/inspect":
                                emit(store.state(scope)); continue
                            if text.startswith("/forget "):
                                emit(store.forget(scope, text[len("/forget "):].strip())); continue
                            updates = []
                            for prefix, kind in (("/require ", "requirement"), ("/correct ", "requirement"), ("/decide ", "decision")):
                                if text.startswith(prefix):
                                    updates = [change(kind, text[len(prefix):])]
                            if text.startswith("/goal "):
                                updates = [{"subject_key": "goal", "value": text[6:], "scope_level": "task", "kind": "goal"}]
                            result = runtime.run(scope, text, changes=updates, budget_bytes=args.budget_bytes)
                            print("助手 > " + (result.get("answer") or "本轮未产生新答复；重复请求或已过时的回传不会更新当前状态。"))
                            if result.get("simulated"):
                                print("[模拟回放]")
                            if result.get("packet", {}).get("degraded_reason"):
                                print("[检索降级：" + str(result["packet"]["degraded_reason"]) + "]")
                        except (ValueError, RuntimeError) as exc:
                            print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
            return 0
        finally:
            store.close()
    except (ValueError, RuntimeError, OSError) as exc:
        print(type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        return 2
