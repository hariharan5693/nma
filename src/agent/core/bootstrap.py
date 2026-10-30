"""Startup bootstrap for local development against PMI internal certificates.

The LangSmith SDK uploads traces using its OWN HTTP client, which does not honor
the httpx client we pass to ChatOpenAI. When NMA_VERIFY_SSL is false (local dev
behind PMI's internal CA), we patch the LangSmith client to skip verification so
trace export does not raise SSLCertVerificationError.

This is a NO-OP when verification is enabled (i.e. inside the deployed platform,
where tracing is handled automatically and certs are trusted).

Import this module once, early (graph.py does so at import time).
"""

from __future__ import annotations

import os
import warnings

_APPLIED = False


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def apply_local_ssl_workaround() -> None:
    """Disable SSL verification for the LangSmith SDK client when requested."""
    global _APPLIED
    if _APPLIED:
        return
    _APPLIED = True

    verify = _as_bool(os.environ.get("NMA_VERIFY_SSL"), default=True)
    if verify:
        return  # trust the cert chain — nothing to do (deployed path)

    # Silence noisy InsecureRequestWarning from urllib3.
    try:
        import urllib3

        urllib3.disable_warnings()
    except Exception:  # noqa: BLE001
        pass

    # Patch langsmith.Client to use a requests.Session with verify=False.
    # (This SDK version accepts a `session=` kwarg rather than `verify=`.)
    try:
        import inspect

        import langsmith.client as _ls_client
        import requests

        params = inspect.signature(_ls_client.Client.__init__).parameters
        _orig_init = _ls_client.Client.__init__

        if "session" in params:
            def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                if kwargs.get("session") is None:
                    sess = requests.Session()
                    sess.verify = False
                    kwargs["session"] = sess
                _orig_init(self, *args, **kwargs)

            _ls_client.Client.__init__ = _patched_init  # type: ignore[method-assign]
        elif "verify" in params:
            def _patched_init(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                kwargs.setdefault("verify", False)
                _orig_init(self, *args, **kwargs)

            _ls_client.Client.__init__ = _patched_init  # type: ignore[method-assign]
        else:
            warnings.warn(
                "LangSmith Client has no session/verify param — trace upload may fail "
                "against PMI internal certs.",
                stacklevel=2,
            )
    except Exception as exc:  # noqa: BLE001
        warnings.warn(f"Could not patch LangSmith client for SSL: {exc}", stacklevel=2)
