"""Local BYOK boundary tests; inject a runtime stub and never contact an API."""

import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bms_agent import cli
from bms_agent.samples import write_sample


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_KEY = "TEST_ONLY_ENVIRONMENT_CREDENTIAL"
PROMPT_KEY = "TEST_ONLY_PROMPT_CREDENTIAL"


class CLITests(unittest.TestCase):
    def setUp(self):
        # Explicit project-local temp storage works in restricted Windows sandboxes.
        self.temp = tempfile.TemporaryDirectory(prefix=".test-cli-", dir=PROJECT_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.csv = write_sample(self.directory / "overcurrent.csv", "overcurrent")
        self.output = self.directory / "reports"
        self.environment = patch.dict(os.environ, {"OPENAI_API_KEY": "", "OPENAI_MODEL": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main([str(self.csv), "--output", str(self.output), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    def payload(self):
        paths = list(self.output.glob("*/report.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text(encoding="utf-8"))

    def report_text(self):
        return "\n".join(path.read_text(encoding="utf-8") for path in self.output.rglob("*") if path.is_file())

    def patch_runtime(self, callable):
        module = types.ModuleType("bms_agent.runtime")
        module.run_agent = callable
        return patch.dict(sys.modules, {"bms_agent.runtime": module})

    def outcome(self, box, leak_marker=""):
        box.invoke("summarize_log")
        box.invoke("list_protection_rules")
        box.invoke("detect_faults")
        event = box.analysis["events"][0]
        return {"conclusion": {"summary": "Model analysis complete " + leak_marker,
                "findings": [{"code": event["code"], "evidence_ids": [event["evidence_id"]], "explanation": "Evidence verified " + leak_marker}],
                "limitations": ["Synthetic analysis " + leak_marker]},
                "usage": {"requests": 4, "input_tokens": 123, "output_tokens": 45, "total_tokens": 168, "simulated": False}}

    def test_default_offline_never_invokes_runtime_even_when_key_is_present(self):
        fake = AsyncMock(side_effect=AssertionError("Offline mode cannot call models"))
        with patch.dict(os.environ, {"OPENAI_API_KEY": ENV_KEY}), self.patch_runtime(fake):
            code, stdout, stderr = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        fake.assert_not_called()
        payload = self.payload()
        self.assertEqual(payload["mode"], "offline")
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["usage"]["requests"], 0)
        self.assertEqual(payload["usage"]["total_tokens"], 0)
        self.assertIn("offline", stdout)
        self.assertNotIn(ENV_KEY, stdout + stderr + self.report_text())

    def test_agent_missing_key_fails_before_runtime_or_report(self):
        fake = AsyncMock()
        with self.patch_runtime(fake):
            code, stdout, stderr = self.invoke("--mode", "agent", "--model", "test-model")
        self.assertEqual(code, 2)
        fake.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertIn("OPENAI_API_KEY", stderr)
        self.assertNotIn("状态：complete", stdout)

    def test_missing_model_fails_without_prompting_for_key(self):
        with patch("bms_agent.cli.getpass.getpass") as prompt:
            code, _, stderr = self.invoke("--mode", "agent", "--prompt-key")
        self.assertEqual(code, 2)
        prompt.assert_not_called()
        self.assertIn("OPENAI_MODEL", stderr)

    def test_prompt_key_is_ephemeral_and_overrides_environment_for_this_run(self):
        async def successful(box, question, model, key, *limits, **options):
            self.assertEqual(key, PROMPT_KEY)
            self.assertEqual(model, "test-user-model")
            self.assertNotIn(PROMPT_KEY, question)
            box.calls.append({"tool": "synthetic-log-entry", "arguments": {"marker": key}, "status": "complete", "result": {"marker": ENV_KEY}})
            return self.outcome(box, leak_marker=key + ENV_KEY)

        fake = AsyncMock(side_effect=successful)
        with patch.dict(os.environ, {"OPENAI_API_KEY": ENV_KEY}), \
             patch("bms_agent.cli.getpass.getpass", return_value=PROMPT_KEY) as prompt, self.patch_runtime(fake):
            before = dict(os.environ)
            code, stdout, stderr = self.invoke("--mode", "agent", "--model", "test-user-model", "--prompt-key", "--question", "Analyze " + PROMPT_KEY + ENV_KEY)
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(code, 0)
        prompt.assert_called_once()
        fake.assert_awaited_once()
        artifacts = stdout + stderr + self.report_text()
        self.assertNotIn(PROMPT_KEY, artifacts)
        self.assertNotIn(ENV_KEY, artifacts)
        self.assertIn("[REDACTED]", artifacts)
        payload = self.payload()
        self.assertEqual((payload["mode"], payload["status"]), ("agent", "complete"))
        self.assertEqual(payload["usage"]["total_tokens"], 168)
        self.assertFalse(payload["usage"]["simulated"])
        self.assertIn("key/project", payload["billing"])

    def test_failure_is_not_reported_as_success_or_zero_billed_tokens(self):
        fake = AsyncMock(side_effect=RuntimeError("Synthetic provider failure " + ENV_KEY))
        with patch.dict(os.environ, {"OPENAI_API_KEY": ENV_KEY}), self.patch_runtime(fake):
            code, stdout, stderr = self.invoke("--mode", "agent", "--model", "test-model")
        self.assertEqual(code, 1)
        fake.assert_awaited_once()
        payload = self.payload()
        self.assertEqual(payload["status"], "failed")
        self.assertNotIn("conclusion", payload)
        self.assertIsNone(payload["usage"]["requests"])
        self.assertIsNone(payload["usage"]["total_tokens"])
        self.assertEqual(payload["error_kind"], "RuntimeError")
        self.assertIn("failed", stdout)
        self.assertNotIn("状态：complete", stdout)
        self.assertTrue(stderr)
        self.assertNotIn(ENV_KEY, stdout + stderr + self.report_text())
        self.assertNotIn("Synthetic provider failure", self.report_text())

    def test_invalid_csv_is_checked_before_hidden_key_prompt(self):
        self.csv = self.directory / "does-not-exist.csv"
        with patch("bms_agent.cli.getpass.getpass") as prompt:
            code, _, _ = self.invoke("--mode", "agent", "--model", "test-model", "--prompt-key")
        self.assertEqual(code, 2)
        prompt.assert_not_called()

    def test_invalid_limits_do_not_prompt_or_invoke_runtime(self):
        fake = AsyncMock()
        with patch("bms_agent.cli.getpass.getpass") as prompt, self.patch_runtime(fake):
            code, _, _ = self.invoke("--mode", "agent", "--model", "test-model", "--prompt-key", "--max-turns", "3")
        self.assertEqual(code, 2)
        prompt.assert_not_called()
        fake.assert_not_called()

    def test_hidden_input_cancellation_does_not_write_reports(self):
        with patch("bms_agent.cli.getpass.getpass", side_effect=EOFError):
            code, _, _ = self.invoke("--mode", "agent", "--model", "test-model", "--prompt-key")
        self.assertEqual(code, 2)
        self.assertFalse(self.output.exists())

    def test_non_hidden_getpass_fallback_is_rejected(self):
        import getpass
        import warnings

        def unsafe_terminal(_prompt):
            warnings.warn("Synthetic terminal cannot hide input", getpass.GetPassWarning)
            raise AssertionError("The warning must stop fallback input before it is read")

        fake = AsyncMock()
        with patch("bms_agent.cli.getpass.getpass", side_effect=unsafe_terminal), self.patch_runtime(fake):
            code, stdout, stderr = self.invoke("--mode", "agent", "--model", "test-model", "--prompt-key")
        self.assertEqual(code, 2)
        fake.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertNotIn("Synthetic terminal", stdout + stderr)

    def test_prompt_key_requires_agent_mode(self):
        with patch("bms_agent.cli.getpass.getpass") as prompt:
            code, _, _ = self.invoke("--prompt-key")
        self.assertEqual(code, 2)
        prompt.assert_not_called()

    def test_repeated_runs_preserve_previous_artifacts(self):
        first, _, _ = self.invoke()
        second, _, _ = self.invoke()
        self.assertEqual((first, second), (0, 0))
        self.assertEqual(len(list(self.output.glob("*/report.json"))), 2)


if __name__ == "__main__":
    unittest.main()
