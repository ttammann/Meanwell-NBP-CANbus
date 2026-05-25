"""Flask route coverage: read validation, SSE, device_info cache, write pacing."""
import queue
import threading
import time

import pytest

import charger_web
from charger_web import app, _stream_events


@pytest.fixture
def web_client(monkeypatch):
    monkeypatch.setattr(charger_web, "charger",
                        charger_web.MeanWellCharger(
                            can_id=0xC0103,
                            bus=charger_web.FakeBus(),
                            recv_timeout=0.05))
    monkeypatch.setattr(charger_web, "_bus_args", {"demo": True})
    charger_web._invalidate_device_info_cache()
    app.config["TESTING"] = True
    return app.test_client()


class TestApiReadValidation:
    def test_unknown_register_names_return_400(self, web_client):
        r = web_client.get("/api/read?names=curve_cc,nope_register")
        assert r.status_code == 400
        body = r.get_json()
        assert body["ok"] is False
        assert "nope_register" in body["error"]
        assert "curve_cc" in body["known"]


class TestApiStream:
    def test_stream_events_sse_format(self):
        q = queue.Queue(maxsize=1)
        q.put_nowait({
            "connected": True,
            "demo": True,
            "operation": 1,
            "latency_ms": 1.0,
            "fail_streak": 0,
            "ts": time.time(),
        })
        gen = _stream_events(q)
        chunk = next(gen)
        if isinstance(chunk, bytes):
            chunk = chunk.decode("utf-8")
        assert "event: state" in chunk
        assert '"demo": true' in chunk or '"demo":true' in chunk
        q.put_nowait(None)
        try:
            next(gen)
        except StopIteration:
            pass


class TestDeviceInfoCache:
    def test_identity_cached_live_telemetry_refreshed(self, web_client, monkeypatch):
        identity_calls = []
        vin_reads = []
        temp_reads = []

        def _fake_identity(read_hook=None):
            identity_calls.append(1)
            return {
                "manufacturer": "MEAN-WELL",
                "model": "NPB-1700-48",
                "serial": "TEST",
                "location": "TW",
                "firmware": "V01.05",
                "made": "250114",
            }

        def _fake_read(name):
            if name == "read_vin":
                vin_reads.append(1)
                return 2304, 230.4
            if name == "read_temp":
                temp_reads.append(1)
                return 354, 35.4
            raise KeyError(name)

        monkeypatch.setattr(
            charger_web.charger, "device_identity", _fake_identity)
        monkeypatch.setattr(
            charger_web.charger, "read_register", _fake_read)
        charger_web._invalidate_device_info_cache()

        r1 = web_client.get("/api/device_info")
        r2 = web_client.get("/api/device_info")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.get_json()["model"] == "NPB-1700-48"
        assert r1.get_json()["vin"] == pytest.approx(230.4, rel=1e-3)
        assert len(identity_calls) == 1
        assert len(vin_reads) == 2
        assert len(temp_reads) == 2


class TestApiWritePostRead:
    def test_write_post_reads_use_paced_helper(self, web_client, monkeypatch):
        paced_calls = []

        def _spy_paced(names):
            paced_calls.append(list(names))
            return {"curve_cc": {"raw": 1600, "value": 16.0}}

        monkeypatch.setattr(charger_web, "_read_registers_paced", _spy_paced)
        r = web_client.post(
            "/api/write",
            json={"settings": {"curve_cc": 16.0}, "cycle": False},
        )
        assert r.status_code == 200
        assert paced_calls == [["curve_cc"]]


# ---------------------------------------------------------------------------
# /api/status pacing — protocol §6.1 requires >=20 ms between requests
# ---------------------------------------------------------------------------


class TestApiStatusPacing:
    def test_status_uses_paced_helper(self, web_client, monkeypatch):
        """Status must read all 5 registers through the paced helper, not
        with 5 back-to-back lock acquisitions (which violated the manual
        and held _lock for the whole batch)."""
        paced_calls = []

        def _spy_paced(names):
            paced_calls.append(list(names))
            return {n: {"raw": 0, "value": 0} for n in names}

        monkeypatch.setattr(charger_web, "_read_registers_paced", _spy_paced)
        r = web_client.get("/api/status")
        assert r.status_code == 200
        assert len(paced_calls) == 1
        assert set(paced_calls[0]) == {
            "fault_status", "chg_status", "system_status",
            "curve_config", "system_config",
        }


# ---------------------------------------------------------------------------
# /api/on, /api/off — readback so the response is authoritative
# ---------------------------------------------------------------------------


class TestApiOnOffReadback:
    def test_api_on_off_reflect_post_write_state(self, web_client):
        # FakeBus boots with operation=1; flip it off, then on, and check
        # the response field comes from a fresh read, not just a confirmation.
        r_off = web_client.post("/api/off")
        assert r_off.status_code == 200
        body_off = r_off.get_json()
        assert body_off["ok"] is True
        assert body_off["operation"] == 0
        assert body_off["on"] is False

        r_on = web_client.post("/api/on")
        assert r_on.status_code == 200
        body_on = r_on.get_json()
        assert body_on["operation"] == 1
        assert body_on["on"] is True


# ---------------------------------------------------------------------------
# JSON Cache-Control — API responses must not be cached by the browser
# ---------------------------------------------------------------------------


class TestNoStoreOnJsonResponses:
    def test_api_health_is_no_store(self, web_client):
        r = web_client.get("/api/health")
        assert r.status_code == 200
        assert r.headers.get("Cache-Control") == "no-store"

    def test_api_registers_is_no_store(self, web_client):
        r = web_client.get("/api/registers")
        assert r.status_code == 200
        assert r.headers.get("Cache-Control") == "no-store"


# ---------------------------------------------------------------------------
# Device-info cache single-flight — concurrent misses share one CAN read
# ---------------------------------------------------------------------------


class TestDeviceIdentitySingleFlight:
    def test_concurrent_misses_only_hit_bus_once(self, monkeypatch):
        """Without the single-flight gate, two simultaneous /api/device_info
        requests during a cache miss would both read the bus.  This test
        forces a slow read and asserts the second waiter gets the cached
        result rather than its own CAN round-trip."""
        charger_web._invalidate_device_info_cache()
        identity_called = threading.Event()
        seen = []
        gate = threading.Event()

        def _slow_identity(read_hook=None):
            seen.append(1)
            identity_called.set()
            # Block briefly so a second waiter has time to enter the cache
            # check while we hold the single-flight lock.
            gate.wait(timeout=2.0)
            return {
                "manufacturer": "MEAN-WELL", "model": "NPB-1700-48",
                "serial": "X", "location": "TW",
                "firmware": "V01.05", "made": "250114",
            }

        monkeypatch.setattr(charger_web, "charger",
                            charger_web.MeanWellCharger(
                                can_id=0xC0103, bus=charger_web.FakeBus(),
                                recv_timeout=0.05))
        monkeypatch.setattr(charger_web.charger,
                            "device_identity", _slow_identity)

        results = []
        def _call():
            results.append(charger_web._cached_device_identity())

        t1 = threading.Thread(target=_call)
        t2 = threading.Thread(target=_call)
        t1.start()
        identity_called.wait(timeout=1.0)  # ensure t1 entered the slow path
        t2.start()
        time.sleep(0.05)                   # let t2 hit the lock
        gate.set()                         # release t1's CAN read
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)
        assert len(seen) == 1, "single-flight: only one CAN read for two waiters"
        assert len(results) == 2
        assert results[0]["model"] == "NPB-1700-48"
        assert results[0]["model"] == results[1]["model"]


# ---------------------------------------------------------------------------
# StateBroadcaster._fanout — must not mutate _subs while iterating it
# ---------------------------------------------------------------------------


class _AlwaysFullQueue(queue.Queue):
    """Queue that always reports full and rejects put_nowait — used to
    force the _fanout drop path so we can assert on the iteration
    safety, regardless of the underlying maxsize semantics."""
    def full(self):
        return True
    def put_nowait(self, item):
        raise queue.Full()
    def get_nowait(self):
        raise queue.Empty()


class TestFanoutDoesNotMutateDuringIteration:
    def test_dropped_subscriber_does_not_skip_others(self):
        """If one queue rejects put_nowait, every *other* subscriber must
        still receive the payload on the same tick.  The old implementation
        removed mid-iteration, which could skip the next subscriber in the
        list."""
        b = charger_web.StateBroadcaster()
        bad = _AlwaysFullQueue()
        good1 = queue.Queue(maxsize=1)
        good2 = queue.Queue(maxsize=1)
        with b._subs_lock:
            b._subs.extend([good1, bad, good2])
        b._fanout({"connected": True, "operation": 1, "latency_ms": 1.0,
                   "fail_streak": 0, "ts": time.time()})
        # Both good queues must have received the payload despite `bad`
        # being between them in the list.
        assert good1.qsize() == 1
        assert good2.qsize() == 1
        # And the bad subscriber was pruned exactly once after the loop.
        assert bad not in b._subs
        assert good1 in b._subs
        assert good2 in b._subs
