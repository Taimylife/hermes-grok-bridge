import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bridge = load("grok_bridge_client", ROOT / "grok" / "bridge.py")
sender = load("hermes_grok_sender", ROOT / "skills/grok-bridge/scripts/grok_send.py")


class BridgeBasicsTests(unittest.TestCase):
    def test_a2a_message_keeps_explicit_sender_and_thread(self):
        message = bridge.a2a_message("grok-research", "thread-123", "hello")
        self.assertEqual(message["contextId"], "thread-123")
        self.assertEqual(message["metadata"]["sender"], "grok-research")
        self.assertIn("hello", message["parts"][0]["text"])

    def test_context_attachment_is_size_limited_and_not_logged(self):
        marker = "confidential-source-text"
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "context.txt"
            source.write_text(marker, encoding="utf-8")
            attached, metadata = bridge._context_attachment(str(source))
            self.assertIn(marker, attached)
            self.assertEqual(metadata["bytes"], len(marker.encode()))
            source.write_bytes(b"x" * (bridge.MAX_CONTEXT_FILE_BYTES + 1))
            with self.assertRaisesRegex(ValueError, "limit"):
                bridge._context_attachment(str(source))

    def test_unknown_grok_identity_is_rejected_before_dispatch(self):
        config = types.SimpleNamespace(agents={"research": {"url": "https://example.invalid"}},
                                       token=lambda _agent: "test-token", max_per_hour=0)
        args = types.SimpleNamespace(slug="research", sender="grok-stranger", message="hello",
                                     context_file=None)
        with patch.object(bridge, "http_request") as request, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(bridge.cmd_ask(config, args), 2)
        request.assert_not_called()

    def test_hermes_sender_fails_closed_without_explicit_recipient_route(self):
        values = {"GROK_BRIDGE_FROM": "hermes-research"}
        with patch.dict(os.environ, values, clear=True), contextlib.redirect_stderr(io.StringIO()):
            code = sender.main(["--to", "grok-operations", "--text", "test", "--dry-run"])
        self.assertEqual(code, 2)

    def test_webhook_transport_error_does_not_print_url_or_key(self):
        url = "https://example.invalid/private-webhook-secret"
        key = "test-key-do-not-print"
        with tempfile.TemporaryDirectory() as directory:
            values = {"GROK_BRIDGE_FROM": "hermes-research",
                      "GROK_BRIDGE_WEBHOOK_URL__GROK_RESEARCH": url,
                      "GROK_BRIDGE_WEBHOOK_KEY__GROK_RESEARCH": key,
                      "GROK_BRIDGE_STATE_DIR": directory}
            stderr = io.StringIO()
            with patch.dict(os.environ, values, clear=True), \
                 patch.object(sender.urllib.request, "urlopen", side_effect=OSError(f"could not reach {url}")), \
                 contextlib.redirect_stderr(stderr):
                code = sender.main(["--to", "grok-research", "--text", "hello"])
        self.assertEqual(code, 1)
        self.assertNotIn(url, stderr.getvalue())
        self.assertNotIn(key, stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
