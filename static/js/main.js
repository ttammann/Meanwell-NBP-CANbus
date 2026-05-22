// Settings shown in the editable table.  curve_config (0xB4) and chg_rst_vbat
// (0xB9) are NOT in this list — they're driven by the friendly checkbox/input
// row underneath the table.
const SETTING_KEYS = [
  'curve_cc', 'curve_cv', 'curve_fv', 'curve_tc',
  'curve_cc_timeout', 'curve_cv_timeout', 'curve_fv_timeout',
];
// Always read these two alongside the table values so we can populate
// the friendly row + preserve unmapped curve_config bits on write.
const FRIENDLY_KEYS = ['curve_config', 'chg_rst_vbat'];

// CURVE_CONFIG bit masks the friendly UI exposes.  Anything outside these
// masks is preserved verbatim on write.
const CFG_TCS_MASK = 0x000C;   // bits 2-3: temp comp slope (00=off, 01=-3mV)
const CFG_CVTSSE   = 0x0020;   // bit 5:   CV completion behavior (0=cut off, 1=float)
const CFG_CUVE     = 0x0080;   // bit 7:   charger mode
const CFG_CVTOE    = 0x0100;   // bit 8:   CV timeout enable
const CFG_CCTOE    = 0x0200;   // bit 9:   CC timeout enable
const CFG_FVTOE    = 0x0400;   // bit 10:  FV timeout enable
const CFG_RSTE     = 0x0800;   // bit 11:  restart-on-Vbat enable

let registers = {};
let currentValues = {};
let currentCurveConfig = null;     // raw 16-bit value last seen on the wire
let currentRestartV    = null;     // last seen chg_rst_vbat in volts

// Suggested 16S LFP values (README) — demo preview / curve fallback only.
const CURVE_DEFAULTS = { cc: 15.0, cv: 55.2, fv: 55.2, tc: 5.0 };
const SETTINGS_DEFAULTS = {
  curve_cc: 15.0, curve_cv: 55.2, curve_fv: 55.2, curve_tc: 5.0,
  curve_cc_timeout: 900, curve_cv_timeout: 60, curve_fv_timeout: 60,
  chg_rst_vbat: 48.0,
};
let isDemoMode = false;
let healthDemoResolved = false;
let curveDefaultsAllowed = false;
let hasSuggestedPreview = false;
let suggestedDefaultsTimer = null;
let hasReloadedFromCharger = false;
let demoAutoReloadDone = false;
let _reloadInFlight = false;
let _reloadQueued = false;
let _applyingSuggestedDefaults = false;
let _modalReturnFocus = null;

const SETTINGS_EXPORT_VERSION = 1;
const SETTINGS_EXPORT_KEYS = SETTING_KEYS.concat(FRIENDLY_KEYS);

// Activity log — kept in memory + mirrored to localStorage so a refresh
// (and the browser remembering session state) doesn't wipe context.
const LOG_KEY  = 'npb.log.v1';
const LOG_MAX  = 60;
let _logRows = [];

function _persistLog() {
  try {
    localStorage.setItem(LOG_KEY, JSON.stringify(_logRows.slice(0, LOG_MAX)));
  } catch { /* quota / privacy mode — silent */ }
}

function _renderLog() {
  const lg = document.getElementById('log');
  if (!lg) return;
  lg.innerHTML = _logRows.map(r => {
    const cls = 'row' + (r.kind ? ' ' + r.kind : '');
    // textContent semantics via DOM, not innerHTML, to avoid XSS from
    // CAN-error messages that could in theory contain HTML.
    const tmp = document.createElement('div');
    tmp.className = cls;
    const ts = document.createElement('span');
    ts.className = 'ts'; ts.textContent = r.ts;
    const msg = document.createElement('span');
    msg.textContent = r.msg;
    tmp.appendChild(ts); tmp.appendChild(msg);
    return tmp.outerHTML;
  }).join('');
}

function log(msg, kind) {
  const ts = new Date().toTimeString().slice(0,8);
  _logRows.unshift({ ts, msg, kind: kind || '' });
  if (_logRows.length > LOG_MAX) _logRows.length = LOG_MAX;
  _persistLog();
  _renderLog();
}

function _loadPersistedLog() {
  try {
    const raw = localStorage.getItem(LOG_KEY);
    if (raw) {
      _logRows = JSON.parse(raw);
      _renderLog();
    }
  } catch { /* silent */ }
}

function setConn(state, text, latencyMs) {
  const el = document.getElementById('conn');
  el.className = 'chip conn ' + state;
  document.getElementById('conn-text').textContent = text;
  updateOnoffHint(state === 'ok');
  const lat = document.getElementById('conn-lat');
  if (lat) {
    lat.textContent = (typeof latencyMs === 'number')
      ? `· ${Math.round(latencyMs)} ms`
      : '';
  }
  // Tooltip on hover gives the raw bits.
  el.title = (typeof latencyMs === 'number')
    ? `CAN read latency: ${latencyMs.toFixed(1)} ms`
    : '';
}

function _sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function fetchJSON(url, opts, retried) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    const retryAfter = parseInt(r.headers.get('Retry-After') || '0', 10);
    if (r.status === 503 && retryAfter > 0 && !retried) {
      await _sleep(retryAfter * 1000);
      return fetchJSON(url, opts, true);
    }
    const err = await r.json().catch(() => ({error: r.statusText}));
    throw new Error(err.error || r.statusText);
  }
  return r.json();
}

function updateOnoffHint(connected) {
  const hint = document.getElementById('onoff-hint');
  const sw = document.getElementById('onoff');
  if (!hint) return;
  if (!connected) {
    hint.textContent = 'Waiting for connection before output can be toggled.';
  } else if (sw && sw.disabled) {
    hint.textContent = 'Reading output state…';
  } else {
    hint.textContent = 'Toggles charger output. You will be asked to confirm.';
  }
}

function hideReloadBanner() {
  const b = document.getElementById('reload-banner');
  if (b) b.hidden = true;
}

function updatePreviewMeta(model) {
  const el = document.getElementById('preview-meta');
  if (!el) return;
  const m = model && String(model).trim() ? String(model).trim() : 'charger';
  el.textContent = `3-stage · ${m} · 48 V LFP`;
}

function updateConfigSummary() {
  const el = document.getElementById('config-summary');
  if (!el) return;
  if (currentCurveConfig === null) {
    el.textContent = 'Reload from charger to see active curve configuration.';
    return;
  }
  let text = _decodeCurveConfigBrief(currentCurveConfig);
  if (currentRestartV !== null) {
    text += ` · restart below ${currentRestartV.toFixed(1)} V`;
  }
  el.textContent = text;
}

function clearLog() {
  _logRows = [];
  _persistLog();
  _renderLog();
  log('log cleared', 'ok');
}

function fmt(v, decimals) {
  if (v === null || v === undefined) return '–';
  return Number(v).toFixed(decimals ?? 2);
}

function flashUpdate(el, newText, dim) {
  if (el.textContent !== newText) {
    el.textContent = newText;
    el.classList.remove('flash');
    void el.offsetWidth;
    el.classList.add('flash');
  }
  el.classList.toggle('dim', !!dim);
}

function parseInputValue(text, reg) {
  const t = text.trim();
  // Bitfield-style registers (scale=1, no unit) accept hex (0x...) or decimal.
  // Plain integer registers (scale=1, with unit, e.g. timeouts in min) are decimal.
  if (reg.scale === 1) {
    if (!reg.unit && t.toLowerCase().startsWith('0x')) return parseInt(t, 16);
    return parseInt(t, 10);
  }
  return parseFloat(t);
}

function formatInputValue(value, reg) {
  if (value === null || value === undefined) return '';
  if (reg.scale === 1) {
    // Bitfield (no unit) -> hex, integer (with unit) -> plain decimal
    return reg.unit
      ? String(Math.round(Number(value)))
      : '0x' + Number(value).toString(16).toUpperCase().padStart(4, '0');
  }
  return Number(value).toFixed(reg.scale < 1 ? 2 : 0);
}

// Maps timeout-register name -> the TOE checkbox id that gates it.
// The checkbox is rendered inline in the timeout row, but it controls
// the same curve_config bit the bottom config-row used to.
const TIMEOUT_TOE = {
  curve_cc_timeout: { id: 'cfg-cctoe', wrapId: 'check-cctoe-wrap', label: 'enabled' },
  curve_cv_timeout: { id: 'cfg-cvtoe', wrapId: 'check-cvtoe-wrap', label: 'enabled' },
  curve_fv_timeout: { id: 'cfg-fvtoe', wrapId: 'check-fvtoe-wrap', label: 'enabled' },
};

function buildSettingsTable() {
  const tbody = document.getElementById('settings');
  tbody.innerHTML = '';
  for (const name of SETTING_KEYS) {
    const reg = registers[name];
    if (!reg) continue;
    const row = document.createElement('tr');
    // Hex input only for "true" bitfields (scale=1 AND no unit).  Integer
    // registers like timeouts (scale=1, unit='min') get a normal number input.
    const isHex = reg.scale === 1 && !reg.unit;
    const inputType = isHex ? 'text' : 'number';
    const step = (reg.scale && reg.scale < 1) ? '0.01' : '1';
    const range = reg.range || [];
    const rangeText = range.length === 2 ? `${range[0]} – ${range[1]}` : '';
    const minMax = (range.length === 2 && !isHex) ? `min="${range[0]}" max="${range[1]}"` : '';
    // For timeout rows, render the corresponding TOE-enable checkbox inline.
    const toe = TIMEOUT_TOE[name];
    const toeCell = toe
      ? `<td class="toe-cell"><label class="check inline-check unloaded" id="${toe.wrapId}">
           <input type="checkbox" id="${toe.id}">
           <span class="box"></span>
           <span>${toe.label}</span>
         </label></td>`
      : `<td class="toe-cell"></td>`;
    row.innerHTML = `
      <td class="desc">${reg.desc}<span class="name">${name}</span></td>
      <td><input type="${inputType}" data-name="${name}" step="${step}" ${minMax}></td>
      <td class="unit-cell">${reg.unit || ''}</td>
      <td class="range-cell">${rangeText}</td>
      ${toeCell}
      <td class="raw-cell" data-raw="${name}"></td>
    `;
    tbody.appendChild(row);
  }
  // Wire input events only on the editable register inputs (those have data-name);
  // the inline TOE checkboxes are handled separately below.
  tbody.querySelectorAll('input[data-name]').forEach(inp => {
    inp.addEventListener('input', () => {
      onUserEditedBeforeReload();
      const name = inp.dataset.name;
      const orig = currentValues[name];
      const reg = registers[name];
      // Always start clean
      inp.classList.remove('invalid', 'changed', 'skipped');
      // Empty value = skip this register on Apply.  This is intentionally
      // distinct from typing "0" (which is a real value the firmware
      // interprets specially for timeouts: 0 = disable).
      if (inp.value.trim() === '') {
        inp.classList.add('skipped');
        inp.title = 'Empty — this register will be skipped on Apply (current charger value preserved)';
        updateDirtyCount();
        return;
      }
      inp.title = '';
      let parsed;
      try { parsed = parseInputValue(inp.value, reg); } catch { parsed = NaN; }
      if (Number.isNaN(parsed)) {
        inp.classList.add('invalid');
      } else if (reg.range && (parsed < reg.range[0] || parsed > reg.range[1])) {
        inp.classList.add('invalid');
      } else if (orig === undefined || Math.abs(parsed - orig) > 1e-9) {
        inp.classList.add('changed');
      }
      updateDirtyCount();
      // Redraw when a curve shaper changes (plot only if all four are set).
      if (['curve_cc','curve_cv','curve_fv','curve_tc'].includes(name)) {
        drawCurvePreview();
      }
    });
  });
  // Wire the inline TOE checkboxes (cfg-cvtoe / cfg-cctoe / cfg-fvtoe) that
  // we just rendered into the timeout rows.  Their dirty/build logic uses
  // the same bitfield code path as the bottom config-row.
  for (const toe of Object.values(TIMEOUT_TOE)) {
    const el = document.getElementById(toe.id);
    if (el) {
      el.addEventListener('change', () => {
        onUserEditedBeforeReload();
        refreshConfigRowChangedClasses();
        updateDirtyCount();
      });
    }
  }
}

function syncActionButtons(dirty, invalid) {
  // Apply is the committing action — give it the filled style only when
  // there is something to write.  Reload stays primary when the form is clean.
  const reload = document.getElementById('reload');
  const apply  = document.getElementById('apply');
  const hasWork = dirty && !invalid;
  reload.classList.toggle('primary', !hasWork);
  apply.classList.toggle('primary', hasWork);
}

function updateDirtyCount() {
  const tableDirty   = document.querySelectorAll('#settings input.changed').length;
  const tableBad     = document.querySelectorAll('#settings input.invalid').length;
  const tableSkipped = document.querySelectorAll('#settings input.skipped').length;
  const rawDirty     = document.querySelectorAll('#cfg-raw.changed').length;
  const rawBad       = document.querySelectorAll('#cfg-raw.invalid').length;
  const cfgDirty     = configRowDirtyCount() + rawDirty;
  const cfgBad       = configRowInvalidCount() + rawBad;
  const dirty   = tableDirty + cfgDirty;
  const invalid = tableBad + cfgBad;
  const span    = document.getElementById('dirty-count');
  const apply   = document.getElementById('apply');
  const discard = document.getElementById('discard');
  span.classList.toggle('invalid', invalid > 0);
  if (invalid) {
    span.textContent = `${invalid} invalid value${invalid===1?'':'s'}`;
    apply.disabled = true;
    apply.title = hasReloadedFromCharger
      ? ''
      : 'Reload from charger before writing to hardware';
    discard.disabled = false;   // user can still discard the bad edit
  } else if (dirty || tableSkipped) {
    const parts = [];
    if (dirty)        parts.push(`${dirty} change${dirty===1?'':'s'}`);
    if (tableSkipped) parts.push(`${tableSkipped} skipped`);
    span.textContent = parts.join(' · ') + ' pending';
    apply.disabled   = !dirty || !hasReloadedFromCharger;
    apply.title      = hasReloadedFromCharger
      ? ''
      : 'Reload from charger before writing to hardware';
    discard.disabled = !dirty && !tableSkipped;
  } else {
    span.textContent = '';
    apply.disabled   = true;
    apply.title      = hasReloadedFromCharger
      ? ''
      : 'Reload from charger before writing to hardware';
    discard.disabled = true;
  }
  syncActionButtons(dirty, invalid);
  updateFormActionState();
}

function updateFormActionState() {
  const exp = document.getElementById('export-settings');
  const imp = document.getElementById('import-settings');
  const ready = hasReloadedFromCharger;
  if (exp) {
    exp.disabled = !ready;
    exp.title = ready ? 'Download current charger settings as JSON'
      : 'Reload from charger first';
  }
  if (imp) {
    imp.disabled = !Object.keys(registers).length;
    imp.title = 'Load settings from a JSON file into the form';
  }
}

// ---------- friendly config row (CURVE_CONFIG checkboxes + restart V) ------

function decodeCurveConfig(raw) {
  // raw is the 16-bit register value last read from the charger
  return {
    chargerMode:  (raw & CFG_CUVE) !== 0,
    tempCompOff:  (raw & CFG_TCS_MASK) === 0,   // ticked = comp disabled
    floatAfterCV: (raw & CFG_CVTSSE) !== 0,
    restartEn:    (raw & CFG_RSTE) !== 0,
    cvTimeoutEn:  (raw & CFG_CVTOE) !== 0,
    ccTimeoutEn:  (raw & CFG_CCTOE) !== 0,
    fvTimeoutEn:  (raw & CFG_FVTOE) !== 0,
  };
}

function readConfigRow() {
  return {
    chargerMode:  document.getElementById('cfg-charger').checked,
    tempCompOff:  document.getElementById('cfg-tempoff').checked,
    floatAfterCV: document.getElementById('cfg-float').checked,
    restartEn:    document.getElementById('cfg-restart').checked,
    cvTimeoutEn:  document.getElementById('cfg-cvtoe').checked,
    ccTimeoutEn:  document.getElementById('cfg-cctoe').checked,
    fvTimeoutEn:  document.getElementById('cfg-fvtoe').checked,
    restartV:     parseFloat(document.getElementById('cfg-restart-v').value),
  };
}

function paintConfigRow(values) {
  // Apply UI state from a decoded config object (+ optional restartV)
  document.getElementById('cfg-charger').checked = values.chargerMode;
  document.getElementById('cfg-tempoff').checked = values.tempCompOff;
  document.getElementById('cfg-float').checked   = values.floatAfterCV;
  document.getElementById('cfg-restart').checked = values.restartEn;
  document.getElementById('cfg-cvtoe').checked   = values.cvTimeoutEn;
  document.getElementById('cfg-cctoe').checked   = values.ccTimeoutEn;
  document.getElementById('cfg-fvtoe').checked   = values.fvTimeoutEn;
  if (typeof values.restartV === 'number' && !Number.isNaN(values.restartV)) {
    document.getElementById('cfg-restart-v').value = values.restartV.toFixed(1);
  }
  applyRestartGroupState();
  refreshConfigRowChangedClasses();
}

function applyRestartGroupState() {
  const enabled = document.getElementById('cfg-restart').checked;
  const grp = document.getElementById('restart-group');
  const inp = document.getElementById('cfg-restart-v');
  grp.classList.toggle('disabled', !enabled);
  inp.disabled = !enabled;
}

function refreshConfigRowChangedClasses() {
  if (currentCurveConfig === null) return;
  const orig = decodeCurveConfig(currentCurveConfig);
  const cur  = readConfigRow();
  document.getElementById('check-charger-wrap')
    .classList.toggle('changed', cur.chargerMode !== orig.chargerMode);
  document.getElementById('check-tcoff-wrap')
    .classList.toggle('changed', cur.tempCompOff !== orig.tempCompOff);
  document.getElementById('check-float-wrap')
    .classList.toggle('changed', cur.floatAfterCV !== orig.floatAfterCV);
  document.getElementById('check-restart-wrap')
    .classList.toggle('changed', cur.restartEn !== orig.restartEn);
  document.getElementById('check-cvtoe-wrap')
    .classList.toggle('changed', cur.cvTimeoutEn !== orig.cvTimeoutEn);
  document.getElementById('check-cctoe-wrap')
    .classList.toggle('changed', cur.ccTimeoutEn !== orig.ccTimeoutEn);
  document.getElementById('check-fvtoe-wrap')
    .classList.toggle('changed', cur.fvTimeoutEn !== orig.fvTimeoutEn);
  const vInp = document.getElementById('cfg-restart-v');
  vInp.classList.remove('invalid', 'changed');
  if (cur.restartEn) {
    if (Number.isNaN(cur.restartV) || cur.restartV < 0 || cur.restartV > 58.4) {
      vInp.classList.add('invalid');
    } else if (currentRestartV === null
               || Math.abs(cur.restartV - currentRestartV) > 1e-6) {
      vInp.classList.add('changed');
    }
  }
}

function configRowDirtyCount() {
  // Checkboxes live in two DOM locations: bottom #config-row (charger
  // mode, temp comp, float-after-CV, restart) and inline in #settings
  // (the per-stage TOE enables that sit next to each timeout value).
  // Both contribute to the curve_config write.
  return document.querySelectorAll('#config-row .check.changed').length
       + document.querySelectorAll('#settings .check.changed').length
       + document.querySelectorAll('#config-row input.num.changed').length;
}
function configRowInvalidCount() {
  return document.querySelectorAll('#config-row input.num.invalid').length;
}

function buildCurveConfigWrite() {
  // Compose a new curve_config 16-bit value from the friendly UI, preserving
  // any bits we don't expose (CUVS preset selector, undocumented bits, etc.).
  if (currentCurveConfig === null) return null;
  const cur = readConfigRow();
  const masksWeOwn = CFG_TCS_MASK | CFG_CVTSSE | CFG_CUVE
                   | CFG_CVTOE | CFG_CCTOE | CFG_FVTOE | CFG_RSTE;
  let v = currentCurveConfig & ~masksWeOwn;
  if (cur.chargerMode)  v |= CFG_CUVE;
  // tempCompOff ticked = bits 2-3 = 00; unticked = restore -3mV/C/cell (01)
  if (!cur.tempCompOff) v |= 0x0004;
  if (cur.floatAfterCV) v |= CFG_CVTSSE;
  if (cur.cvTimeoutEn)  v |= CFG_CVTOE;
  if (cur.ccTimeoutEn)  v |= CFG_CCTOE;
  if (cur.fvTimeoutEn)  v |= CFG_FVTOE;
  if (cur.restartEn)    v |= CFG_RSTE;
  return v & 0xFFFF;
}

function _parseRawCurveConfig() {
  const inp = document.getElementById('cfg-raw');
  if (!inp || inp.classList.contains('invalid')) return null;
  const t = inp.value.trim();
  if (!t) return null;
  const n = t.toLowerCase().startsWith('0x') ? parseInt(t, 16) : parseInt(t, 10);
  if (Number.isNaN(n) || n < 0 || n > 0xFFFF) return null;
  return n & 0xFFFF;
}

function gatherConfigRowWrites() {
  const out = {};
  if (currentCurveConfig === null) return out;
  const rawInp = document.getElementById('cfg-raw');
  // Power-user hex edit takes precedence over the checkbox row when the
  // hex field itself is marked changed (not just repainted from checkboxes).
  if (rawInp && rawInp.classList.contains('changed')) {
    const n = _parseRawCurveConfig();
    if (n !== null && n !== currentCurveConfig) out.curve_config = n;
  } else {
    const orig = decodeCurveConfig(currentCurveConfig);
    const cur  = readConfigRow();
    const cfgChanged =
      cur.chargerMode  !== orig.chargerMode ||
      cur.tempCompOff  !== orig.tempCompOff ||
      cur.floatAfterCV !== orig.floatAfterCV ||
      cur.restartEn    !== orig.restartEn ||
      cur.cvTimeoutEn  !== orig.cvTimeoutEn ||
      cur.ccTimeoutEn  !== orig.ccTimeoutEn ||
      cur.fvTimeoutEn  !== orig.fvTimeoutEn;
    if (cfgChanged) out.curve_config = buildCurveConfigWrite();
  }
  const orig = decodeCurveConfig(currentCurveConfig);
  const cur  = readConfigRow();
  if (cur.restartEn
      && !Number.isNaN(cur.restartV)
      && (currentRestartV === null
          || Math.abs(cur.restartV - currentRestartV) > 1e-6)) {
    out.chg_rst_vbat = cur.restartV;
  }
  return out;
}

// Wire up the five config-row controls
// Wire up the bottom config-row controls (the inline TOE checkboxes
// inside the table get their listeners from buildSettingsTable).
['cfg-charger', 'cfg-tempoff', 'cfg-float', 'cfg-restart'].forEach(id => {
  document.getElementById(id).addEventListener('change', () => {
    onUserEditedBeforeReload();
    if (id === 'cfg-restart') applyRestartGroupState();
    refreshConfigRowChangedClasses();
    updateDirtyCount();
  });
});
document.getElementById('cfg-restart-v').addEventListener('input', () => {
  onUserEditedBeforeReload();
  refreshConfigRowChangedClasses();
  updateDirtyCount();
});

let userToggling = 0;

// ---------- charge curve preview -------------------------------------------
//
// Live SVG mirror of the manual's 3-stage diagram (page 44 / page 22 of 40
// in the PDF).  Reads curve_cc / curve_cv / curve_fv / curve_tc straight
// from the form inputs and redraws on every keystroke.  A traveller dot
// loops along both curves so the page never feels static.

function allowSettingsDefaults() {
  curveDefaultsAllowed = true;
}

function cancelSuggestedDefaultsTimer() {
  if (suggestedDefaultsTimer) {
    clearTimeout(suggestedDefaultsTimer);
    suggestedDefaultsTimer = null;
  }
}

function onUserEditedBeforeReload() {
  if (!_applyingSuggestedDefaults) cancelSuggestedDefaultsTimer();
}

function setSuggestedPreviewMode(on) {
  hasSuggestedPreview = on;
  const panel = document.getElementById('settings-panel');
  if (panel) panel.classList.toggle('suggested-preview', on);
}

function clearSuggestedPreviewForm() {
  setSuggestedPreviewMode(false);
  curveDefaultsAllowed = false;
  for (const name of SETTING_KEYS) {
    const inp = document.querySelector(`#settings input[data-name="${name}"]`);
    if (inp) {
      inp.value = '';
      inp.placeholder = '';
      inp.classList.remove('changed', 'invalid', 'skipped');
    }
    const raw = document.querySelector(`#settings [data-raw="${name}"]`);
    if (raw) raw.textContent = '';
    delete currentValues[name];
  }
  drawCurvePreview();
}

function applyDemoModeFromHealth(h) {
  if (!h || healthDemoResolved) return;
  isDemoMode = !!h.demo;
  healthDemoResolved = true;
}

function applyDemoModeFromSse(data) {
  if (healthDemoResolved || data.demo === undefined) return;
  isDemoMode = !!data.demo;
  healthDemoResolved = true;
  if (isDemoMode && !hasReloadedFromCharger && !suggestedDefaultsTimer) {
    scheduleSuggestedDefaults();
  }
}

async function fetchHealthWithRetry() {
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      return await fetchJSON('/api/health');
    } catch {
      if (attempt < 2) await _sleep(400 * (attempt + 1));
    }
  }
  return null;
}

function scheduleSuggestedDefaults() {
  if (!isDemoMode) return;
  cancelSuggestedDefaultsTimer();
  suggestedDefaultsTimer = setTimeout(() => {
    suggestedDefaultsTimer = null;
    if (hasReloadedFromCharger) return;
    applySuggestedDefaults();
    log('filled suggested 16S LFP defaults — Reload from charger to read the unit', 'ok');
  }, 5000);
}

/** Preview-only fill (README 16S LFP) — not a charger baseline for Apply. */
function applySuggestedDefaults() {
  if (!registers || !Object.keys(registers).length) return;
  _applyingSuggestedDefaults = true;
  allowSettingsDefaults();
  setSuggestedPreviewMode(true);
  for (const name of SETTING_KEYS) {
    const val = SETTINGS_DEFAULTS[name];
    if (val === undefined) continue;
    const inp = document.querySelector(`#settings input[data-name="${name}"]`);
    if (inp) {
      const formatted = formatInputValue(val, registers[name]);
      inp.value = formatted;
      inp.placeholder = `(suggested preview — ${formatted})`;
      inp.classList.remove('changed', 'invalid', 'skipped');
      inp.title = 'Suggested value for preview only — Reload from charger before Apply';
    }
    const raw = document.querySelector(`#settings [data-raw="${name}"]`);
    if (raw) {
      const reg = registers[name];
      const rawN = reg.scale === 1
        ? (Number(val) & 0xFFFF)
        : Math.round(Number(val) / reg.scale);
      raw.textContent = `raw=${rawN} (suggested)`;
    }
  }
  _applyingSuggestedDefaults = false;
  updateDirtyCount();
  drawCurvePreview();
  updateConfigSummary();
}

function readCurveInputs() {
  function pick(name, key) {
    const inp = document.querySelector(`#settings input[data-name="${name}"]`);
    if (inp && inp.value.trim() !== '' && !inp.classList.contains('invalid')) {
      const v = parseFloat(inp.value);
      if (!Number.isNaN(v)) return v;
    }
    if (currentValues[name] !== undefined && currentValues[name] !== null) {
      return currentValues[name];
    }
    if (isDemoMode && curveDefaultsAllowed) return CURVE_DEFAULTS[key];
    return null;
  }
  return {
    cc: pick('curve_cc', 'cc'),
    cv: pick('curve_cv', 'cv'),
    fv: pick('curve_fv', 'fv'),
    tc: pick('curve_tc', 'tc'),
  };
}

function curveInputsComplete(curve) {
  return curve.cc !== null && curve.cv !== null
      && curve.fv !== null && curve.tc !== null;
}

// Plot coordinate system. The diagram in the manual divides the x-axis
// into three roughly equal stage bands; we keep that convention.
//
// Layout (left → right):
//   |--axis title--|--tick labels--|====== plot area ======|--tick labels--|--axis title--|
//      ~18px           ~50px              variable             ~50px             ~18px
//
const PLOT = {
  W: 780, H: 320,
  L: 70,        // left padding: voltage axis-title (rotated) + tick labels
  R: 70,        // right padding: current axis-title (rotated) + tick labels
  T: 46,        // top padding for stage labels
  B: 44,        // bottom padding for x-axis baseline labels (no more LED strip)
  stageFrac: [0.42, 0.36, 0.22],
  vMin: 40, vMax: 60,       // voltage range, V
  iMax: 26,                  // current range, A (matches RANGES['curve_cc'])
};

function curvePlotBox() {
  const inner = {
    x: PLOT.L,
    y: PLOT.T,
    w: PLOT.W - PLOT.L - PLOT.R,
    h: PLOT.H - PLOT.T - PLOT.B,
  };
  inner.x2 = inner.x + inner.w;
  inner.y2 = inner.y + inner.h;
  return inner;
}

function vToY(v, box) {
  const t = (v - PLOT.vMin) / (PLOT.vMax - PLOT.vMin);
  return box.y2 - Math.max(0, Math.min(1, t)) * box.h;
}
function iToY(i, box) {
  const t = i / PLOT.iMax;
  return box.y2 - Math.max(0, Math.min(1, t)) * box.h;
}

// Curve shape — informed by the diagram on page 44 and real LFP behaviour:
//   I(t) in stage 1 (CC): horizontal at cc
//   I(t) in stage 2 (CV): exponential decay from cc -> tc
//   I(t) in stage 3 (FV): horizontal at low (we use tc * 0.25 as trickle)
//   V(t) in stage 1: stays flat near the resting pack voltage for most of
//     the stage, then rises *sharply* near the end as the cells approach
//     full and start absorbing voltage rather than capacity.  This is the
//     LFP shape — almost-step at the knee, not the smooth rise the early
//     ease-out version drew.
//   V(t) in stage 2: horizontal at cv
//   V(t) in stage 3: horizontal at fv
//
// Returns {iPath, vPath, samples: [{x, vy, iy, stage}], stageBounds}.
function buildCurvePaths({cc, cv, fv, tc}) {
  const box = curvePlotBox();
  const fracs = PLOT.stageFrac;
  const stageX = [
    box.x,
    box.x + box.w * fracs[0],
    box.x + box.w * (fracs[0] + fracs[1]),
    box.x2,
  ];
  // Resting voltage for a 16S LFP pack at low SoC ≈ 3.2 V/cell.  Clamp
  // between the plot floor + 1 V and (cv - 0.5) so we always show a
  // visible rise even if the user picks an unusually low cv.
  const V_INIT = Math.max(PLOT.vMin + 1, Math.min(cv - 0.5, 51.2));
  const trickle = Math.max(0.2, tc * 0.25);

  const samples = [];
  const N_PER = 24;
  for (let s = 0; s < 3; s++) {
    const xa = stageX[s], xb = stageX[s+1];
    for (let k = 0; k <= N_PER; k++) {
      const u = k / N_PER;
      const x = xa + (xb - xa) * u;
      let iVal, vVal;
      if (s === 0) {
        iVal = cc;
        // Flat-then-sharp-rise.  Knee-curve: u^6 keeps the line within ~1.5%
        // of V_INIT for the first ~60% of the stage, then accelerates hard
        // to hit cv exactly at u=1.  Matches the LFP voltage profile under
        // constant current much better than the previous ease-out shape.
        const eased = Math.pow(u, 6);
        vVal = V_INIT + (cv - V_INIT) * eased;
      } else if (s === 1) {
        // exponential decay: I(u) = tc + (cc - tc) * e^{-k*u}
        const decay = Math.exp(-3.2 * u);
        iVal = tc + (cc - tc) * decay;
        vVal = cv;
      } else {
        iVal = trickle;
        vVal = fv;
      }
      samples.push({ x, iy: iToY(iVal, box), vy: vToY(vVal, box), stage: s });
    }
  }
  const toPath = (key) =>
    samples.map((p, i) => (i === 0 ? 'M' : 'L') + p.x.toFixed(1) + ' ' + p[key].toFixed(1)).join(' ');
  const iPath = toPath('iy');
  const vPath = toPath('vy');
  return { iPath, vPath, samples, stageX, box };
}

function drawCurvePreview() {
  const wrap = document.querySelector('.curve-preview');
  const curve = readCurveInputs();
  if (!curveInputsComplete(curve)) {
    wrap?.classList.add('is-empty');
    for (const id of ['curve-i', 'curve-v', 'curve-i-area', 'curve-v-area']) {
      document.getElementById(id)?.setAttribute('d', '');
    }
    document.getElementById('curve-stages').innerHTML = '';
    document.getElementById('curve-grid').innerHTML = '';
    document.getElementById('curve-axis-titles').innerHTML = '';
    document.getElementById('curve-annotations').innerHTML = '';
    curveSamples = [];
    return;
  }
  wrap?.classList.remove('is-empty');
  const { iPath, vPath, samples, stageX, box } = buildCurvePaths(curve);

  document.getElementById('curve-i').setAttribute('d', iPath);
  document.getElementById('curve-v').setAttribute('d', vPath);
  document.getElementById('curve-i-area').setAttribute('d',
    iPath + ` L ${box.x2.toFixed(1)} ${box.y2.toFixed(1)} L ${box.x.toFixed(1)} ${box.y2.toFixed(1)} Z`);
  document.getElementById('curve-v-area').setAttribute('d',
    vPath + ` L ${box.x2.toFixed(1)} ${box.y2.toFixed(1)} L ${box.x.toFixed(1)} ${box.y2.toFixed(1)} Z`);

  // Stage background tints + dividers + labels at the top.
  const stagesG = document.getElementById('curve-stages');
  // Battery folks usually call these stages bulk / absorption / float.
  // Keep the CC/CV/FV initials as a subtitle so the manual cross-reference
  // is still obvious.
  const stageNames = ['stage 1 · bulk', 'stage 2 · absorption', 'stage 3 · float'];
  const stageDescs = ['constant current (CC)', 'constant voltage (CV)', 'float voltage (FV)'];
  const stageClasses = ['cc', 'cv', 'fv'];
  let html = '';
  for (let s = 0; s < 3; s++) {
    const x = stageX[s], w = stageX[s+1] - stageX[s];
    html += `<rect class="stage-bg ${stageClasses[s]}" data-stage="${s}"
                  x="${x.toFixed(1)}" y="${box.y.toFixed(1)}"
                  width="${w.toFixed(1)}" height="${box.h.toFixed(1)}"/>`;
    if (s > 0) {
      html += `<line class="stage-divider"
                     x1="${x.toFixed(1)}" x2="${x.toFixed(1)}"
                     y1="${box.y.toFixed(1)}" y2="${box.y2.toFixed(1)}"/>`;
    }
    const cx = x + w / 2;
    html += `<text class="stage-label" x="${cx.toFixed(1)}" y="${(box.y - 18).toFixed(1)}" text-anchor="middle">${stageNames[s]}</text>`;
    html += `<text class="stage-sub"   x="${cx.toFixed(1)}" y="${(box.y - 6).toFixed(1)}"  text-anchor="middle">${stageDescs[s]}</text>`;
  }
  stagesG.innerHTML = html;

  // Grid + tick labels.
  const gridG = document.getElementById('curve-grid');
  let g = '';
  // Voltage tick labels + horizontal gridlines on the left axis (voltage curve).
  for (let v = PLOT.vMin; v <= PLOT.vMax; v += 4) {
    const y = vToY(v, box);
    g += `<line class="grid-line" x1="${box.x.toFixed(1)}" x2="${box.x2.toFixed(1)}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}"/>`;
    g += `<text class="axis-tick" x="${(box.x - 8).toFixed(1)}" y="${(y + 3.5).toFixed(1)}" text-anchor="end">${v}</text>`;
  }
  // Current tick labels on the right axis (current curve).
  for (const i of [0, 5, 10, 15, 20, 25]) {
    const y = iToY(i, box);
    g += `<text class="axis-tick" x="${(box.x2 + 8).toFixed(1)}" y="${(y + 3.5).toFixed(1)}" text-anchor="start">${i}</text>`;
  }
  // x-axis baseline + "start" / "time →" markers, in muted tick style.
  g += `<line class="grid-line" x1="${box.x.toFixed(1)}" x2="${box.x2.toFixed(1)}" y1="${box.y2.toFixed(1)}" y2="${box.y2.toFixed(1)}"/>`;
  g += `<text class="axis-tick" x="${box.x.toFixed(1)}" y="${(box.y2 + 16).toFixed(1)}" text-anchor="start">start</text>`;
  g += `<text class="axis-tick" x="${box.x2.toFixed(1)}" y="${(box.y2 + 16).toFixed(1)}" text-anchor="end">time →</text>`;
  gridG.innerHTML = g;

  // Axis titles: rotated 90° on each side so they unambiguously belong
  // to their own axis.  Voltage on the left (ink colour, matches the
  // V curve), current on the right (accent colour, matches the I curve).
  const titlesG = document.getElementById('curve-axis-titles');
  const midY = (box.y + box.y2) / 2;
  const vTitleX = 18;
  const iTitleX = PLOT.W - 18;
  titlesG.innerHTML =
    `<text class="axis-title v" transform="translate(${vTitleX} ${midY.toFixed(1)}) rotate(-90)" text-anchor="middle">voltage (V)</text>` +
    `<text class="axis-title i" transform="translate(${iTitleX} ${midY.toFixed(1)}) rotate(90)"  text-anchor="middle">current (A)</text>`;

  // Annotation pills with leader lines to the curve's salient points.
  // CC pill on the CC plateau, CV on the CV plateau, FV on the float plateau, TC on the taper bottom.
  const ann = document.getElementById('curve-annotations');
  const annotate = (cx, cy, ax, ay, label, name, cls) => {
    // Leader line from (cx, cy) to (ax, ay); pill at (ax, ay)
    const tw = Math.max(48, label.length * 7 + 28);
    const th = 28;
    const rx = ax - tw / 2, ry = ay - th / 2;
    return (
      `<path class="annotation-leader" d="M ${cx.toFixed(1)} ${cy.toFixed(1)} L ${ax.toFixed(1)} ${ay.toFixed(1)}"/>` +
      `<rect class="annotation-pill ${cls}" x="${rx.toFixed(1)}" y="${ry.toFixed(1)}" width="${tw}" height="${th}" rx="8" ry="8"/>` +
      `<text class="annotation-text" x="${ax.toFixed(1)}" y="${(ay + 1).toFixed(1)}" text-anchor="middle">` +
        `<tspan class="name">${name} </tspan>${label}` +
      `</text>`
    );
  };
  // CC annotation: middle of stage 1, on the I curve
  const ccMidX = (stageX[0] + stageX[1]) / 2;
  const ccMidY = iToY(curve.cc, box);
  // CV annotation: middle of stage 2, on the V curve
  const cvMidX = (stageX[1] + stageX[2]) / 2;
  const cvMidY = vToY(curve.cv, box);
  // FV annotation: middle of stage 3, on the V curve
  const fvMidX = (stageX[2] + stageX[3]) / 2;
  const fvMidY = vToY(curve.fv, box);
  // TC annotation: end of stage 2, on the I curve
  const tcX = stageX[2];
  const tcY = iToY(curve.tc, box);
  let aHtml = '';
  aHtml += annotate(ccMidX, ccMidY, ccMidX, ccMidY - 30, `${curve.cc.toFixed(1)} A`, 'CC',  'i');
  aHtml += annotate(cvMidX, cvMidY, cvMidX, cvMidY - 26, `${curve.cv.toFixed(1)} V`, 'CV', 'v');
  // If CV and FV are identical (typical LFP), nudge FV pill so it doesn't overlap CV.
  const fvLabelY = (Math.abs(curve.fv - curve.cv) < 0.05) ? fvMidY + 32 : fvMidY - 26;
  aHtml += annotate(fvMidX, fvMidY, fvMidX, fvLabelY, `${curve.fv.toFixed(1)} V`, 'FV', 'v');
  aHtml += annotate(tcX, tcY, tcX + 8, tcY + 28, `${curve.tc.toFixed(1)} A`, 'TC', 'i');
  ann.innerHTML = aHtml;

  // The LED-colour strip used to live here.  It was removed because
  //  (a) the firmware's per-stage LED colours don't match the simplified
  //      "orange / red / green" mnemonic for every model in the family
  //      (e.g. stage 2 isn't red on the NPB units used here), and
  //  (b) it was redundant with the stage labels already drawn above the
  //      plot.  If you ever want it back, render it into <g id="curve-led">
  //      again — the SVG container was removed too.
  //
  // The floating top-right legend was replaced by rotated axis titles
  // (drawn above) so the "voltage (V)" / "current (A)" labels live on
  // the axis they describe instead of floating in empty space.

  curveSamples = samples;
}

// Traveller dot — rAF loop that walks through the curve samples and
// pulses the active stage tint.
let curveSamples = [];
let curveT = 0;     // 0..1 progress through the full curve
let lastFrame = performance.now();

function tickCurveDot(now) {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const dt = (now - lastFrame) / 1000;
  lastFrame = now;
  // 8 seconds per full traversal.
  curveT = (curveT + dt / 8) % 1;
  if (curveSamples.length >= 2) {
    const idx = Math.min(curveSamples.length - 1, Math.floor(curveT * curveSamples.length));
    const p = curveSamples[idx];
    const di = document.getElementById('curve-dot-i');
    const dv = document.getElementById('curve-dot-v');
    di.setAttribute('cx', p.x.toFixed(1));
    di.setAttribute('cy', p.iy.toFixed(1));
    dv.setAttribute('cx', p.x.toFixed(1));
    dv.setAttribute('cy', p.vy.toFixed(1));
    // Highlight the active stage tint
    document.querySelectorAll('#curve-stages .stage-bg').forEach(el => {
      el.classList.toggle('active', Number(el.dataset.stage) === p.stage);
    });
  }
  requestAnimationFrame(tickCurveDot);
}

// ---------- connection stream (SSE) ---------------------------------------
//
// One long-lived EventSource replaces what used to be a 5-second poll on
// /api/read?names=operation.  The server pushes a `state` event every ~3 s
// (shared CAN poller) plus SSE `: ping` comment lines between events so
// proxies don't treat the connection as idle.

let stream = null;
let streamStallTimer = null;

function markStreamStall() {
  // No 'state' or comment received for >2x heartbeat interval — treat as
  // disconnected.  Triggered by the watchdog in startStream().
  setConn('err', 'no response');
}

function startStream() {
  if (stream) { try { stream.close(); } catch {} }
  stream = new EventSource('/api/stream');

  const resetWatchdog = () => {
    if (streamStallTimer) clearTimeout(streamStallTimer);
    // Server tick is every 3s; allow 8s before flagging stall.
    streamStallTimer = setTimeout(markStreamStall, 8000);
  };

  stream.addEventListener('state', (e) => {
    resetWatchdog();
    let data;
    try { data = JSON.parse(e.data); } catch { return; }
    applyDemoModeFromSse(data);
    if (!data.connected) {
      setConn('err', data.error ? 'CAN error' : 'no response');
      updateOnoffHint(false);
      return;
    }
    setConn('ok', 'connected', data.latency_ms);
    if (data.fault_status || data.chg_status || data.system_status) {
      paintHeaderStatus(data);
    }
    if (data.operation !== null && data.operation !== undefined
        && Date.now() - userToggling > 2500) {
      const sw = document.getElementById('onoff');
      sw.disabled = false;
      sw.checked = data.operation === 1;
      updateOnoffHint(true);
    }
    maybeAutoReloadOnConnect();
  });

  // `: ping` comments keep the TCP stream warm; the browser does not
  // dispatch a JS event for them.  The watchdog resets on every `state`
  // event — if those stop (charger silent / server hung), we show no response.
  stream.addEventListener('open', resetWatchdog);

  stream.onerror = () => {
    // Browser will auto-reconnect (default retry = 3s).  Show the
    // disconnect state immediately so the chip is honest.
    setConn('err', 'no response');
  };

  resetWatchdog();
}

// ---------- header status pills (fault / charge / system) ------------------
//
// Flat list in the header bar — CCM, CVM, OTP, DC_OK, etc.  No section
// title; hover a pill for the full decoded string from the manual.

const CHG_STAGE_KEYS = new Set(['CCM', 'CVM', 'FVM', 'FULLM']);

function _pillClassForFlag(kind, shortName) {
  if (kind === 'fault') return 'bad';
  if (kind === 'chg' && CHG_STAGE_KEYS.has(shortName)) return 'stage';
  if (kind === 'sys' && shortName === 'DC_OK') return 'ok';
  return '';
}

function paintHeaderStatus(data) {
  const el = document.getElementById('header-status');
  if (!el) return;
  const chg = data.chg_status || [];
  const fault = data.fault_status || [];
  const sys = data.system_status || [];
  el.replaceChildren();
  const addPill = (full, kind) => {
    const short = full.includes('(') ? full.split(' (')[0].trim() : full;
    const cls = _pillClassForFlag(kind, short);
    const span = document.createElement('span');
    span.className = 'pill' + (cls ? ' ' + cls : '');
    span.title = full;
    span.textContent = short;
    el.appendChild(span);
  };
  for (const f of chg) addPill(f, 'chg');
  if (fault.length) {
    for (const f of fault) addPill(f, 'fault');
  } else {
    const ok = document.createElement('span');
    ok.className = 'pill ok';
    ok.title = 'No active faults';
    ok.textContent = 'OK';
    el.appendChild(ok);
  }
  for (const f of sys) addPill(f, 'sys');
  if (!el.childElementCount) {
    const ok = document.createElement('span');
    ok.className = 'pill ok';
    ok.title = 'No active flags';
    ok.textContent = 'ok';
    el.appendChild(ok);
  }
}

async function refreshStatus() {
  try {
    const s = await fetchJSON('/api/status');
    paintHeaderStatus({
      chg_status:    s.chg_status?.flags || [],
      fault_status: s.fault_status?.flags || [],
      system_status: s.system_status?.flags || [],
    });
  } catch {
    const el = document.getElementById('header-status');
    if (el) {
      el.replaceChildren();
      const dim = document.createElement('span');
      dim.className = 'pill dim';
      dim.title = 'Could not read status';
      dim.textContent = '…';
      el.appendChild(dim);
    }
  }
}

async function maybeAutoReloadOnConnect() {
  if (!isDemoMode || hasReloadedFromCharger || demoAutoReloadDone
      || _reloadInFlight) return;
  demoAutoReloadDone = true;
  log('demo mode — loading settings from simulated charger', 'ok');
  await reloadFromCharger();
  if (!hasReloadedFromCharger) demoAutoReloadDone = false;
}

async function refreshDeviceInfo() {
  try {
    const d = await fetchJSON('/api/device_info');
    // Some units return literal "000" or empty placeholders for unpopulated
    // fields; treat those as empty so the UI shows the friendly fallback.
    const isPlaceholder = v => !v || /^[0\s]+$/.test(String(v));
    const setVal = (id, v, fallback='unavailable') => {
      const el = document.getElementById(id);
      if (!isPlaceholder(v)) {
        el.textContent = v;
        el.classList.remove('empty');
      } else {
        el.textContent = fallback;
        el.classList.add('empty');
      }
    };
    setVal('info-model',  d.model);
    setVal('info-mfr',    d.manufacturer);
    setVal('info-serial', d.serial);
    setVal('info-fw',     d.firmware);
    setVal('info-loc',    d.location);
    // pretty-print the date if it's a YYMMDD string
    let dateText = d.made;
    if (dateText && /^\d{6}$/.test(dateText)) {
      dateText = `20${dateText.slice(0,2)}-${dateText.slice(2,4)}-${dateText.slice(4,6)}`;
    }
    setVal('info-date', dateText);
    const vinEl = document.getElementById('info-vin');
    const tempEl = document.getElementById('info-temp');
    if (vinEl) {
      if (d.vin !== null && d.vin !== undefined) {
        vinEl.textContent = fmt(d.vin, 1) + ' V';
        vinEl.classList.remove('empty');
      } else {
        vinEl.textContent = 'unavailable';
        vinEl.classList.add('empty');
      }
    }
    if (tempEl) {
      if (d.temp !== null && d.temp !== undefined) {
        tempEl.textContent = fmt(d.temp, 1) + ' °C';
        tempEl.classList.remove('empty');
      } else {
        tempEl.textContent = 'unavailable';
        tempEl.classList.add('empty');
      }
    }
    updatePreviewMeta(d.model);
  } catch (e) { /* silent — device info isn't critical to UI */ }
}

async function loadRegisters() {
  // Just fetch metadata and render empty rows.  The user explicitly clicks
  // "Reload from charger" to populate values.
  registers = await fetchJSON('/api/registers');
  buildSettingsTable();
}

async function reloadFromCharger() {
  if (_reloadInFlight) {
    _reloadQueued = true;
    return;
  }
  cancelSuggestedDefaultsTimer();
  setSuggestedPreviewMode(false);
  _reloadInFlight = true;
  const reloadBtn = document.getElementById('reload');
  reloadBtn.classList.add('writing');
  reloadBtn.disabled = true;
  reloadBtn.title = 'Reading from charger…';
  try {
    const allKeys = SETTING_KEYS.concat(FRIENDLY_KEYS);
    const data = await fetchJSON('/api/read?names=' + allKeys.join(','));
    const missing = [];
    for (const name of allKeys) {
      const v = data[name];
      if (!v || v.value === null || v.value === undefined) missing.push(name);
    }
    if (missing.length) {
      throw new Error('no response for: ' + missing.join(', '));
    }
    if (isDemoMode) allowSettingsDefaults();
    for (const name of SETTING_KEYS) {
      const v = data[name];
      const value = v.value;
      currentValues[name] = value;
      const inp = document.querySelector(`#settings input[data-name="${name}"]`);
      if (inp) {
        const formatted = formatInputValue(value, registers[name]);
        inp.value = formatted;
        inp.placeholder = `(skip — ${formatted})`;
        inp.classList.remove('changed', 'invalid', 'skipped');
        inp.title = '';
      }
      const raw = document.querySelector(`#settings [data-raw="${name}"]`);
      if (raw) raw.textContent = `raw=${v.raw}`;
    }
    currentRestartV = data.chg_rst_vbat.value;
    currentCurveConfig = data.curve_config.raw;
    const decoded = decodeCurveConfig(currentCurveConfig);
    paintConfigRow({...decoded, restartV: currentRestartV ?? 48.0});
    document.getElementById('config-row').classList.remove('unloaded');
    document.querySelectorAll('.check.inline-check.unloaded')
      .forEach(el => el.classList.remove('unloaded'));
    refreshRawHexFromState();
    updateDirtyCount();
    drawCurvePreview();
    updateConfigSummary();
    hasReloadedFromCharger = true;
    hideReloadBanner();
    log('reloaded settings from charger', 'ok');
  } catch (e) {
    log('reload failed: ' + e.message, 'err');
  } finally {
    reloadBtn.classList.remove('writing');
    reloadBtn.disabled = false;
    reloadBtn.title = '';
    _reloadInFlight = false;
    if (_reloadQueued) {
      _reloadQueued = false;
      reloadFromCharger();
    }
  }
}

function _applySettingsToForm(settings, markAsChanged) {
  for (const name of SETTING_KEYS) {
    if (!(name in settings)) continue;
    const val = settings[name];
    const inp = document.querySelector(`#settings input[data-name="${name}"]`);
    if (!inp || !registers[name]) continue;
    inp.value = formatInputValue(val, registers[name]);
    inp.classList.remove('invalid', 'skipped');
    inp.title = '';
    if (markAsChanged) {
      const orig = currentValues[name];
      inp.classList.toggle('changed',
        orig === undefined || Math.abs(Number(val) - Number(orig)) > 1e-9);
    } else {
      inp.classList.remove('changed');
      currentValues[name] = val;
    }
    const rawCell = document.querySelector(`#settings [data-raw="${name}"]`);
    if (rawCell) {
      const reg = registers[name];
      const raw = reg.scale === 1
        ? (Number(val) & 0xFFFF)
        : Math.round(Number(val) / reg.scale);
      rawCell.textContent = `raw=${raw}`;
    }
  }
  if ('curve_config' in settings) {
    currentCurveConfig = Number(settings.curve_config) & 0xFFFF;
  }
  if ('chg_rst_vbat' in settings) {
    currentRestartV = Number(settings.chg_rst_vbat);
  }
  if (currentCurveConfig !== null) {
    const decoded = decodeCurveConfig(currentCurveConfig);
    paintConfigRow({...decoded, restartV: currentRestartV ?? 48.0});
    document.getElementById('config-row').classList.remove('unloaded');
    document.querySelectorAll('.check.inline-check.unloaded')
      .forEach(el => el.classList.remove('unloaded'));
  }
  refreshRawHexFromState();
  updateDirtyCount();
  drawCurvePreview();
  updateConfigSummary();
}

function exportSettings() {
  if (!hasReloadedFromCharger) {
    log('export requires reloading from charger first', 'err');
    return;
  }
  const settings = {};
  for (const name of SETTINGS_EXPORT_KEYS) {
    if (name === 'curve_config' && currentCurveConfig !== null) {
      settings[name] = currentCurveConfig;
    } else if (name === 'chg_rst_vbat' && currentRestartV !== null) {
      settings[name] = currentRestartV;
    } else if (currentValues[name] !== undefined) {
      settings[name] = currentValues[name];
    }
  }
  const payload = {
    version: SETTINGS_EXPORT_VERSION,
    exported_at: new Date().toISOString(),
    source: 'npb-console',
    settings,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)],
    { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'npb-settings-' + new Date().toISOString().slice(0, 10) + '.json';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(a.href);
  log('exported settings to JSON file', 'ok');
}

function applyImportedSettings(data) {
  if (!data || data.version !== SETTINGS_EXPORT_VERSION) {
    throw new Error('unsupported settings file (expected version 1)');
  }
  const settings = data.settings;
  if (!settings || typeof settings !== 'object' || Array.isArray(settings)) {
    throw new Error('settings must be an object');
  }
  for (const name of Object.keys(settings)) {
    if (!SETTINGS_EXPORT_KEYS.includes(name)) {
      throw new Error(`unknown register ${name}`);
    }
    if (!registers[name]) {
      throw new Error('register metadata not loaded yet');
    }
    const reg = registers[name];
    const val = settings[name];
    if (reg.scale === 1 && !reg.unit) {
      const n = Number(val);
      if (!Number.isInteger(n) || n < 0 || n > 0xFFFF) {
        throw new Error(`${name} must be a 16-bit integer`);
      }
    } else if (reg.range) {
      const num = Number(val);
      if (Number.isNaN(num) || num < reg.range[0] || num > reg.range[1]) {
        throw new Error(`${name} must be in [${reg.range[0]}, ${reg.range[1]}]`);
      }
    }
  }
  const markAsChanged = hasReloadedFromCharger;
  _applySettingsToForm(settings, markAsChanged);
  if (!hasReloadedFromCharger) {
    log('imported settings as draft — Reload from charger to compare with hardware', 'warn');
  } else {
    log('imported settings into form (Apply to write)', 'ok');
  }
}

function importSettingsFromFile(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = () => {
    try {
      applyImportedSettings(JSON.parse(reader.result));
    } catch (e) {
      log('import failed: ' + e.message, 'err');
    }
  };
  reader.onerror = () => log('import failed: could not read file', 'err');
  reader.readAsText(file);
}

// ---------- diff preview + confirm modal -----------------------------------
//
// gatherDirtyWrites returns a settings dict + a human-readable diff list
// so we can show the user exactly what will change before committing.

function _formatForDisplay(name, value) {
  const reg = registers[name];
  if (!reg) return String(value);
  if (reg.scale === 1) {
    return reg.unit
      ? String(Math.round(Number(value))) + ' ' + reg.unit
      : '0x' + Number(value).toString(16).toUpperCase().padStart(4,'0');
  }
  const dec = reg.scale < 1 ? 2 : 0;
  return Number(value).toFixed(dec) + (reg.unit ? ' ' + reg.unit : '');
}

function gatherDirtyWrites() {
  const settings = {};
  const diff = [];
  for (const inp of document.querySelectorAll('#settings input.changed')) {
    const name  = inp.dataset.name;
    const newV  = parseInputValue(inp.value, registers[name]);
    const oldV  = currentValues[name];
    settings[name] = newV;
    diff.push({
      name,
      desc:    registers[name].desc,
      oldText: oldV === undefined ? '—' : _formatForDisplay(name, oldV),
      newText: _formatForDisplay(name, newV),
    });
  }
  const cfgWrites = gatherConfigRowWrites();
  Object.assign(settings, cfgWrites);
  // Decode curve_config / chg_rst_vbat into friendly diffs (the table
  // doesn't show them but the user toggled real flags).
  if ('curve_config' in cfgWrites && currentCurveConfig !== null) {
    const before = decodeCurveConfig(currentCurveConfig);
    const after  = decodeCurveConfig(cfgWrites.curve_config);
    const rawInp = document.getElementById('cfg-raw');
    if (rawInp && rawInp.classList.contains('changed')) {
      diff.push({
        name:    'curve_config',
        desc:    'curve_config (raw hex)',
        oldText: '0x' + currentCurveConfig.toString(16).toUpperCase().padStart(4,'0'),
        newText: '0x' + cfgWrites.curve_config.toString(16).toUpperCase().padStart(4,'0'),
      });
    } else {
    const flagNames = {
      chargerMode:  'Charger mode',
      tempCompOff:  'Temp comp off',
      floatAfterCV: 'Float after CV',
      restartEn:    'Restart on low Vbat',
      cvTimeoutEn:  'CV timeout enabled',
      ccTimeoutEn:  'CC timeout enabled',
      fvTimeoutEn:  'FV timeout enabled',
    };
    for (const [k, label] of Object.entries(flagNames)) {
      if (before[k] !== after[k]) {
        diff.push({
          name:    'curve_config',
          desc:    label,
          oldText: before[k] ? 'on' : 'off',
          newText: after[k]  ? 'on' : 'off',
        });
      }
    }
    }
  }
  if ('chg_rst_vbat' in cfgWrites) {
    diff.push({
      name:    'chg_rst_vbat',
      desc:    'Restart trigger voltage',
      oldText: currentRestartV === null ? '—' : currentRestartV.toFixed(1) + ' V',
      newText: cfgWrites.chg_rst_vbat.toFixed(1) + ' V',
    });
  }
  return { settings, diff };
}

let _pendingWrites = null;  // {settings, diff} captured when modal opens

function _buildDiffBody(container, diff) {
  container.replaceChildren();
  for (const d of diff) {
    const row = document.createElement('div');
    row.className = 'diff-row';
    const label = document.createElement('div');
    label.className = 'label';
    label.appendChild(document.createTextNode(d.desc));
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = d.name;
    label.appendChild(name);
    const val = document.createElement('div');
    val.className = 'val';
    const old = document.createElement('span');
    old.className = 'old';
    old.textContent = d.oldText;
    const arrow = document.createElement('span');
    arrow.className = 'arrow';
    arrow.textContent = '→';
    const neu = document.createElement('span');
    neu.className = 'new';
    neu.textContent = d.newText;
    val.append(old, arrow, neu);
    row.append(label, val);
    container.appendChild(row);
  }
}

function _setModalWarn(isOn) {
  const warn = document.getElementById('diff-warn');
  warn.replaceChildren();
  if (isOn) {
    const strong = document.createElement('strong');
    strong.textContent = 'Heads up:';
    warn.appendChild(strong);
    warn.appendChild(document.createTextNode(
      ' the output is currently ON. Applying will briefly switch the charger OFF, '
      + 'write all changes, then switch it back ON — a connected battery will see '
      + 'charge current drop and resume.'));
  } else {
    warn.textContent = 'Output is currently OFF; settings will be written directly '
      + 'without a power cycle.';
  }
}

async function openDiffModal() {
  if (!hasReloadedFromCharger) {
    log('reload from charger before applying to hardware', 'err');
    return;
  }
  const { settings, diff } = gatherDirtyWrites();
  if (!Object.keys(settings).length) return;
  _pendingWrites = { settings, diff };

  _buildDiffBody(document.getElementById('diff-body'), diff);

  let isOn = false;
  try {
    const r = await fetchJSON('/api/operation');
    isOn = !!r.on;
  } catch { /* keep isOn=false */ }
  _setModalWarn(isOn);

  _modalReturnFocus = document.activeElement;
  document.getElementById('diff-modal').hidden = false;
  document.getElementById('diff-cancel').focus();
}

function closeDiffModal() {
  document.getElementById('diff-modal').hidden = true;
  _pendingWrites = null;
  if (_modalReturnFocus && typeof _modalReturnFocus.focus === 'function') {
    _modalReturnFocus.focus();
  }
  _modalReturnFocus = null;
}

async function confirmDiffModal() {
  if (!_pendingWrites) return closeDiffModal();
  const { settings } = _pendingWrites;
  closeDiffModal();
  await applyChanges(settings);
}

async function applyChanges(settings) {
  const apply = document.getElementById('apply');
  apply.classList.add('writing');
  apply.disabled = true;
  document.getElementById('discard').disabled = true;
  try {
    log('writing: ' + Object.entries(settings).map(([k,v]) => {
      const reg = registers[k];
      return reg && reg.scale === 1 && !reg.unit
        ? `${k}=0x${v.toString(16).toUpperCase().padStart(4,'0')}`
        : `${k}=${v}`;
    }).join(', '));
    const r = await fetchJSON('/api/write', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ settings })
    });
    if (r.cycled) log('write OK (output cycled OFF→ON)', 'ok');
    else          log('write OK (output stayed off)', 'ok');
    // Flag any value that landed different from what we requested (clamp
    // / rounding by the firmware).  Helpful when, say, you ask for
    // curve_cv=58.5 V on a hard-clamped firmware.
    if (r.post) {
      for (const [k, v] of Object.entries(settings)) {
        const got = r.post[k];
        if (!got || got.value === null) continue;
        const same = Math.abs(Number(got.value) - Number(v)) < 1e-6;
        if (!same) {
          log(`note: ${k} requested ${v}, charger reports ${got.value}`, 'warn');
        }
      }
    }
    await reloadFromCharger();
  } catch (e) {
    log('write failed: ' + e.message, 'err');
  } finally {
    apply.classList.remove('writing');
    updateDirtyCount();
  }
}

function discardChanges() {
  const hasEdits = document.querySelectorAll(
    '#settings input.changed, #config-row .check.changed, #config-row input.num.changed, #cfg-raw.changed'
  ).length;
  if (!hasEdits) return;
  if (!hasReloadedFromCharger) {
    clearSuggestedPreviewForm();
    updateConfigSummary();
    log('cleared suggested preview', 'ok');
    return;
  }
  for (const name of SETTING_KEYS) {
    const inp = document.querySelector(`#settings input[data-name="${name}"]`);
    if (!inp) continue;
    if (currentValues[name] !== undefined) {
      inp.value = formatInputValue(currentValues[name], registers[name]);
    }
    inp.classList.remove('changed', 'invalid', 'skipped');
  }
  if (currentCurveConfig !== null) {
    paintConfigRow({...decodeCurveConfig(currentCurveConfig),
                    restartV: currentRestartV ?? 48.0});
  }
  refreshRawHexFromState();
  updateDirtyCount();
  drawCurvePreview();
  log('discarded pending changes', 'ok');
}

// ---------- raw curve_config hex (power-user details) ---------------------

function refreshRawHexFromState() {
  const inp = document.getElementById('cfg-raw');
  const dec = document.getElementById('cfg-raw-decoded');
  if (!inp) return;
  if (currentCurveConfig === null) {
    inp.value = ''; dec.textContent = '(reload from charger first)';
    inp.disabled = true;
    return;
  }
  inp.disabled = false;
  inp.classList.remove('changed', 'invalid');
  inp.value = '0x' + currentCurveConfig.toString(16).toUpperCase().padStart(4,'0');
  dec.textContent = _decodeCurveConfigBrief(currentCurveConfig);
}

function _decodeCurveConfigBrief(v) {
  // Mirrors charger_app._decode_curve_config but shortened for the side label.
  const cuvs = ['custom','preset1','preset2','preset3'][v & 0b11];
  const tcs  = ['off','-3mV','-4mV','-5mV'][(v >> 2) & 0b11];
  const flags = [];
  if (v & 0x0080) flags.push('charger'); else flags.push('PSU');
  if (v & 0x0020) flags.push('float-after-CV');
  if (v & 0x0800) flags.push('restart-on-Vbat');
  if (v & 0x0100) flags.push('cv-timeout');
  if (v & 0x0200) flags.push('cc-timeout');
  if (v & 0x0400) flags.push('fv-timeout');
  return `curve=${cuvs}, tcomp=${tcs}, ${flags.join(', ')}`;
}

document.getElementById('cfg-raw').addEventListener('input', (e) => {
  onUserEditedBeforeReload();
  const inp = e.target;
  const dec = document.getElementById('cfg-raw-decoded');
  inp.classList.remove('changed', 'invalid');
  const t = inp.value.trim();
  if (!t) { dec.textContent = ''; return; }
  let n;
  try {
    n = t.toLowerCase().startsWith('0x') ? parseInt(t, 16) : parseInt(t, 10);
  } catch { n = NaN; }
  if (Number.isNaN(n) || n < 0 || n > 0xFFFF) {
    inp.classList.add('invalid');
    dec.textContent = 'must be a 16-bit value (0x0000–0xFFFF)';
    return;
  }
  dec.textContent = _decodeCurveConfigBrief(n);
  if (currentCurveConfig !== null && n !== currentCurveConfig) {
    inp.classList.add('changed');
    // Hex edits override the checkbox row.  Re-paint the friendly UI so
    // the user sees what their hex implies, and update currentCurveConfig
    // proxy in a *pending* way (not committing to wire yet).
    const decoded = decodeCurveConfig(n);
    paintConfigRow({...decoded, restartV: currentRestartV});
    // Manually mark the friendly checkboxes as changed since paintConfigRow
    // would have compared to the *original* currentCurveConfig.
    refreshConfigRowChangedClasses();
  }
  updateDirtyCount();
});

// ---------- button wiring + keyboard shortcuts ----------------------------

document.getElementById('apply').addEventListener('click', openDiffModal);
document.getElementById('reload').addEventListener('click', reloadFromCharger);
document.getElementById('reload-banner-btn')?.addEventListener('click', reloadFromCharger);
document.getElementById('export-settings')?.addEventListener('click', exportSettings);
document.getElementById('import-settings')?.addEventListener('click', () => {
  document.getElementById('import-file')?.click();
});
document.getElementById('import-file')?.addEventListener('change', (e) => {
  const file = e.target.files && e.target.files[0];
  importSettingsFromFile(file);
  e.target.value = '';
});
document.getElementById('log-clear')?.addEventListener('click', clearLog);
document.getElementById('discard').addEventListener('click', discardChanges);
document.getElementById('diff-cancel').addEventListener('click', closeDiffModal);
document.getElementById('diff-confirm').addEventListener('click', confirmDiffModal);
document.getElementById('diff-modal').addEventListener('click', (e) => {
  // Backdrop click closes the modal.
  if (e.target.id === 'diff-modal') closeDiffModal();
});

document.addEventListener('keydown', (e) => {
  const modal = document.getElementById('diff-modal');
  const inModal = modal && !modal.hidden;
  if (inModal && e.key === 'Tab') {
    const box = modal.querySelector('.modal');
    const focusable = [...box.querySelectorAll('button, [href], input, select, textarea')]
      .filter(el => !el.disabled);
    if (focusable.length) {
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  }
  if ((e.metaKey || e.ctrlKey) && e.key === 's') {
    e.preventDefault();
    if (!document.getElementById('apply').disabled) openDiffModal();
    return;
  }
  if ((e.metaKey || e.ctrlKey) && e.shiftKey && (e.key === 'r' || e.key === 'R')) {
    e.preventDefault();
    reloadFromCharger();
    return;
  }
  if (e.key === 'Escape') {
    if (inModal) { closeDiffModal(); return; }
    if (!document.getElementById('discard').disabled) discardChanges();
  }
});

document.getElementById('onoff').addEventListener('change', async (e) => {
  const target = e.target.checked;
  const msg = target
    ? 'Turn charger OUTPUT ON?\n\nA connected battery may begin charging.'
    : 'Turn charger OUTPUT OFF?\n\nCharge current will stop.';
  if (!confirm(msg)) {
    e.target.checked = !target;
    return;
  }
  userToggling = Date.now();
  e.target.disabled = true;
  try {
    await fetchJSON(target ? '/api/on' : '/api/off', { method: 'POST' });
    log('output ' + (target ? 'ON' : 'OFF'), 'ok');
  } catch (err) {
    log('toggle failed: ' + err.message, 'err');
    e.target.checked = !target;
  } finally {
    e.target.disabled = false;
  }
});

// Boot sequence: restore persisted log, detect demo vs real CAN, load
// register metadata, then SSE / device-info polls.
async function bootApp() {
  _loadPersistedLog();
  applyDemoModeFromHealth(await fetchHealthWithRetry());
  try {
    await loadRegisters();
    drawCurvePreview();
    refreshRawHexFromState();
    updateDirtyCount();
    if (isDemoMode) scheduleSuggestedDefaults();
  } catch (e) {
    log('register load failed: ' + e.message, 'err');
  }
  refreshDeviceInfo();
  startStream();
  requestAnimationFrame((t) => { lastFrame = t; tickCurveDot(t); });
  setInterval(refreshDeviceInfo, 60000);
}
bootApp();
