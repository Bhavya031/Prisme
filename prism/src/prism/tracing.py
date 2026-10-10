"""Small Neatlogs lifecycle and span adapters."""

import contextlib
import logging
import os
import sys


@contextlib.contextmanager
def _noop_span():
    yield None


def logs_to_stderr():
    """Import Neatlogs with its logging moved off stdout, where it would corrupt JSON and markdown output."""
    import neatlogs
    for handler in logging.getLogger("neatlogs").handlers:
        if getattr(handler, "stream", None) in (sys.stdout, sys.__stdout__):
            handler.setStream(sys.stderr)
    return neatlogs


def _sdk():
    if not os.getenv("NEATLOGS_API_KEY"):
        return None
    return logs_to_stderr()


def init():
    sdk = _sdk()
    if sdk:
        sdk.init(api_key=os.environ["NEATLOGS_API_KEY"], endpoint="https://ingest.neatlogs.com", workflow_name="prism")


@contextlib.contextmanager
def workflow(name):
    sdk = _sdk()
    if not sdk:
        yield None
        return
    with sdk.trace(name, kind="WORKFLOW") as span:
        yield span


def step(name, kind):
    sdk = _sdk()
    if not sdk:
        return _noop_span()
    return sdk.trace(name, kind=kind)  # span() is decorator-only


def shutdown():
    sdk = _sdk()
    if sdk:
        sdk.flush()
        sdk.shutdown()
