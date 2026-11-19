"""Keep test runs from writing into logs/calls."""

import os
import tempfile

os.environ.setdefault("CALL_LOG_DIR", tempfile.mkdtemp(prefix="voicebot-tests-"))
