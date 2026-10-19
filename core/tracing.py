"""Langfuse tracing. One trace per run; LangChain calls nest under it via the callback handler.
No-op when LANGFUSE_PUBLIC_KEY is unset, so local runs work without an account."""
import os
from contextlib import contextmanager


def enabled() -> bool:
    return bool(os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"))


class Trace:
    def __init__(self, client=None, span=None):
        self.client, self.span = client, span
        self.callbacks = []
        self.url = None

    def score(self, name: str, value: float, comment: str | None = None):
        if self.span:
            self.span.score_trace(name=name, value=value, comment=comment)

    def output(self, value):
        if self.span:
            self.span.update(output=value)


@contextmanager
def trace(name: str, *, run_id: str, tags: list[str], metadata: dict):
    if not enabled():
        yield Trace()
        return
    from langfuse import get_client, propagate_attributes
    from langfuse.langchain import CallbackHandler

    client = get_client()
    try:
        with client.start_as_current_observation(name=name, as_type="agent", metadata=metadata) as span, \
                propagate_attributes(trace_name=name, session_id=run_id, tags=tags,
                                     version=metadata.get("git_sha"), metadata={"run_id": run_id}):
            t = Trace(client, span)
            t.callbacks = [CallbackHandler()]
            t.url = client.get_trace_url(trace_id=client.get_current_trace_id())
            yield t
    finally:
        client.flush()
