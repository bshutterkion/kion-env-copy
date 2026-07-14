import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kion.engine.read import list_records


class PagedClient:
    """Serves an {items,total} envelope one page at a time, keyed by the
    ``page`` param (page 1 is the initial param-free request)."""
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        page = (params or {}).get("page", 1)
        return self.pages[page - 1]


def test_list_records_collects_all_pages():
    pages = [
        {"items": [{"id": 1}, {"id": 2}], "total": 4},
        {"items": [{"id": 3}, {"id": 4}], "total": 4},
    ]
    c = PagedClient(pages)
    recs = list_records(c, "/beta/scope")
    assert [r["id"] for r in recs] == [1, 2, 3, 4]
    # first request is param-free (the bare-list contract both call sites rely on)
    assert c.calls[0] == ("/beta/scope", None)
    # a second, paged request was made
    assert len(c.calls) == 2 and c.calls[1][1]["page"] == 2


def test_list_records_bare_list_single_request():
    class C:
        def __init__(self):
            self.calls = 0

        def get(self, path, params=None):
            self.calls += 1
            return [{"id": 1}, {"id": 2}]
    c = C()
    assert len(list_records(c, "/v3/ou")) == 2
    assert c.calls == 1  # bare list -> no paging


def test_list_records_data_envelope_without_total_single_request():
    class C:
        def __init__(self):
            self.calls = 0

        def get(self, path, params=None):
            self.calls += 1
            return {"data": [{"id": 1}]}
    c = C()
    assert len(list_records(c, "/x")) == 1
    assert c.calls == 1  # no 'total' -> single request


def test_list_records_swallows_api_error():
    from kion.client import KionAPIError

    class C:
        def get(self, path, params=None):
            raise KionAPIError(500, "GET", path, "boom")
    assert list_records(C(), "/x") == []


def test_list_records_calls_on_error_and_still_returns_empty():
    """A KionAPIError must be reported via on_error (so a target-side read
    failure isn't indistinguishable from 'genuinely zero records' — the
    finding this test guards against), but list_records still degrades to []
    rather than raising."""
    from kion.client import KionAPIError

    class C:
        def get(self, path, params=None):
            raise KionAPIError(503, "GET", path, "unavailable")

    seen = []

    def on_error(path, exc):
        seen.append((path, exc))

    result = list_records(C(), "/x", on_error=on_error)
    assert result == []
    assert len(seen) == 1
    assert seen[0][0] == "/x"
    assert isinstance(seen[0][1], KionAPIError)
    assert seen[0][1].status == 503


def test_list_records_on_error_none_still_returns_empty_without_raising():
    """Explicitly passing on_error=None (or omitting it) preserves the
    original silent-degrade behavior for callers that don't opt in."""
    from kion.client import KionAPIError

    class C:
        def get(self, path, params=None):
            raise KionAPIError(500, "GET", path, "boom")
    assert list_records(C(), "/x", on_error=None) == []
