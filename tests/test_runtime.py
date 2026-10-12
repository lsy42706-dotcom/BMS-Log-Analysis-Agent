"""Exercise the installed Agents SDK without credentials, HTTP, or model requests."""

import copy
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bms_agent.domain import Dataset, default_rules
from bms_agent.samples import generate_samples
from bms_agent.tools import Toolbox
from bms_agent.conclusions import canonical_event_fact
from bms_agent.run_control import checkpoint_state

try:
    from agents.agent_output import AgentOutputSchema
    from agents.exceptions import MaxTurnsExceeded
    from agents.items import ModelResponse
    from agents.models import _openai_shared
    from agents.models.interface import Model, ModelTracing
    from agents.usage import Usage
    from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText
    from bms_agent import runtime
except ModuleNotFoundError:
    SDK_AVAILABLE = False
    Model = object
else:
    SDK_AVAILABLE = True


def toolbox():
    return Toolbox(Dataset(generate_samples("overcurrent"), [], "synthetic.csv", "0" * 64), default_rules())


class ScriptedModel(Model):
    """Real SDK Model interface; returns deterministic Responses API output items."""

    def __init__(self, steps):
        self.steps = steps
        self.seen = []

    async def get_response(self, system_instructions, input, model_settings, tools,
                           output_schema, handoffs, tracing, *, previous_response_id,
                           conversation_id, prompt):
        turn = len(self.seen)
        self.seen.append({"input": copy.deepcopy(input), "settings": model_settings,
                          "tools": tools, "schema": output_schema, "tracing": tracing})
        kind, value = self.steps[turn] if turn < len(self.steps) else self.steps[-1]
        if kind == "tool":
            name, arguments = value
            output = [ResponseFunctionToolCall(name=name, arguments=json.dumps(arguments),
                       call_id=f"local-call-{turn}", id=f"local-item-{turn}", type="function_call")]
        else:
            output = [ResponseOutputMessage(id=f"local-message-{turn}", role="assistant",
                       status="completed", type="message", content=[ResponseOutputText(
                           text=json.dumps(value), type="output_text", annotations=[])])]
        return ModelResponse(output=output, usage=Usage(requests=1, input_tokens=10,
                             output_tokens=5, total_tokens=15), response_id=None)

    async def stream_response(self, *args, **kwargs):
        raise AssertionError("The nonstreaming integration test must not stream")
        yield  # Make this the SDK's required async iterator interface.


def scenario_steps(box, citation=None, code=None, include_required=True):
    event = box.analysis["events"][0]
    steps = [("tool", ("summarize_log", {})),
             ("tool", ("list_protection_rules", {})),
             ("tool", ("detect_faults", {"code": "all", "offset": 0, "limit": 20}))]
    if not include_required:
        steps = steps[:1]
    else:
        steps.append(("tool", ("get_evidence", {"evidence_id": event["evidence_id"]})))
    steps.append(("final", {"summary": "Confirmed sampled overcurrent, not a physical root cause.",
                  "findings": [{"code": code or event["code"],
                                "evidence_ids": [citation or event["evidence_id"]],
                                "facts": [{**canonical_event_fact(event), "event_id": citation or event["evidence_id"]}],
                                "explanation": "The returned evidence identifies the matching rule."}],
                  "limitations": ["Synthetic demonstration thresholds; no battery hardware validation."],
                  "coverage": "full"}))
    return steps


@unittest.skipUnless(SDK_AVAILABLE, "Install the optional .[agent] dependency for SDK integration tests")
class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_runner_executes_tools_and_validates_structured_evidence(self):
        box = toolbox()
        fake = ScriptedModel(scenario_steps(box))
        with patch("bms_agent.runtime.AsyncOpenAI") as client, patch("socket.create_connection", side_effect=AssertionError("No HTTP allowed")):
            result = await runtime.run_agent(box, "Why was overcurrent confirmed?", "", "", model_override=fake)
        client.assert_not_called()
        self.assertEqual([call["tool"] for call in box.calls],
                         ["summarize_log", "list_protection_rules", "detect_faults", "get_evidence"])
        self.assertTrue(all(call["status"] == "complete" for call in box.calls))
        self.assertEqual(result["conclusion"]["findings"][0]["evidence_ids"], ["E001"])
        self.assertEqual({key: result["usage"][key] for key in ("requests", "input_tokens", "output_tokens", "total_tokens", "simulated")},
                         {"requests": 5, "input_tokens": 50, "output_tokens": 25, "total_tokens": 75, "simulated": True})
        self.assertTrue(result["usage"]["complete"])
        self.assertTrue(all(item["tracing"] == ModelTracing.DISABLED for item in fake.seen))
        self.assertTrue(all(item["settings"].store is False for item in fake.seen))
        # Proves the real Runner added a tool result before the next model turn.
        self.assertTrue(any(item.get("type") == "function_call_output" for item in fake.seen[1]["input"]))

    async def test_fabricated_citation_is_rejected_after_real_sdk_completion(self):
        box = toolbox()
        with self.assertRaisesRegex(ValueError, "not returned") as failure:
            await runtime.run_agent(box, "Check faults", "", "", model_override=ScriptedModel(scenario_steps(box, citation="E999")))
        self.assertEqual(failure.exception.bms_usage["total_tokens"], 75)
        self.assertTrue(failure.exception.bms_usage["complete"])

    async def test_budget_preflight_stops_before_model_request(self):
        fake = ScriptedModel(scenario_steps(toolbox()))
        with self.assertRaises(runtime.TokenBudgetExceeded) as failure:
            await runtime.run_agent(toolbox(), "Check faults", "", "", max_total_tokens=2000, model_override=fake)
        self.assertEqual(fake.seen, [])
        self.assertEqual(failure.exception.bms_usage["total_tokens"], 0)
        self.assertEqual(failure.exception.bms_usage["unresolved_requests"], 0)

    async def test_failed_inflight_request_retains_known_and_unknown_usage(self):
        class FailingModel(ScriptedModel):
            async def get_response(self, *args, **kwargs):
                if self.seen:
                    raise TimeoutError("TEST_ONLY_PRIVATE_REQUEST_MATERIAL")
                return await super().get_response(*args, **kwargs)
        with self.assertRaises(TimeoutError) as failure:
            await runtime.run_agent(toolbox(), "Check faults", "", "", model_override=FailingModel([("tool", ("summarize_log", {}))]))
        usage = failure.exception.bms_usage
        self.assertEqual(usage["total_tokens"], 15)
        self.assertEqual(usage["requests"], 1)
        self.assertEqual(usage["unresolved_requests"], 1)
        self.assertFalse(usage["complete"])

    async def test_checkpoint_resumes_verified_tools_with_cumulative_usage(self):
        old = toolbox()
        for name in ("summarize_log", "list_protection_rules", "detect_faults"):
            old.invoke(name)
        prior = {"requests": 3, "input_tokens": 30, "output_tokens": 15, "total_tokens": 45,
                 "complete": True, "unresolved_requests": 0}
        state = checkpoint_state(old, "Check faults", "", prior, status="failed")
        box = toolbox()
        fake = ScriptedModel([scenario_steps(box)[-1]])
        result = await runtime.run_agent(box, "Check faults", "", "", model_override=fake, resume_state=state)
        self.assertTrue(result["resumed"])
        self.assertEqual(len(fake.seen), 1)
        self.assertEqual(result["usage"]["requests"], 4)
        self.assertEqual(result["usage"]["total_tokens"], 60)
        self.assertEqual([call["tool"] for call in box.calls], ["summarize_log", "list_protection_rules", "detect_faults"])
        self.assertEqual(sum(item.get("type") == "function_call_output" for item in fake.seen[0]["input"]), 3)

    async def test_resume_turn_limit_preserves_prior_known_usage(self):
        box = toolbox()
        prior = {"requests": 4, "input_tokens": 40, "output_tokens": 20, "total_tokens": 60,
                 "complete": True, "unresolved_requests": 0}
        state = checkpoint_state(box, "Check faults", "", prior, status="failed")
        with self.assertRaises(ValueError) as failure:
            await runtime.run_agent(toolbox(), "Check faults", "", "", max_turns=4, model_override=ScriptedModel([]), resume_state=state)
        self.assertEqual(failure.exception.bms_usage["total_tokens"], 60)
        self.assertTrue(failure.exception.bms_usage["complete"])

    async def test_model_inputs_and_resume_history_are_redacted(self):
        sentinel = "TEST_ONLY_FILENAME_CREDENTIAL"
        box = toolbox()
        box.dataset.source_name = sentinel + ".csv"
        fake = ScriptedModel(scenario_steps(box))
        await runtime.run_agent(box, "Check faults " + sentinel, "test-model", sentinel, model_override=fake)
        self.assertNotIn(sentinel, json.dumps(fake.seen, default=str))
        prior = {"requests": 3, "input_tokens": 30, "output_tokens": 15, "total_tokens": 45,
                 "complete": False, "unresolved_requests": 1}
        old = toolbox()
        old.dataset.source_name = sentinel + ".csv"
        for name in ("summarize_log", "list_protection_rules", "detect_faults"):
            old.invoke(name)
        state = checkpoint_state(old, "Check faults", "test-model", prior, status="failed", secrets=(sentinel,))
        box = toolbox()
        box.dataset.source_name = sentinel + ".csv"
        fake = ScriptedModel([scenario_steps(box)[-1]])
        result = await runtime.run_agent(box, "Check faults", "test-model", sentinel, model_override=fake, resume_state=state)
        self.assertNotIn(sentinel, json.dumps(fake.seen, default=str))
        self.assertFalse(result["usage"]["complete"])
        self.assertEqual(result["usage"]["unresolved_requests"], 1)

    async def test_wrong_event_code_is_rejected(self):
        box = toolbox()
        with self.assertRaisesRegex(ValueError, "does not match"):
            await runtime.run_agent(box, "Check faults", "", "", model_override=ScriptedModel(scenario_steps(box, code="OVERVOLTAGE")))

    async def test_skipping_required_tools_is_rejected(self):
        box = toolbox()
        with self.assertRaisesRegex(ValueError, "must inspect"):
            await runtime.run_agent(box, "Check faults", "", "", model_override=ScriptedModel(scenario_steps(box, include_required=False)))

    async def test_max_turns_stops_actual_sdk_tool_loop(self):
        box = toolbox()
        fake = ScriptedModel([("tool", ("summarize_log", {}))])
        with self.assertRaises(MaxTurnsExceeded):
            await runtime.run_agent(box, "Keep checking", "", "", max_turns=4, model_override=fake)
        self.assertEqual(len(fake.seen), 4)
        self.assertEqual(len(box.calls), 4)

    async def test_invalid_arguments_return_safe_tool_error_through_runner(self):
        box = toolbox()
        steps = scenario_steps(box)
        steps.insert(0, ("tool", ("get_evidence", {"evidence_id": "untrusted-test-marker"})))
        fake = ScriptedModel(steps)
        await runtime.run_agent(box, "Check faults", "", "", model_override=fake)
        output = next(item["output"] for item in fake.seen[1]["input"] if item.get("type") == "function_call_output")
        self.assertNotIn("untrusted-test-marker", str(output))
        self.assertEqual(box.calls[0]["status"], "failed")

    async def test_per_run_key_is_forwarded_to_private_client_and_never_global(self):
        sentinel = "TEST_ONLY_PER_RUN_CREDENTIAL"
        box = toolbox()
        fake = ScriptedModel(scenario_steps(box))
        client = SimpleNamespace(close=AsyncMock())
        before_key = _openai_shared.get_default_openai_key()
        before_client = _openai_shared.get_default_openai_client()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "TEST_ONLY_OTHER_USER", "OPENAI_BASE_URL": "https://invalid.local"}), \
             patch("bms_agent.runtime.AsyncOpenAI", return_value=client) as factory, \
             patch("bms_agent.runtime.OpenAIResponsesModel", return_value=fake) as model_factory, \
             patch("bms_agent.runtime.RunConfig", wraps=runtime.RunConfig) as config:
            result = await runtime.run_agent(box, "Check faults", "test-user-model", sentinel)
        factory.assert_called_once_with(api_key=sentinel, base_url="https://api.openai.com/v1", max_retries=0, timeout=30.0)
        model_factory.assert_called_once_with(model="test-user-model", openai_client=client)
        client.close.assert_awaited_once()
        self.assertTrue(config.call_args.kwargs["tracing_disabled"])
        self.assertFalse(config.call_args.kwargs["trace_include_sensitive_data"])
        self.assertEqual(_openai_shared.get_default_openai_key(), before_key)
        self.assertIs(_openai_shared.get_default_openai_client(), before_client)
        self.assertNotIn(sentinel, json.dumps(fake.seen, default=str))
        self.assertNotIn(sentinel, json.dumps(result))
        self.assertFalse(result["usage"]["simulated"])

    async def test_private_client_is_closed_when_model_run_fails(self):
        client = SimpleNamespace(close=AsyncMock())
        with patch("bms_agent.runtime.AsyncOpenAI", return_value=client), \
             patch("bms_agent.runtime.OpenAIResponsesModel", return_value=ScriptedModel([])), \
             patch("bms_agent.runtime.Runner.run", new=AsyncMock(side_effect=RuntimeError("Synthetic provider failure"))):
            with self.assertRaises(RuntimeError):
                await runtime.run_agent(toolbox(), "Check faults", "test-model", "TEST_ONLY_PER_RUN_CREDENTIAL")
        client.close.assert_awaited_once()

    async def test_invalid_limits_and_missing_key_fail_before_client_creation(self):
        with patch("bms_agent.runtime.AsyncOpenAI") as client:
            with self.assertRaises(ValueError):
                await runtime.run_agent(toolbox(), "Check faults", "test-model", "")
            with self.assertRaises(ValueError):
                await runtime.run_agent(toolbox(), "Check faults", "test-model", "TEST_ONLY", max_turns=3)
        client.assert_not_called()

    def test_real_sdk_tool_and_output_schemas_are_strict(self):
        agent = runtime.build_agent(toolbox(), ScriptedModel([]))
        self.assertEqual({tool.name for tool in agent.tools}, {"summarize_log", "list_protection_rules", "detect_faults", "inspect_window", "get_evidence", "simulate_rule"})
        for tool in agent.tools:
            with self.subTest(tool=tool.name):
                schema = tool.params_json_schema
                self.assertTrue(tool.strict_json_schema)
                self.assertEqual(schema["type"], "object")
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(schema.get("required", [])), set(schema["properties"]))
        output = AgentOutputSchema(agent.output_type)
        self.assertTrue(output.is_strict_json_schema())
        schema = output.json_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), {"summary", "findings", "limitations", "coverage"})
        finding = schema["$defs"]["Finding"]
        self.assertFalse(finding["additionalProperties"])
        self.assertEqual(set(finding["required"]), {"code", "evidence_ids", "facts", "explanation"})


if __name__ == "__main__":
    unittest.main()
