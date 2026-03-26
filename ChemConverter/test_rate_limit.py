# test_rate_limit.py
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import cas_client  # your updated file

def main():
    # Create client (override token + HTTP session to avoid network)
    client = cas_client.CASClient()
    client.settings.rate_limit_per_sec = 1.0  # expect ~1 request/sec
    client.settings.server = "https://example.com"  # dummy base

    # Avoid token fetch
    client.auth.get_token = lambda: {"token_type": "Bearer", "access_token": "dummy"}

    # Collect the times when POST is actually executed
    call_times = []

    class DummyResp:
        def __init__(self):
            self.status_code = 200
            self.text = "{}"
        def raise_for_status(self): pass
        def json(self): return {}

    # Monkeypatch the real HTTP to a fast fake that logs time
    def fake_post(url, data=None, verify=True):
        call_times.append(time.monotonic())
        return DummyResp()

    client.http.session.post = fake_post

    # Fire N concurrent calls through the *same* RequestHandler (important!)
    N = 5
    print(f"Firing {N} concurrent POSTs; limiter is 1 req/sec; expect ~{N-1} seconds total.")

    def do_call(i):
        # Use the RequestHandler directly so we hit the limiter in .post()
        return client.http.post(url=f"{client._server}/search/substances", json_body={"i": i})

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=N) as pool:
        futs = [pool.submit(do_call, i) for i in range(N)]
        for _ in as_completed(futs):
            pass
    ended = time.monotonic()

    # Analyze spacing
    call_times.sort()
    deltas = [round(call_times[i] - call_times[i-1], 3) for i in range(1, len(call_times))]

    print("Call times (monotonic):", [round(t - call_times[0], 3) for t in call_times])
    print("Inter-call deltas (s): ", deltas)
    print(f"Total elapsed: {round(ended - started, 3)} s")

    # Simple assertions (allow a little jitter)
    assert len(call_times) == N
    assert all(d >= 0.95 for d in deltas), "Inter-call gaps are too small—rate limit not enforced"
    print("✅ Rate limit appears enforced (≥ ~1s between calls).")

if __name__ == "__main__":
    main()
