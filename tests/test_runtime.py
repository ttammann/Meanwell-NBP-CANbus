"""Runtime-behaviour tests for the request/response loop, write_many
cycle preservation, range validation, and the SSE broadcaster.

Uses FakeBus from charger_web (the same in-process simulator the --demo
mode runs against) so these tests have no CAN-hardware dependency.

Run with:  pytest tests/
"""
from __future__ import annotations

import queue
import threading
import time

import pytest
import can

from charger_app import MeanWellCharger, REGISTERS, RANGES, main as cli_main
from charger_web import (
    FakeBus, StateBroadcaster, app, _parse_write_request,
    _read_registers_paced,
)
import charger_web


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_charger(bus=None, recv_timeout=0.5):
    return MeanWellCharger(can_id=0xC0103, bus=bus or FakeBus(),
                          recv_timeout=recv_timeout)


# ---------------------------------------------------------------------------
# FakeBus → MeanWellCharger round-trip framing
# ---------------------------------------------------------------------------

class TestRequestResponseFraming:
    def test_read_operation_returns_initial_on(self):
        mw = _make_charger()
        raw, scaled = mw.read_register("operation")
        # FakeBus boots with OPERATION = 1 (ON)
        assert raw == 1
        # Bitfield/integer registers have scale=1 so scaled == raw
        assert scaled == 1

    def test_read_vin_scaled_correctly(self):
        mw = _make_charger()
        raw, scaled = mw.read_register("read_vin")
        # FakeBus init: 0x50 = 2304 raw → 230.4 V scaled (scale=0.1).
        # FakeBus adds ±1 LSB drift to the live registers so the demo
        # dashboard looks alive, so allow a tiny tolerance here.
        assert 2302 <= raw <= 2306
        assert scaled == pytest.approx(230.4, abs=0.3)

    def test_read_curve_cv_two_byte_le(self):
        mw = _make_charger()
        raw, scaled = mw.read_register("curve_cv")
        # 5520 raw → 55.20 V (scale=0.01)
        assert raw == 5520
        assert scaled == pytest.approx(55.20, rel=1e-3)

    def test_read_curve_config_bitfield(self):
        mw = _make_charger()
        raw, scaled = mw.read_register("curve_config")
        # 0x0884 = charger mode + temp comp + RSTE — the "recommended" value
        assert raw == 0x0884
        assert scaled == 0x0884   # scale=1, so identical

    def test_unknown_register_raises_keyerror(self):
        mw = _make_charger()
        with pytest.raises(KeyError):
            mw.read_register("totally_made_up")

    def test_request_total_wait_is_bounded_by_recv_timeout(self):
        """A misbehaving bus that returns wrong-coded frames must not hold
        _lock for ``N × recv_timeout``; the loop is bounded by a single
        deadline.  Previously a 5-skip × 0.5 s timeout could block for
        2.5 s per request."""
        class _WrongCodeBus(FakeBus):
            """recv() returns a frame echoing the wrong command code so
            the _request loop never matches."""
            def recv(self, timeout=None):
                if self._last_request is None:
                    return None
                # Return a frame echoing 0xFF (a code we'll never request).
                self._last_request = None
                payload = [0xFF, 0xFF, 0, 0, 0, 0, 0, 0]
                return can.Message(arbitration_id=0xC0103,
                                   data=payload, is_extended_id=True)

        mw = MeanWellCharger(can_id=0xC0103, bus=_WrongCodeBus(),
                             recv_timeout=0.2)
        t0 = time.monotonic()
        raw, _ = mw.read_register("operation")
        elapsed = time.monotonic() - t0
        assert raw is None
        # Allow plenty of headroom for slow CI; the point is "well under
        # 5 × recv_timeout = 1.0 s".  The wrong-coded frames return
        # immediately so this should be near-instant on a healthy box.
        assert elapsed < 0.6, f"_request took {elapsed:.2f}s (bound was 0.2s)"

    def test_request_drains_stale_frames(self):
        """A stale frame already in the rx buffer should not be mistaken
        for the response to our next request — the framing protocol
        requires the response to echo our exact request code."""
        bus = FakeBus()
        # Pre-load a stale frame as if someone else's read response was
        # still sitting in the rx queue.  FakeBus implements this by
        # setting _last_request manually.
        bus._last_request = 0x60  # READ_VOUT
        mw = MeanWellCharger(can_id=0xC0103, bus=bus, recv_timeout=0.5)
        # Now ask for the operation register.  If the code mis-attributed
        # the stale READ_VOUT response, we'd get back 5418 instead of 1.
        raw, _ = mw.read_register("operation")
        assert raw == 1


# ---------------------------------------------------------------------------
# write_register + range validation
# ---------------------------------------------------------------------------

class TestWriteRegister:
    def test_write_curve_cc_round_trip(self):
        mw = _make_charger()
        mw.write_register("curve_cc", 20.0)   # scaled value → 2000 raw
        raw, scaled = mw.read_register("curve_cc")
        assert raw == 2000
        assert scaled == pytest.approx(20.0, rel=1e-3)

    def test_write_below_range_rejected(self):
        mw = _make_charger()
        # RANGES["curve_cc"] = (5.0, 25.0)
        with pytest.raises(ValueError, match="curve_cc must be in"):
            mw.write_register("curve_cc", 0.0)

    def test_write_above_range_rejected(self):
        mw = _make_charger()
        with pytest.raises(ValueError, match="curve_cv must be in"):
            mw.write_register("curve_cv", 99.0)   # ceiling is 58.4

    def test_write_to_read_only_register_rejected(self):
        mw = _make_charger()
        with pytest.raises(ValueError, match="is read-only"):
            mw.write_register("read_vout", 50.0)

    def test_writable_flag_matches_ranges_table(self):
        """Every register listed in RANGES must also be writable.  Catches
        the case where someone adds a range for a read-only register."""
        for name in RANGES:
            assert REGISTERS[name].writable, \
                f"{name} has a RANGE but is marked read-only"


# ---------------------------------------------------------------------------
# write_many ON/OFF cycle preservation — the safety property
# ---------------------------------------------------------------------------

class TestWriteManyCyclePreservation:
    def test_writes_apply_when_output_already_on(self):
        mw = _make_charger()
        # Boots ON; write a batch and confirm it stays ON afterwards.
        was_on = mw.write_many([("curve_cc", 18.0), ("curve_tc", 4.0)])
        assert was_on is True
        raw, _ = mw.read_register("operation")
        assert raw == 1, "output should still be ON after cycle"
        # Values committed
        assert mw.read_register("curve_cc")[1] == pytest.approx(18.0, rel=1e-3)
        assert mw.read_register("curve_tc")[1] == pytest.approx(4.0,  rel=1e-3)

    def test_output_stays_off_if_off_before_apply(self):
        """The safety property: a user with the output OFF must not have it
        silently re-energised when they save settings."""
        mw = _make_charger()
        mw.set_off()
        was_on = mw.write_many([("curve_cv", 53.6)])
        assert was_on is False
        raw, _ = mw.read_register("operation")
        assert raw == 0, "output must remain OFF after writes"
        assert mw.read_register("curve_cv")[1] == pytest.approx(53.6, rel=1e-3)

    def test_cycle_skipped_entirely_when_cycle_false(self):
        mw = _make_charger()
        mw.set_off()
        mw.write_many([("curve_fv", 55.2)], cycle=False)
        raw, _ = mw.read_register("operation")
        assert raw == 0

    def test_write_many_refuses_cycle_when_operation_unknown(self):
        """If we cannot read operation, do not guess output state and write."""
        mw = _make_charger()
        orig = mw.read_register

        def fake_read(name):
            if name == "operation":
                return None, None
            return orig(name)

        mw.read_register = fake_read
        with pytest.raises(ValueError, match="operation read failed"):
            mw.write_many([("curve_cc", 18.0)])

    def test_write_many_allows_no_cycle_when_operation_unknown(self):
        mw = _make_charger()
        orig = mw.read_register

        def fake_read(name):
            if name == "operation":
                return None, None
            return orig(name)

        mw.read_register = fake_read
        was_on = mw.write_many([("curve_cc", 18.0)], cycle=False)
        assert was_on is None
        assert mw.read_register("curve_cc")[1] == pytest.approx(18.0, rel=1e-3)

    def test_write_many_validates_before_any_write(self):
        """Invalid values in a batch must not commit earlier registers."""
        mw = _make_charger()
        mw.write_register("curve_cc", 10.0)
        with pytest.raises(ValueError, match="curve_cv"):
            mw.write_many([("curve_cc", 20.0), ("curve_cv", 999.0)])
        assert mw.read_register("curve_cc")[1] == pytest.approx(10.0, rel=1e-3)
        raw, _ = mw.read_register("operation")
        assert raw == 1


# ---------------------------------------------------------------------------
# StateBroadcaster — disconnect hysteresis + fan-out
# ---------------------------------------------------------------------------

class _FlakyBus(FakeBus):
    """FakeBus that fails the next N request/response cycles.

      * fail_n: the next N recv()s that follow a request return None
        (simulating a timeout — bus is healthy, charger didn't answer)
      * raise_n: the next N send()s raise can.CanError (simulating a
        broken bus — USB unplug, slcand crashed, etc.)

    The bus's drain-pre-reads (recv with no pending request) are NOT
    faulted because `_request` swallows drain errors and we want the
    fault to land on the actual request/response, not the cleanup loop."""

    def __init__(self, fail_n: int = 0, raise_n: int = 0):
        super().__init__()
        self._fail_n  = fail_n
        self._raise_n = raise_n

    def send(self, msg):
        if self._raise_n > 0:
            self._raise_n -= 1
            raise can.CanError("simulated bus drop")
        super().send(msg)

    def recv(self, timeout=None):
        # Only fault the response path — drain calls have _last_request==None
        # (nothing pending) and we let those return None naturally.
        if self._last_request is not None and self._fail_n > 0:
            self._fail_n -= 1
            self._last_request = None
            return None
        return super().recv(timeout)


@pytest.fixture
def patched_lock(monkeypatch):
    # The broadcaster grabs charger_web._lock — patch it to a lock we
    # control so tests are isolated from any running broadcaster.
    import charger_web
    monkeypatch.setattr(charger_web, "_lock", threading.Lock())
    return charger_web._lock


def _swap_charger(monkeypatch, bus):
    import charger_web
    mw = MeanWellCharger(can_id=0xC0103, bus=bus, recv_timeout=0.05)
    monkeypatch.setattr(charger_web, "charger", mw)
    # Disable reconnect (only relevant for real bus replacement)
    monkeypatch.setattr(charger_web, "_bus_args", {"demo": True})
    return mw


class TestStateBroadcaster:
    def test_subscribe_pushes_latest_payload(self, monkeypatch, patched_lock):
        _swap_charger(monkeypatch, FakeBus())
        b = StateBroadcaster()
        # Manually tick once instead of starting the thread, so the test
        # is deterministic.
        b._last_payload = b._read_once()
        q = b.subscribe()
        # Subscribe should immediately replay the last payload.
        evt = q.get(timeout=0.1)
        assert evt["connected"] is True
        assert evt["operation"] == 1
        assert isinstance(evt["latency_ms"], float)

    def test_disconnect_hysteresis_requires_two_failures(self, monkeypatch, patched_lock):
        """One timeout shouldn't flip the UI to disconnected; two in a row
        should.  This stops a single mid-write hiccup from blinking the
        chip red on a healthy bus."""
        _swap_charger(monkeypatch, _FlakyBus(fail_n=3))
        b = StateBroadcaster()
        # Seed the broadcaster as if it had previously been connected, so
        # the hysteresis can carry "last connected state" forward.
        b._last_payload = {"connected": True, "operation": 1,
                           "latency_ms": 1.0, "fail_streak": 0,
                           "ts": time.time()}

        # First failed read: hysteresis keeps connected=True
        p1 = b._read_once()
        assert p1["operation"]   is None
        assert p1["fail_streak"] == 1
        assert p1["connected"]   is True

        b._last_payload = p1
        # Second failed read: now we cross DISCONNECT_THRESHOLD
        p2 = b._read_once()
        assert p2["fail_streak"] == 2
        assert p2["connected"]   is False

    def test_recovers_after_failures(self, monkeypatch, patched_lock):
        _swap_charger(monkeypatch, _FlakyBus(fail_n=2))
        b = StateBroadcaster()
        b._last_payload = {"connected": True, "operation": 1, "latency_ms": 1.0,
                           "fail_streak": 0, "ts": time.time()}
        b._last_payload = b._read_once()   # fail #1
        b._last_payload = b._read_once()   # fail #2 -> flips disconnected
        assert b._last_payload["connected"] is False
        # Bus recovers on next read
        good = b._read_once()
        assert good["connected"]   is True
        assert good["fail_streak"] == 0
        assert good["operation"]   == 1

    def test_can_error_also_uses_hysteresis(self, monkeypatch, patched_lock):
        _swap_charger(monkeypatch, _FlakyBus(raise_n=3))
        b = StateBroadcaster()
        b._last_payload = {"connected": True, "operation": 1, "latency_ms": 1.0,
                           "fail_streak": 0, "ts": time.time()}
        p1 = b._read_once()
        assert "error" in p1
        assert p1["fail_streak"] == 1
        assert p1["connected"]   is True   # one error, still connected

        b._last_payload = p1
        p2 = b._read_once()
        assert p2["fail_streak"] == 2
        assert p2["connected"]   is False

    def test_multiple_subscribers_each_receive_event(self, monkeypatch, patched_lock):
        _swap_charger(monkeypatch, FakeBus())
        b = StateBroadcaster()
        b._last_payload = b._read_once()
        q1 = b.subscribe()
        q2 = b.subscribe()
        q3 = b.subscribe()
        # Initial replay should hit all three.
        for q in (q1, q2, q3):
            assert q.get(timeout=0.1)["connected"] is True

        # A fresh tick fans out to all three.
        new_payload = b._read_once()
        b._last_payload = new_payload
        for q in (q1, q2, q3):
            try: q.put_nowait(new_payload)
            except queue.Full: pass
        for q in (q1, q2, q3):
            evt = q.get(timeout=0.1)
            assert evt["operation"] == 1

    def test_unsubscribe_removes_queue(self, monkeypatch, patched_lock):
        _swap_charger(monkeypatch, FakeBus())
        b = StateBroadcaster()
        b._last_payload = b._read_once()
        q = b.subscribe()
        assert q in b._subs
        b.unsubscribe(q)
        assert q not in b._subs

    def test_fanout_keeps_latest_state_only(self, monkeypatch, patched_lock):
        """Per-subscriber queue depth is 1 — fan-out replaces stale state."""
        _swap_charger(monkeypatch, FakeBus())
        b = StateBroadcaster()
        q = b.subscribe()
        b._fanout({"connected": True, "operation": 1, "latency_ms": 1.0,
                   "fail_streak": 0, "ts": time.time()})
        b._fanout({"connected": True, "operation": 99, "latency_ms": 2.0,
                   "fail_streak": 0, "ts": time.time()})
        assert q.qsize() == 1
        assert q.get_nowait().get("operation") == 99


# ---------------------------------------------------------------------------
# CLI exit codes
# ---------------------------------------------------------------------------


class TestCliExitCodes:
    def test_check_exits_zero_when_bus_ok(self, monkeypatch):
        class _Stub:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def bus_ok(self): return True

        monkeypatch.setattr(
            "charger_app.MeanWellCharger", lambda **kw: _Stub())
        assert cli_main(["check"]) == 0

    def test_check_exits_one_when_no_response(self, monkeypatch):
        class _Stub:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def bus_ok(self): return False

        monkeypatch.setattr(
            "charger_app.MeanWellCharger", lambda **kw: _Stub())
        assert cli_main(["check"]) == 1


# ---------------------------------------------------------------------------
# POST /api/write validation
# ---------------------------------------------------------------------------


class TestParseWriteRequest:
    def test_rejects_non_object_body(self):
        with pytest.raises(ValueError, match="JSON object"):
            _parse_write_request(["curve_cc", 15])

    def test_rejects_non_object_settings(self):
        with pytest.raises(ValueError, match="settings must be an object"):
            _parse_write_request({"settings": [("curve_cc", 15)]})

    def test_rejects_null_settings(self):
        with pytest.raises(ValueError, match="settings must be an object"):
            _parse_write_request({"settings": None})

    def test_rejects_empty_settings(self):
        with pytest.raises(ValueError, match="must not be empty"):
            _parse_write_request({"settings": {}})

    def test_rejects_unknown_register(self):
        with pytest.raises(ValueError, match="unknown register"):
            _parse_write_request({"settings": {"nope": 1}})

    def test_rejects_read_only_register(self):
        with pytest.raises(ValueError, match="read-only"):
            _parse_write_request({"settings": {"read_vout": 50}})

    def test_accepts_valid_payload(self):
        settings, cycle = _parse_write_request(
            {"settings": {"curve_cc": 15.0}, "cycle": False})
        assert settings == [("curve_cc", 15.0)]
        assert cycle is False

    def test_rejects_non_boolean_cycle(self):
        with pytest.raises(ValueError, match="cycle must be a JSON boolean"):
            _parse_write_request({"settings": {"curve_cc": 15.0},
                                  "cycle": "false"})


@pytest.fixture
def web_client(monkeypatch, patched_lock):
    monkeypatch.setattr(charger_web, "charger",
                        _make_charger(recv_timeout=0.05))
    monkeypatch.setattr(charger_web, "_bus_args", {"demo": True})
    app.config["TESTING"] = True
    return app.test_client()


class TestReadRegistersPaced:
    def test_paced_batch_read(self, monkeypatch, patched_lock):
        monkeypatch.setattr(charger_web, "charger", _make_charger())
        out = _read_registers_paced(["operation", "curve_cc", "curve_cv"])
        assert out["operation"]["raw"] == 1
        assert out["curve_cc"]["value"] == pytest.approx(15.0, rel=1e-3)
        assert out["curve_cv"]["value"] == pytest.approx(55.20, rel=1e-3)


class TestApiHealth:
    def test_health_endpoint(self, web_client):
        r = web_client.get('/api/health')
        assert r.status_code == 200
        body = r.get_json()
        assert body['ok'] is True
        assert body['demo'] is True


class TestApiWriteEndpoint:
    def test_null_settings_returns_400(self, web_client):
        r = web_client.post("/api/write",
                            json={"settings": None})
        assert r.status_code == 400
        assert "object" in r.get_json()["error"]

    def test_empty_settings_returns_400(self, web_client):
        r = web_client.post("/api/write", json={"settings": {}})
        assert r.status_code == 400

    def test_valid_write_round_trips(self, web_client):
        r = web_client.post("/api/write",
                            json={"settings": {"curve_cc": 16.0},
                                  "cycle": False})
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["post"]["curve_cc"]["value"] == pytest.approx(16.0, rel=1e-3)


# ---------------------------------------------------------------------------
# CAN bus auto-reconnect
# ---------------------------------------------------------------------------


class TestBusReconnect:
    def test_try_reconnect_swaps_charger_instance(self, monkeypatch, patched_lock):
        old = _make_charger()
        monkeypatch.setattr(charger_web, "charger", old)
        monkeypatch.setattr(charger_web, "_bus_args", {
            "channel": "can0",
            "bitrate": 250000,
            "can_id": 0xC0103,
            "interface": "socketcan",
            "demo": False,
        })
        created = []

        def _factory(**kw):
            mw = _make_charger(recv_timeout=0.05)
            created.append(mw)
            return mw

        monkeypatch.setattr(charger_web, "MeanWellCharger", _factory)
        b = StateBroadcaster()
        b._err_streak = 4
        b._try_reconnect()
        assert len(created) == 1
        assert charger_web.charger is created[0]
        assert charger_web.charger is not old

    def test_try_reconnect_skipped_in_demo(self, monkeypatch, patched_lock):
        old = _make_charger()
        monkeypatch.setattr(charger_web, "charger", old)
        monkeypatch.setattr(charger_web, "_bus_args", {"demo": True})
        created = []
        monkeypatch.setattr(charger_web, "MeanWellCharger",
                            lambda **kw: created.append(1) or old)
        b = StateBroadcaster()
        b._try_reconnect()
        assert created == []
        assert charger_web.charger is old
