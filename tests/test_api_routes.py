"""Flask route coverage: read validation, SSE, device_info cache, write pacing."""
import queue
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
    charger_web._device_info_cache = None
    charger_web._device_info_cache_at = 0.0
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
    def test_device_info_served_from_cache(self, web_client, monkeypatch):
        calls = []

        def _fake_device_info(read_hook=None):
            calls.append(1)
            return {
                "manufacturer": "MEAN-WELL",
                "model": "NPB-1700-48",
                "serial": "TEST",
                "location": "TW",
                "firmware": "V01.05",
                "made": "250114",
                "vin": 230.4,
                "temp": 35.0,
            }

        monkeypatch.setattr(
            charger_web.charger, "device_info", _fake_device_info)
        charger_web._invalidate_device_info_cache()

        r1 = web_client.get("/api/device_info")
        r2 = web_client.get("/api/device_info")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.get_json()["model"] == "NPB-1700-48"
        assert len(calls) == 1


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
