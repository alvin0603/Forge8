"""Production CLI delivery with real source/engine gates, never a real model.

Only verified-asset/process fixtures and ordinary transport responses are mocked.
The owned source contains a top-level exception and is never imported or executed.
"""

import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from forge8 import cli, explain, inference
from forge8.server import ServerPreparation
from forge8.trace import verify_trace
from test_explain import answer_document as legacy_answer_wire
from test_fix_cli import _supervisor_type
import test_resident_reading as resident_fixture


_PROSE = '  回傳 7。\n"literal" \\ path\t🧪\n  '
_REASON = "需要呼叫端的輸入；這是模型判斷。"


def answer_wire(text=_PROSE, *, evidence_id="E1"):
    return json.dumps({"kind": "answer", "text": text, "citations": [
        {"evidence_id": evidence_id, "start_line": 3, "end_line": 4}]})


class ReadingDeliveryTests(unittest.TestCase):
    # Borrow fixture helpers only, never inherit the fixture's independent tests.
    start_patch = resident_fixture.ResidentReadingTests.start_patch
    cleanup_owner = resident_fixture.ResidentReadingTests.cleanup_owner
    patch = resident_fixture.ResidentReadingTests.patch

    def setUp(self):
        resident_fixture.ResidentReadingTests.setUp(self)
        self.source_bytes = (self.source / "app.py").read_bytes()
        self.responses = []
        self.after_chat = None
        test = self

        def chat(transport, request):
            test.calls.append(request)
            test.transports.append(transport)
            test.assertTrue(test.responses, "unexpected extra inference call")
            raw, finish = test.responses.pop(0)
            # Split every escape, Unicode surrogate pair and wrapper boundary.
            if transport.on_text is not None:
                for character in raw:
                    transport.on_text(character)
            if test.after_chat is not None:
                test.after_chat(transport, request)
            return inference.ChatResponse(raw, finish,
                {"prompt_tokens": 100, "completion_tokens": 25}, {"predicted_n": 25})

        self.patch(inference.OpenAITransport, "chat", chat)

    def run_delivery(self, raw=None, *, resident=False, reader="qwen35", kind="explain",
            finish="stop", stream=True, question="What does answer return?", fault=None):
        raw = answer_wire() if raw is None else raw
        self.responses = ([(json.dumps({"candidates": ["D0001"]}), "stop")]
            if kind == "project" else []) + [(raw, finish)]
        cancellation = threading.Event()
        chunks, results, phases = [], [], []
        first_call = len(self.calls)
        args = argparse.Namespace(repo=self.source, question=question,
            focus=["app.py:3-4"] if kind == "explain" else [], reader=reader, as_json=True)

        def after_chat(_transport, _request):
            if fault == "cancel":
                cancellation.set()
            elif fault == "source":
                (self.source / "app.py").write_bytes(self.source_bytes + b"# changed\n")

        self.after_chat = after_chat
        runner = {"explain": cli._run_explain_cli, "locate": cli._run_locate_cli,
            "project": cli._run_project_cli}[kind]
        supervisor = _supervisor_type(shutdown_ok=fault != "cleanup")
        plan = SimpleNamespace(endpoint=self.plan.endpoint,
            model_manifest_path=self.assets / "model.json",
            as_dict=lambda: {"endpoint": self.plan.endpoint})
        with ExitStack() as stack:
            if not resident:
                stack.enter_context(patch.object(cli, "prepare_server",
                    return_value=ServerPreparation("ready", (), plan=plan)))
                stack.enter_context(patch.object(cli, "LocalServerSupervisor", supervisor))
            engine = stack.enter_context(patch.object(cli, "run_explanation",
                wraps=explain.run_explanation))
            code = runner(args, on_result=results.append, on_progress=phases.append,
                cancel_event=cancellation,
                **({"on_text": chunks.append if stream else None} if kind != "locate" else {}),
                **({"resident_model": self.owner} if resident else {}))
        self.assertEqual(len(results), 1, results)
        self.assertEqual(self.responses, [], "planned ordinary response was not consumed")
        return SimpleNamespace(code=code, result=results[0], chunks=chunks, phases=phases,
            requests=self.calls[first_call:], transports=self.transports[first_call:],
            engine_calls=engine.call_args_list, supervisors=supervisor.instances, question=question)

    def assert_safe_result(self, run, *, accepted):
        result = run.result
        self.assertIs(result["ok"], accepted)
        self.assertIs(result["semantic_claims_verified"], False)
        self.assertIs(result["repository_code_executed"], False)
        self.assertTrue(all(transport.api_key is None for transport in run.transports))
        root = Path(result["run_root"])
        self.assertTrue(verify_trace(root / "trace.jsonl", root / "trace.seal.json").ok)
        document = json.loads((root / "explanation.json").read_bytes())
        self.assertIs(document["semantic_claims_verified"], False)
        self.assertIs(document["repository_code_executed"], False)
        if not accepted:
            self.assertIsNone(document["answer"])
            self.assertIsNone((result.get("outcome") or {}).get("answer"))
            self.assertIsNone(cli._unverified_explain_prose(result))
            self.assertNotIn("unverified_prose", document)
        return root, document

    def assert_qwen_request(self, request, question, *, resident):
        self.assertEqual(request.model, "pinned-model")
        self.assertEqual(request.response_format, explain._STRUCTURED_READING_FORMAT)
        self.assertEqual([message.role for message in request.messages], ["system", "user"])
        self.assertEqual(request.messages[0].content, explain._STRUCTURED_READING_PROMPT)
        self.assertIn("     3|def answer():\n     4|    return 7", request.messages[1].content)
        self.assertTrue(request.messages[1].content.endswith(question))
        self.assertNotIn("never import this reading source", request.messages[1].content)
        self.assertEqual((request.temperature, request.top_p, request.top_k,
            request.min_p, request.presence_penalty, request.repeat_penalty),
            (0.6, 0.95, 20, 0.0, 0.0, 1.0))
        self.assertEqual((request.max_tokens, request.seed), (4096, 1))
        self.assertIsNone(request.reasoning_budget_tokens)
        self.assertIsNone(request.enable_thinking)
        self.assertIs(request.cache_prompt, True if resident else None)

    def test_qwen_cli_streams_exact_decoded_prose_and_retains_citations_both_lifecycles(self):
        for resident in (False, True):
            with self.subTest(resident=resident):
                run = self.run_delivery(resident=resident)
                self.assertEqual(run.code, 0, run.result)
                self.assertEqual(len(run.requests), 1)
                self.assert_qwen_request(run.requests[0], run.question, resident=resident)
                self.assertEqual(len(run.engine_calls), 1)
                self.assertEqual(run.engine_calls[0].kwargs["reading_format"], "structured_v1")
                self.assertGreater(len(run.chunks), 1)
                self.assertEqual("".join(run.chunks), _PROSE)
                root, document = self.assert_safe_result(run, accepted=True)
                claim, = document["answer"]["claims"]
                self.assertEqual(claim["text"], _PROSE)
                citation, = claim["citations"]
                self.assertEqual((citation["evidence_id"], citation["path"],
                    citation["start_line"], citation["end_line"]), ("E1", "app.py", 3, 4))
                self.assertEqual(citation["file_sha256"], hashlib.sha256(self.source_bytes).hexdigest())
                self.assertEqual(document["answer"]["citation_scope"], "document_references_only")
                self.assertIs(run.result["outcome"]["source_unchanged"], True)
                self.assertIs(run.result["outcome"]["snapshot_unchanged"], True)
                self.assertNotIn(answer_wire().encode(), (root / "ANSWER.txt").read_bytes())
                self.assertEqual((self.source / "app.py").read_bytes(), self.source_bytes)
                self.assertEqual(cli._explain_gpu_state(run.result["server"]),
                    "resident" if resident else "released")

    def test_qwen_without_a_stream_sink_still_uses_structured_final_delivery(self):
        run = self.run_delivery(stream=False)
        self.assertEqual(run.code, 0, run.result)
        self.assertEqual(run.chunks, [])
        self.assertIsNone(run.transports[0].on_text)
        self.assert_qwen_request(run.requests[0], run.question, resident=False)
        self.assert_safe_result(run, accepted=True)

    def test_preview_cannot_accept_malformed_truncated_or_unknown_citation_results(self):
        cases = ((answer_wire()[:-1], "stop"), (answer_wire(), "length"),
            (answer_wire(evidence_id="E999"), "stop"))
        for resident in (False, True):
            for raw, finish in cases:
                with self.subTest(resident=resident, finish=finish, raw=raw[-30:]):
                    run = self.run_delivery(raw, resident=resident, finish=finish)
                    self.assertNotEqual(run.code, 0)
                    self.assertEqual(len(run.requests), 1)
                    # A decoded preview is visible before the independent final refusal.
                    self.assertEqual("".join(run.chunks), _PROSE)
                    self.assert_safe_result(run, accepted=False)
                    self.assertIsNone(cli._insufficient_explain_reason(run.result))

    def test_cancel_source_drift_and_failed_cleanup_cannot_publish_streamed_answer(self):
        for resident in (False, True):
            for fault in (("cancel", "source", "cleanup") if not resident else ("cancel", "source")):
                with self.subTest(resident=resident, fault=fault):
                    try:
                        run = self.run_delivery(resident=resident, fault=fault)
                        self.assertNotEqual(run.code, 0)
                        self.assertEqual(len(run.requests), 1)
                        self.assertEqual("".join(run.chunks), _PROSE)
                        self.assert_safe_result(run, accepted=False)
                        self.assertIsNone(cli._insufficient_explain_reason(run.result))
                        if resident:
                            self.assertEqual(self.owner.status()["state"], "unloaded")
                    finally:
                        (self.source / "app.py").write_bytes(self.source_bytes)

    def test_insufficient_keeps_existing_manual_and_project_recovery_without_json_preview(self):
        raw = json.dumps({"kind": "insufficient", "reason": _REASON})
        for resident in (False, True):
            for kind in ("explain", "project"):
                with self.subTest(resident=resident, kind=kind):
                    run = self.run_delivery(raw, resident=resident, kind=kind)
                    self.assertEqual(run.code, 1, run.result)
                    self.assertEqual(run.chunks, [])
                    self.assertEqual(cli._insufficient_explain_reason(run.result), _REASON)
                    root, _ = self.assert_safe_result(run, accepted=False)
                    ingress = json.loads((root / "input/ingress.json").read_bytes())
                    self.assertEqual(ingress["focus"], ["app.py:3-4"])
                    if kind == "project":
                        self.assertEqual(ingress["focus_origin"], "project_candidates")
                    else:
                        self.assertNotIn("focus_origin", ingress)
                    self.assertEqual(len(run.requests), 2 if kind == "project" else 1)
                    self.assert_qwen_request(run.requests[-1], run.question, resident=resident)
                    if kind == "project":
                        self.assertIs(run.result["project_reading"]["answer_attempted"], True)
                        self.assertEqual(run.result["project_reading"]["focus"],
                            [{"path": "app.py", "start_line": 3, "end_line": 4}])
                        self.assertIsNone(run.transports[0].on_text)
                        self.assertNotEqual(run.requests[0].response_format, explain._STRUCTURED_READING_FORMAT)
                    if resident:
                        self.assertTrue(cli._reading_request_complete(run.result))
                        self.assertEqual(self.owner.status()["state"], "idle")

    def test_resident_second_question_reuses_session_not_previous_prose_or_wrapper(self):
        first = self.run_delivery(resident=True, question="First self-contained question?")
        second = self.run_delivery(answer_wire("Second answer."), resident=True,
            question="Second self-contained question?")
        self.assertEqual((first.code, second.code), (0, 0))
        self.assertEqual(len(self.supervisors), 1)
        self.assertEqual(first.result["request_completion"], second.result["request_completion"])
        self.assertEqual("".join(second.chunks), "Second answer.")
        self.assertNotIn(_PROSE, second.requests[0].messages[1].content)
        self.assertNotIn(first.question, second.requests[0].messages[1].content)
        self.assertEqual(self.supervisors[0].stop_calls, 0)
        self.assert_safe_result(second, accepted=True)

    def test_discovery_stays_nonstreaming_and_does_not_route_through_reading_schema(self):
        run = self.run_delivery('{"candidates":["D0001"]}', kind="locate", resident=True)
        self.assertEqual(run.code, 0, run.result)
        self.assertEqual(run.engine_calls, [])
        self.assertEqual(len(run.requests), 1)
        self.assertEqual(run.chunks, [])
        self.assertIsNone(run.transports[0].on_text)
        self.assertNotEqual(run.requests[0].response_format, explain._STRUCTURED_READING_FORMAT)
        self.assertNotIn("     4|    return 7", run.requests[0].messages[1].content)

    def test_gemma_readers_keep_existing_prose_and_action_routes(self):
        prose = "It returns 7. [E1:L3-L4]"
        action = legacy_answer_wire(inference="It returns 7.")
        for reader, raw in (("gemma12b", prose), ("gemma4", action)):
            with self.subTest(reader=reader):
                run = self.run_delivery(raw, reader=reader)
                self.assertEqual(run.code, 0, run.result)
                self.assertEqual(len(run.requests), 1)
                self.assertNotIn("reading_format", run.engine_calls[0].kwargs)
                self.assertNotIn("reading_recipe", run.result["outcome"]["inference"])
                if reader == "gemma12b":
                    self.assertIsNone(run.requests[0].response_format)
                    self.assertEqual("".join(run.chunks), prose)
                    self.assertEqual((run.requests[0].temperature, run.requests[0].top_k), (1.0, 64))
                else:
                    self.assertIsNotNone(run.requests[0].response_format)
                    self.assertNotEqual(run.requests[0].response_format, explain._STRUCTURED_READING_FORMAT)
                    self.assertEqual(run.chunks, [])
                    self.assertIsNone(run.transports[0].on_text)
                self.assert_safe_result(run, accepted=True)


if __name__ == "__main__":
    unittest.main()
