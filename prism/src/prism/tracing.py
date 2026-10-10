"""Small Neatlogs lifecycle and span adapters."""

import contextlib
import os


@contextlib.contextmanager
def _noop_span():
    yield None


def _sdk():
    if not os.getenv("NEATLOGS_API_KEY"):
        return None
    import neatlogs
    return neatlogs


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
    return sdk.span(kind=kind, name=name)


def shutdown():
    sdk = _sdk()
    if sdk:
        sdk.flush()
        sdk.shutdown()
