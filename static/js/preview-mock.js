/**
 * Static preview shim — loaded only by preview.html.
 * Replaces fetch() and EventSource with in-memory demo data (no Flask, no CAN).
 */
(function () {
  'use strict';

  const REGISTERS = window.PREVIEW_REGISTERS || {};
  let reads = Object.assign({}, window.PREVIEW_READS || {});
  let operation = reads.operation?.raw ?? 1;

  const STATUS = {
    fault_status: { raw: 0, flags: [] },
    chg_status: {
      raw: 0x0004,
      flags: ['CVM (in CV mode)'],
    },
    system_status: {
      raw: 0x0002,
      flags: ['DC_OK (DC output normal)'],
    },
    curve_config: {
      raw: 0x0884,
      decoded: 'curve=customised, temp_comp=-3mV/C/cell, cv_timeout_action=cut-off, mode=charger, cv_timeout_en=off, cc_timeout_en=off, fv_timeout_en=off, restart_en=on',
    },
    system_config: {
      raw: 0x0002,
      decoded: 'power_on_state=ON, eeprom_writes=enabled',
    },
  };

  const DEVICE_INFO = {
    manufacturer: 'MEAN-WELL',
    model: 'NPB-1700-48',
    serial: 'DEMO0001',
    location: 'TW',
    firmware: 'V01.05',
    made: '250114',
    vin: 230.4,
    temp: 35.4,
  };

  function jsonResponse(body, status) {
    return Promise.resolve(new Response(JSON.stringify(body), {
      status: status || 200,
      headers: { 'Content-Type': 'application/json' },
    }));
  }

  function parseWriteBody(opts) {
    try {
      return JSON.parse(opts.body).settings || {};
    } catch {
      return {};
    }
  }

  function applyWrites(settings) {
    for (const [name, value] of Object.entries(settings)) {
      const reg = REGISTERS[name];
      if (!reg) continue;
      let raw;
      if (reg.scale === 1) raw = value & 0xFFFF;
      else raw = Math.round(Number(value) / reg.scale);
      reads[name] = {
        raw,
        value: reg.scale === 1 ? raw : raw * reg.scale,
      };
      if (name === 'operation') operation = raw;
      if (name === 'curve_config') STATUS.curve_config.raw = raw;
    }
  }

  const realFetch = window.fetch.bind(window);
  window.fetch = function (input, opts) {
    opts = opts || {};
    const url = typeof input === 'string' ? input : input.url;
    const path = url.replace(/^https?:\/\/[^/]+/, '').split('?')[0];
    const qs = url.includes('?') ? new URL(url, 'http://local').searchParams : null;

    if (path === '/api/health') {
      return jsonResponse({ ok: true, demo: true, connected: true });
    }
    if (path === '/api/registers') {
      return jsonResponse(REGISTERS);
    }
    if (path === '/api/read') {
      const names = (qs.get('names') || '').split(',').map(s => s.trim()).filter(Boolean);
      const out = {};
      for (const name of names) {
        if (reads[name]) out[name] = reads[name];
      }
      return jsonResponse(out);
    }
    if (path === '/api/status') {
      return jsonResponse(STATUS);
    }
    if (path === '/api/device_info') {
      return jsonResponse(DEVICE_INFO);
    }
    if (path === '/api/operation') {
      return jsonResponse({ operation, on: operation === 1 });
    }
    if (path === '/api/on' && opts.method === 'POST') {
      operation = 1;
      reads.operation = { raw: 1, value: 1 };
      return jsonResponse({ ok: true });
    }
    if (path === '/api/off' && opts.method === 'POST') {
      operation = 0;
      reads.operation = { raw: 0, value: 0 };
      return jsonResponse({ ok: true });
    }
    if (path === '/api/write' && opts.method === 'POST') {
      const settings = parseWriteBody(opts);
      const wasOn = operation === 1;
      const cycle = opts.body && JSON.parse(opts.body).cycle !== false;
      if (cycle && wasOn) operation = 0;
      applyWrites(settings);
      if (cycle && wasOn) operation = 1;
      const post = {};
      for (const name of Object.keys(settings)) {
        post[name] = reads[name] || null;
      }
      return jsonResponse({
        ok: true,
        wrote: settings,
        post,
        cycled: cycle && wasOn,
        was_on: wasOn,
      });
    }
    return realFetch(input, opts);
  };

  /** Minimal EventSource mock for /api/stream */
  class PreviewEventSource {
    constructor(url) {
      this.url = url;
      this.listeners = {};
      this.onerror = null;
      this._closed = false;
      const self = this;
      setTimeout(() => {
        if (self._closed) return;
        self._emit('open', {});
        self._tick();
      }, 50);
    }
    addEventListener(type, fn) {
      (this.listeners[type] = this.listeners[type] || []).push(fn);
    }
    close() {
      this._closed = true;
      if (this._timer) clearInterval(this._timer);
    }
    _emit(type, data) {
      const ev = { type, data: data ? JSON.stringify(data) : '' };
      (this.listeners[type] || []).forEach(fn => fn(ev));
    }
    _tick() {
      const self = this;
      const push = () => {
        if (self._closed) return;
        self._emit('state', {
          connected: true,
          demo: true,
          operation,
          latency_ms: 4 + Math.random() * 8,
          fail_streak: 0,
          ts: Date.now() / 1000,
          chg_status: STATUS.chg_status.flags,
          fault_status: STATUS.fault_status.flags,
          system_status: STATUS.system_status.flags,
        });
      };
      push();
      this._timer = setInterval(push, 3000);
    }
  }

  const NativeEventSource = window.EventSource;
  window.EventSource = function (url) {
    if (String(url).indexOf('/api/stream') !== -1) {
      return new PreviewEventSource(url);
    }
    return new NativeEventSource(url);
  };

  document.documentElement.classList.add('preview-mode');
})();
