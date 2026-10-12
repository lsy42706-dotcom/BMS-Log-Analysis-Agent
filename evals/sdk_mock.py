"""Optional real Agents Runner integration driven by a local scripted Model."""

from __future__ import annotations

import json
from typing import Any

from .graders import model_payload


def parse_tool_output(output: Any) -> dict[str, Any] | None:
    """Accept explicit JSON or an SDK-native dict, never Python repr/eval.

    Fixed error strings and other output content are not successful evidence.
    Malformed serialization must fail the workflow instead of silently passing
    empty-finding fixtures.
    """
    try:
        data = json.loads(output) if isinstance(output, str) else output
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def make_mock_model() -> Any:
    # Optional imports live here: the default evaluation remains stdlib-only.
    from agents.items import ModelResponse
    from agents.models.interface import Model
    from agents.usage import Usage
    from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

    class ToolReflectionModel(Model):
        """No endpoint/client: copies only events actually returned by SDK tools."""

        def __init__(self):
            self.turns = 0
            self.saw_sdk_tool_output = False
            self.events: list[dict[str, Any]] = []
            self.total_events = 0
            self.detection_received = False

        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, *, previous_response_id,
                               conversation_id, prompt):
            turn = self.turns
            self.turns += 1
            for item in input:
                if isinstance(item, dict) and item.get("type") == "function_call_output":
                    self.saw_sdk_tool_output = True
                    data = parse_tool_output(item.get("output"))
                    if (data is not None and isinstance(data.get("events"), list)
                            and isinstance(data.get("total"), int) and not isinstance(data["total"], bool)
                            and data["total"] >= len(data["events"])):
                        self.events = data["events"]
                        self.total_events = data["total"]
                        self.detection_received = True
            required = ("summarize_log", "list_protection_rules", "detect_faults")
            if turn < 3:
                name = required[turn]
                arguments = {"code": "all", "offset": 0, "limit": 20} if name == "detect_faults" else {}
                output = [ResponseFunctionToolCall(name=name, arguments=json.dumps(arguments),
                          call_id=f"eval-call-{turn}", id=f"eval-item-{turn}", type="function_call")]
            else:
                if not self.detection_received:
                    raise ValueError("SDK mock did not consume a valid JSON/dict detection result")
                payload = model_payload(self.events, "full" if len(self.events) == self.total_events else "partial")
                output = [ResponseOutputMessage(id=f"eval-message-{turn}", role="assistant", status="completed",
                          type="message", content=[ResponseOutputText(text=json.dumps(payload), type="output_text", annotations=[])])]
            # Requests are simulated SDK turns; token fields are not measured.
            return ModelResponse(output=output, usage=Usage(requests=1, input_tokens=0, output_tokens=0, total_tokens=0), response_id=None)

        async def stream_response(self, *args, **kwargs):
            raise AssertionError("Offline evals must not stream or access an API")
            yield

    return ToolReflectionModel()
