'use strict';
/*
 * Dashboard client. Tile definitions come from /api/tiles, current values
 * from /api/now (polled), history from /api/history. No dependencies.
 */
(function () {
  var GROUP_NAMES = { usage: 'Usage', io: 'I/O', temps: 'Temperatures', energy: 'Energy', fans: 'Fans' };
  var CHIP_NAMES = { all: 'All', usage: 'Usage', io: 'I/O', temps: 'Temps', energy: 'Energy', fans: 'Fans' };
  var WINDOW = { fast: 1800, '6h': 21600, '24h': 86400 };
  var GAP = { fast: 10, '6h': 120, '24h': 120 };   // seconds without data that break a line
  var POLL_MS = 2000;
  var FAST_POINTS = 900;

  var S = {
    range: 'fast', filter: 'all', rev: -1, title: document.title,
    groups: [], tiles: [], cards: [],
    hist: { t: [], series: {} },
    now: {}, notes: {}, info: {}, ts: 0,
    online: null, ticks: 0, timer: null, tilesPending: false
  };

  // ---- small helpers -----------------------------------------------------

  function $(id) { return document.getElementById(id); }

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function getJSON(url) {
    return fetch(url, { cache: 'no-store', credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.json();
    });
  }

  var cssCache = {};
  function css(name) {
    if (!(name in cssCache)) cssCache[name] = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return cssCache[name];
  }

  function store(key, value) {
    try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch (e) { /* private mode */ }
    return null;
  }

  // ---- formatting ----------------------------------------------------------

  function fmtNum(v, dec) { return v == null || isNaN(v) ? '--' : Number(v).toFixed(dec); }

  function fmtRate(v) {
    var units = ['B/s', 'kB/s', 'MB/s', 'GB/s'], i = 0;
    if (v == null || isNaN(v)) return ['--', 'B/s'];
    while (Math.abs(v) >= 1000 && i < units.length - 1) { v /= 1000; i++; }
    var dec = i === 0 ? 0 : v < 10 ? 2 : v < 100 ? 1 : 0;
    return [v.toFixed(dec), units[i]];
  }

  function fmtValue(tile, v) {
    return tile.format === 'rate' ? fmtRate(v) : [fmtNum(v, tile.decimals || 0), tile.unit || ''];
  }

  function fmtAxis(tile, v, range) {
    if (tile.format === 'rate') return fmtRate(v).join(' ');
    return range >= 10 ? String(Math.round(v)) : Number(v).toFixed(1);
  }

  function fmtTime(ts, seconds) {
    var opts = { hour: '2-digit', minute: '2-digit' };
    if (seconds) opts.second = '2-digit';
    return new Date(ts * 1000).toLocaleTimeString([], opts);
  }

  function fmtUptime(s) {
    var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
    return (d ? d + 'd ' : '') + h + 'h ' + m + 'm';
  }

  function level(tile, v) {
    if (v == null || (tile.warn == null && tile.hot == null && tile.low == null)) return '';
    if (tile.low != null && v <= tile.low) return 'warn';
    if (tile.hot != null && v >= tile.hot) return 'hot';
    if (tile.warn != null && v >= tile.warn) return 'warn';
    return 'ok';
  }

  function seriesColor(tile, i) { return i === 0 ? css('--' + tile.group) : css('--second'); }

  // ---- filter chips ----------------------------------------------------------

  function readFilter() {
    var f = location.hash.slice(1) || store('filter') || 'all';
    return CHIP_NAMES[f] ? f : 'all';
  }

  function buildChips() {
    var nav = $('chips'), counts = {};
    nav.textContent = '';
    S.tiles.forEach(function (t) { counts[t.group] = (counts[t.group] || 0) + 1; });
    ['all'].concat(S.groups).forEach(function (g) {
      if (g !== 'all' && !counts[g] && S.filter !== g) return;
      var b = el('button', 'chip');
      b.type = 'button';
      b.dataset.filter = g;
      if (g !== 'all') { var dot = el('i'); dot.style.background = css('--' + g); b.appendChild(dot); }
      b.appendChild(document.createTextNode(CHIP_NAMES[g]));
      b.appendChild(el('span', 'n', String(g === 'all' ? S.tiles.length : counts[g] || 0)));
      nav.appendChild(b);
    });
    applyFilter();
  }

  function applyFilter() {
    var f = S.filter;
    Array.prototype.forEach.call($('chips').children, function (b) {
      b.setAttribute('aria-pressed', String(b.dataset.filter === f));
    });
    Array.prototype.forEach.call(document.querySelectorAll('#app section'), function (sec) {
      sec.hidden = f !== 'all' && sec.dataset.group !== f;
    });
    $('app').classList.toggle('only', f !== 'all');
    var empty = $('empty');
    if (empty) empty.hidden = f === 'all' || !!document.querySelector('#app section[data-group="' + f + '"]');
    drawAll();
  }

  function setFilter(f) {
    S.filter = f;
    store('filter', f);
    history.replaceState(null, '', f === 'all' ? location.pathname : '#' + f);
    applyFilter();
    var on = document.querySelector('.chip[aria-pressed="true"]');
    if (on && on.scrollIntoView) on.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }

  // ---- cards ---------------------------------------------------------------

  function makeCard(tile) {
    var card = { tile: tile, hx: null }, root = el('div', 'card');
    var head = el('div', 'head');
    head.appendChild(el('span', 'label', tile.label));
    if (tile.source) {
      head.appendChild(el('span', 'badge ' + tile.source, tile.source === 'measured' ? 'measured' : 'est.'));
    }
    root.appendChild(head);

    if (tile.series.length > 1) {
      card.multi = el('div', 'multi');
      card.parts = tile.series.map(function (s, i) {
        var span = el('span'), dot = el('i'), num = el('b'), unit = el('small');
        dot.style.background = seriesColor(tile, i);
        span.appendChild(dot); span.appendChild(num); span.appendChild(unit);
        card.multi.appendChild(span);
        return { num: num, unit: unit, name: s[1] };
      });
      root.appendChild(card.multi);
    } else {
      card.value = el('div', 'value');
      card.num = document.createTextNode('--');
      card.unit = el('small');
      card.value.appendChild(card.num);
      card.value.appendChild(card.unit);
      root.appendChild(card.value);
    }
    card.notes = el('div', 'notes');
    root.appendChild(card.notes);
    card.root = root;

    if (tile.chart === 'bar') {
      root.classList.add('has-meter');
      card.meter = el('div', 'meter');
      card.meter.setAttribute('role', 'meter');
      card.meter.setAttribute('aria-label', tile.label);
      card.meter.setAttribute('aria-valuemin', String(tile.min));
      card.meter.setAttribute('aria-valuemax', String(tile.max));
      card.fill = el('div', 'fill');
      card.meter.appendChild(card.fill);
      [tile.warn, tile.hot].forEach(function (mark) {
        if (mark == null) return;
        var m = el('i');
        m.style.left = meterPct(tile, mark) + '%';
        card.meter.appendChild(m);
      });
      root.appendChild(card.meter);
      return card;
    }

    card.canvas = el('canvas');
    card.canvas.setAttribute('role', 'img');
    card.canvas.setAttribute('aria-label', tile.label + ' history');
    root.appendChild(card.canvas);

    function move(e) { var b = card.canvas.getBoundingClientRect(); card.hx = e.clientX - b.left; draw(card); }
    function leave() { card.hx = null; draw(card); }
    card.canvas.addEventListener('pointermove', move);
    card.canvas.addEventListener('pointerdown', move);
    card.canvas.addEventListener('pointerleave', leave);
    card.canvas.addEventListener('pointercancel', leave);
    return card;
  }

  function meterPct(tile, v) {
    return Math.max(0, Math.min(100, (v - tile.min) / (tile.max - tile.min) * 100));
  }

  function build() {
    var app = $('app');
    app.textContent = '';
    S.cards = [];
    S.groups.forEach(function (g) {
      var mine = S.tiles.filter(function (t) { return t.group === g; });
      if (!mine.length) return;
      var sec = el('section'), h = el('h2'), dot = el('i'), grid = el('div', 'grid');
      sec.dataset.group = g;
      dot.style.background = css('--' + g);
      h.appendChild(dot);
      h.appendChild(document.createTextNode(GROUP_NAMES[g] || g));
      sec.appendChild(h);
      mine.forEach(function (t) {
        var card = makeCard(t);
        S.cards.push(card);
        grid.appendChild(card.root);
      });
      sec.appendChild(grid);
      app.appendChild(sec);
    });
    var empty = el('p', 'empty', 'No sensors found in this category.');
    empty.id = 'empty';
    app.appendChild(empty);
    buildChips();
    updateValues();
  }

  function updateValues() {
    S.cards.forEach(function (card) {
      var t = card.tile;
      if (card.parts) {
        card.parts.forEach(function (p, i) {
          var f = fmtValue(t, S.now[t.series[i][0]]);
          p.num.textContent = f[0];
          p.unit.textContent = f[1] + ' ' + p.name;
        });
      } else {
        var v = S.now[t.id], f = fmtValue(t, v);
        card.num.nodeValue = f[0];
        card.unit.textContent = f[1];
        card.value.className = 'value ' + level(t, v);
        if (card.meter) {
          card.fill.style.width = (v == null ? 0 : meterPct(t, v)) + '%';
          card.fill.className = 'fill ' + level(t, v);
          card.meter.setAttribute('aria-valuenow', v == null ? '' : String(v));
        }
      }
      var text = S.notes[t.id] || '';
      if (card.noteText !== text) {
        card.noteText = text;
        card.notes.textContent = '';
        text.split('\n').forEach(function (line) { if (line) card.notes.appendChild(el('div', null, line)); });
      }
    });
  }

  // ---- charts --------------------------------------------------------------

  function draw(card) {
    if (!card.canvas) return;   // meter tiles have no history chart
    var cv = card.canvas, w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;   // hidden by the category filter
    var dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
    }
    var c = cv.getContext('2d');
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.clearRect(0, 0, w, h);

    var tile = card.tile, t = S.hist.t, n = t.length, i;
    var PT = 6, PB = 16, ph = h - PT - PB;
    var mut = css('--mut'), line = css('--line');
    c.font = '11px system-ui, -apple-system, sans-serif';
    if (n < 2) { c.fillStyle = mut; c.fillText('collecting data…', 4, h / 2); return; }

    var tEnd = t[n - 1], span = Math.max(60, Math.min(WINDOW[S.range], tEnd - t[0])), tStart = tEnd - span;
    var keys = tile.series.map(function (s) { return s[0]; });

    // y-range
    var lo = Infinity, hi = -Infinity;
    keys.forEach(function (k) {
      var a = S.hist.series[k] || [];
      for (i = 0; i < n; i++) {
        if (t[i] < tStart || a[i] == null) continue;
        if (a[i] < lo) lo = a[i];
        if (a[i] > hi) hi = a[i];
      }
    });
    if (lo === Infinity) { lo = 0; hi = 1; }
    var minSpan = tile.span || 1;
    if (tile.min != null && tile.max != null) { lo = tile.min; hi = tile.max; }
    else if (tile.zero) { lo = 0; hi = Math.max(hi * 1.15, minSpan); }
    else {
      var pad = Math.max((hi - lo) * 0.2, minSpan * 0.25);
      lo -= pad; hi += pad;
      if (hi - lo < minSpan) { var mid = (hi + lo) / 2; lo = mid - minSpan / 2; hi = mid + minSpan / 2; }
    }
    function X(ts) { return (ts - tStart) / span * w; }
    function Y(v) { return PT + ph - (v - lo) / (hi - lo) * ph; }

    // grid and labels
    c.strokeStyle = line; c.lineWidth = 1;
    for (i = 0; i < 3; i++) {
      var gy = Math.round(PT + ph * i / 2) + 0.5;
      c.beginPath(); c.moveTo(0, gy); c.lineTo(w, gy); c.stroke();
    }
    c.fillStyle = mut;
    c.fillText(fmtAxis(tile, hi, hi - lo), 3, PT + 10);
    c.fillText(fmtAxis(tile, lo, hi - lo), 3, PT + ph - 3);
    c.fillText(fmtTime(tStart), 3, h - 3);
    var te = fmtTime(tEnd);
    c.fillText(te, w - c.measureText(te).width - 3, h - 3);

    // series, split into segments wherever data is missing
    keys.forEach(function (k, si) {
      var a = S.hist.series[k] || [], col = seriesColor(tile, si), segs = [], seg = null, lastT = 0;
      for (i = 0; i < n; i++) {
        if (t[i] < tStart) continue;
        if (a[i] == null || (seg && t[i] - lastT > GAP[S.range])) { seg = null; if (a[i] == null) continue; }
        if (!seg) { seg = []; segs.push(seg); }
        seg.push([X(t[i]), Y(a[i])]);
        lastT = t[i];
      }
      segs.forEach(function (pts) {
        c.beginPath();
        pts.forEach(function (p, j) { if (j) c.lineTo(p[0], p[1]); else c.moveTo(p[0], p[1]); });
        c.strokeStyle = col; c.lineWidth = 1.8; c.lineJoin = 'round'; c.stroke();
        if (keys.length === 1 && pts.length > 1) {
          var g = c.createLinearGradient(0, PT, 0, PT + ph);
          g.addColorStop(0, col + '50'); g.addColorStop(1, col + '00');
          c.lineTo(pts[pts.length - 1][0], PT + ph); c.lineTo(pts[0][0], PT + ph); c.closePath();
          c.fillStyle = g; c.fill();
        }
      });
    });

    // hover read-out
    if (card.hx == null) return;
    var tt = tStart + Math.max(0, Math.min(w, card.hx)) / w * span, bi = -1, bd = Infinity;
    for (i = 0; i < n; i++) {
      if (t[i] < tStart) continue;
      var dd = Math.abs(t[i] - tt);
      if (dd < bd) { bd = dd; bi = i; }
    }
    if (bi < 0) return;
    var px = X(t[bi]), parts = [];
    c.strokeStyle = mut; c.lineWidth = 1;
    c.beginPath(); c.moveTo(Math.round(px) + 0.5, PT); c.lineTo(Math.round(px) + 0.5, PT + ph); c.stroke();
    keys.forEach(function (k, si) {
      var v = (S.hist.series[k] || [])[bi];
      if (v != null) {
        c.fillStyle = seriesColor(tile, si);
        c.beginPath(); c.arc(px, Y(v), 3.5, 0, 2 * Math.PI); c.fill();
      }
      parts.push(fmtValue(tile, v).join(' ').trim());
    });
    var label = parts.join(' / ') + '  ' + fmtTime(t[bi], S.range === 'fast');
    var tw = c.measureText(label).width + 12, bx = px + 8;
    if (bx + tw > w) bx = px - 8 - tw;
    if (bx < 0) bx = 0;
    c.fillStyle = css('--card'); c.globalAlpha = 0.94; c.fillRect(bx, PT + 2, tw, 18); c.globalAlpha = 1;
    c.strokeStyle = line; c.strokeRect(bx + 0.5, PT + 2.5, tw, 18);
    c.fillStyle = css('--fg'); c.fillText(label, bx + 6, PT + 15);
  }

  function drawAll() { S.cards.forEach(draw); }

  // ---- data ------------------------------------------------------------------

  function setStatus() {
    var bar = $('status'), text;
    bar.className = S.online ? 'online' : S.online === false ? 'offline' : '';
    if (S.online === false) text = 'offline · retrying';
    else if (!S.ts) text = 'connecting…';
    else {
      text = fmtTime(S.ts, true);
      if (S.info.uptime != null) text = 'up ' + fmtUptime(S.info.uptime) + ' · ' + text;
    }
    $('status-text').textContent = text;
  }

  function loadTiles() {
    if (S.tilesPending) return;
    S.tilesPending = true;
    getJSON('/api/tiles').then(function (d) {
      S.rev = d.rev;
      S.groups = d.groups;
      S.tiles = d.tiles;
      build();
      drawAll();
    }).catch(function () {}).then(function () { S.tilesPending = false; });
  }

  function loadHistory() {
    var range = S.range;
    getJSON('/api/history?tier=' + (range === 'fast' ? 'fast' : 'slow')).then(function (h) {
      if (range !== S.range) return;   // user switched again meanwhile
      if (range === '6h' && h.t.length) {
        var cut = h.t[h.t.length - 1] - WINDOW['6h'], start = 0;
        while (start < h.t.length && h.t[start] < cut) start++;
        h.t = h.t.slice(start);
        for (var k in h.series) h.series[k] = h.series[k].slice(start);
      }
      S.hist = h;
      drawAll();
    }).catch(function () {});
  }

  // In the 30 min view new samples are appended locally instead of refetching.
  function appendNow() {
    var h = S.hist, n = h.t.length, k;
    if (!S.ts || (n && S.ts <= h.t[n - 1])) return;
    h.t.push(S.ts);
    for (k in S.now) {
      if (!h.series[k]) { h.series[k] = []; for (var i = 0; i < n; i++) h.series[k].push(null); }
    }
    for (k in h.series) h.series[k].push(S.now[k] != null ? S.now[k] : null);
    if (h.t.length > FAST_POINTS) {
      h.t.shift();
      for (k in h.series) h.series[k].shift();
    }
  }

  function tick() {
    getJSON('/api/now').then(function (d) {
      S.online = true;
      S.now = d.v || {};
      S.notes = d.notes || {};
      S.info = d.info || {};
      S.ts = d.ts;
      if (d.rev !== S.rev) loadTiles();
      S.ticks++;
      if (S.range === 'fast') appendNow();
      else if (S.ticks % 15 === 0) loadHistory();
      updateValues();
      if (S.range === 'fast') drawAll();
      setStatus();
    }).catch(function () { S.online = false; setStatus(); });
  }

  function startPolling() {
    if (S.timer) return;
    tick();
    S.timer = setInterval(tick, POLL_MS);
  }

  function stopPolling() { clearInterval(S.timer); S.timer = null; }

  // ---- wiring --------------------------------------------------------------

  $('range').addEventListener('click', function (e) {
    var b = e.target.closest('button');
    if (!b || b.dataset.range === S.range) return;
    S.range = b.dataset.range;
    store('range', S.range);
    Array.prototype.forEach.call(this.querySelectorAll('button'), function (x) {
      x.setAttribute('aria-pressed', String(x === b));
    });
    loadHistory();
  });

  $('chips').addEventListener('click', function (e) {
    var b = e.target.closest('.chip');
    if (b) setFilter(b.dataset.filter);
  });

  window.addEventListener('hashchange', function () { S.filter = readFilter(); applyFilter(); });

  var resizeQueued = false;
  window.addEventListener('resize', function () {
    if (resizeQueued) return;
    resizeQueued = true;
    requestAnimationFrame(function () { resizeQueued = false; drawAll(); });
  });

  var scheme = window.matchMedia('(prefers-color-scheme: light)');
  var onScheme = function () { cssCache = {}; build(); drawAll(); };
  if (scheme.addEventListener) scheme.addEventListener('change', onScheme);

  // Pause while the tab is hidden; catch up on history when it comes back.
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { stopPolling(); return; }
    loadHistory();
    startPolling();
  });

  S.filter = readFilter();
  if (S.filter !== 'all' && location.hash !== '#' + S.filter) history.replaceState(null, '', '#' + S.filter);
  var savedRange = store('range');
  if (WINDOW[savedRange]) {
    S.range = savedRange;
    Array.prototype.forEach.call(document.querySelectorAll('#range button'), function (x) {
      x.setAttribute('aria-pressed', String(x.dataset.range === S.range));
    });
  }
  loadTiles();
  loadHistory();
  startPolling();
})();
