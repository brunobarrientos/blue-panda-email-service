"""Regression tests for the 2026-08-05..10 core dumps and lost PDF attachments.

`GmailClient` built its googleapiclient service **once** and shared it across
every request. Every endpoint here is a sync `def`, so FastAPI runs them in an
anyio threadpool -- real concurrency over one service object, one
`httplib2.Http`, one TLS socket. googleapiclient documents the service object as
not thread-safe, and there was no lock.

Two threads interleaving on one TLS record stream produced:

    ssl.SSLError: [SSL] record layer failure (_ssl.c:2660)

and, because that corruption happens inside OpenSSL's C state, repeated
`gmail-service.service: Failed with result 'core-dump'` -- 2-4 per day on star
from 2026-08-05 onward.

Sending a PDF is the case that suffers most: a base64 attachment holds the
shared socket for far longer than a one-line text body, so a `/health` or
`/profile` poller thread has a much wider window to corrupt the same stream
mid-upload. Star was being polled ~161 times per 5 minutes across four hosts,
and `/profile` calls Gmail on every single hit.

The transport must therefore be per-thread, and an SSL record-layer failure must
be treated as the transient fault it is rather than surfacing as a 502.
"""

import ssl
import threading

import pytest

from gmail_service.gmail_client import GmailClient, _is_retryable


class _FakeCreds:
    valid = True
    expired = False


def test_service_is_not_shared_between_threads(monkeypatch):
    """Each thread must get its own service, hence its own TLS socket."""
    built = []

    def fake_build(creds):
        marker = object()
        built.append(marker)
        return marker

    monkeypatch.setattr('gmail_service.gmail_client._build_service', fake_build)

    client = GmailClient(_FakeCreds())
    seen: dict[int, int] = {}
    barrier = threading.Barrier(2)

    def grab(idx: int) -> None:
        barrier.wait(timeout=5)
        seen[idx] = id(client._service)

    threads = [threading.Thread(target=grab, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(seen) == 2, 'both threads must have resolved a service'
    assert seen[0] != seen[1], (
        'both threads got the SAME service object -- concurrent Gmail calls '
        'share one non-thread-safe httplib2 TLS socket, which is what corrupted '
        'the record layer and core-dumped the process'
    )


def test_service_is_reused_within_one_thread(monkeypatch):
    """Thread-local must cache, not rebuild a TLS connection per property read."""
    calls = []

    def fake_build(creds):
        calls.append(1)
        return object()

    monkeypatch.setattr('gmail_service.gmail_client._build_service', fake_build)

    client = GmailClient(_FakeCreds())
    first = client._service
    second = client._service

    assert first is second
    assert len(calls) == 1, 'service was rebuilt within a single thread'


def test_ssl_record_layer_failure_is_retryable():
    """The exact error star raised must retry, not become a 502."""
    exc = ssl.SSLError('[SSL] record layer failure (_ssl.c:2660)')
    assert _is_retryable(exc), (
        'ssl.SSLError subclasses OSError, not ConnectionError, so it fell '
        'through the retryable tuple and lost the send outright'
    )
