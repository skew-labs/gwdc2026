import base64
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

class EntrypointSecretTests(unittest.TestCase):
    def test_owner_only_session_file_and_unconfigured_model_keep_service_available(self):
        try:
            import fastapi  # noqa: F401
        except ImportError:
            self.skipTest("service optional dependency not installed")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            secret = root / "session.key"
            secret.write_text(base64.b64encode(b"s" * 32).decode() + "\n")
            secret.chmod(0o600)
            environment = {"FINANCE_SERVICE_SESSION_KEY_ID": "key-one",
                "FINANCE_SERVICE_SESSION_HMAC_FILE": str(secret),
                "FINANCE_SERVICE_REFERENCE_SQLITE_PATH": str(root / "service.sqlite3")}
            with patch.dict(os.environ, environment, clear=True):
                sys.modules.pop("finance_service.entrypoint", None)
                module = importlib.import_module("finance_service.entrypoint")
                app = module.build_app()
            self.assertEqual(app.title, "GWDC Economic Service")

    def test_duplicate_session_sources_are_rejected(self):
        environment = {"FINANCE_SERVICE_SESSION_KEY_ID": "key-one",
            "FINANCE_SERVICE_SESSION_HMAC_B64": base64.b64encode(b"s" * 32).decode(),
            "FINANCE_SERVICE_SESSION_HMAC_FILE": "/tmp/duplicate",
            "FINANCE_SERVICE_REFERENCE_SQLITE_PATH": "/tmp/unused.sqlite3"}
        with patch.dict(os.environ, environment, clear=True):
            sys.modules.pop("finance_service.entrypoint", None)
            with self.assertRaisesRegex(RuntimeError, "exactly one"):
                importlib.import_module("finance_service.entrypoint")


if __name__ == "__main__":
    unittest.main()
