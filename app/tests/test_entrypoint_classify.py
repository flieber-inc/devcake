"""classify_harness_failure units (docs/15 §1, §4): a nonzero harness exit is
DEV_AUTH (12) only on credential wording, else DEV_CRASH (10). The grok CLI's
revoked-session wording ("Not signed in", "run `grok login`") must map to 12 —
the historical miss made revoked grok creds burn three DEV_CRASH attempts
instead of tripping the DEV_AUTH breaker once."""

import importlib.util
import os
from pathlib import Path

# host checkout: repo/app/tests → repo/images/common; app container: /srv/tests
# → /srv/images/common (read-only compose mount)
_CANDIDATES = [Path(__file__).parents[2] / "images" / "common" / "dev_entrypoint.py",
               Path(__file__).parents[1] / "images" / "common" / "dev_entrypoint.py"]
ENTRYPOINT = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])

# module reads these at import; redis.from_url is lazy (no connection until
# use). Restore the env afterwards — leaking REDIS_URL here would repoint the
# live messaging tests (same pytest process) at a nonexistent server.
_ENV_KEYS = ("DEVCAKE_RUN_ID", "REDIS_URL", "REDIS_USER", "REDIS_PASSWORD")
_saved = {k: os.environ.get(k) for k in _ENV_KEYS}
os.environ.setdefault("DEVCAKE_RUN_ID", "T-1-1-EXECUTE-AAAAAA")
os.environ.setdefault("REDIS_URL", "redis://localhost:6399/0")
os.environ.setdefault("REDIS_USER", "test")
os.environ.setdefault("REDIS_PASSWORD", "test")

spec = importlib.util.spec_from_file_location("dev_entrypoint_classify", ENTRYPOINT)
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)

for _k, _v in _saved.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


def test_grok_revoked_session_is_auth():
    assert ep.classify_harness_failure("Error: Not signed in.") == 12
    assert ep.classify_harness_failure(
        "You are signed out. Run `grok login` to continue.") == 12
    assert ep.classify_harness_failure("Please sign in to continue") == 12


def test_legacy_markers_still_auth():
    assert ep.classify_harness_failure("401 Unauthorized") == 12
    assert ep.classify_harness_failure("authentication failed for user") == 12
    assert ep.classify_harness_failure("please log in again") == 12


def test_generic_crash_is_not_auth():
    assert ep.classify_harness_failure("Traceback (most recent call last): ...") == 10
    assert ep.classify_harness_failure("segmentation fault") == 10
    assert ep.classify_harness_failure("") == 10


def test_precision_no_bare_login_or_sign_in_substrings():
    # "login"/"sign in" alone are NOT markers — a crash while *processing* such
    # text must stay DEV_CRASH; a false 12 pauses the whole Dev Type (docs/15 §4)
    assert ep.classify_harness_failure("error rendering the login page fixture") == 10
    assert ep.classify_harness_failure("failed test: sign in button missing") == 10


def test_case_insensitive():
    assert ep.classify_harness_failure("NOT SIGNED IN") == 12
