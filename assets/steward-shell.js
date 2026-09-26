// Repo Steward shell: the system bar every page shares.
//
// Mounts on <body data-page="operations|insights|evaluation|metrics|audit">.
// Owns everything global: navigation, live status (mode, engine, schedule,
// tick / sweep / build state, site uptime), the run/stop tick control, the
// settings popover, keyboard shortcuts, dialogs and toasts. Pages use the
// helpers on window.Steward and render only their own content.
(function () {
  var THEME_KEY = 'steward-theme';
  try { var t = localStorage.getItem(THEME_KEY); if (t) document.documentElement.dataset.theme = t; } catch (e) {}

  var PAGES = [
    { id: 'operations', href: '/dashboard.html', label: 'Operations', key: 'o' },
    { id: 'insights', href: '/insights.html', label: 'Insights', key: 'i' },
    { id: 'evaluation', href: '/evaluation.html', label: 'Evaluation', key: 'e' },
    { id: 'metrics', href: '/metrics.html', label: 'Metrics', key: 'm' },
    { id: 'audit', href: '/audit.html', label: 'Audit', key: 'a' }
  ];
  var page = document.body.dataset.page || '';
  var listeners = [];
  var status = null;

  // ---------- helpers ----------
  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function api(path, body) {
    var opts = body === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
    };
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (x) {
        if (!r.ok || x.error) throw new Error(x.error || (path + ' failed (' + r.status + ')'));
        return x;
      });
    });
  }
  function fmtDur(s) {
    if (s == null) return '—';
    s = Math.max(0, Math.round(s));
    var h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), r = s % 60;
    return h ? h + 'h' + String(m).padStart(2, '0') + 'm' : m ? m + 'm' + String(r).padStart(2, '0') + 's' : r + 's';
  }
  function ago(iso) {
    var t = Date.parse(iso);
    if (isNaN(t)) return '';
    var s = (Date.now() - t) / 1000;
    if (s < 90) return 'just now';
    if (s < 5400) return Math.round(s / 60) + 'm ago';
    if (s < 129600) return Math.round(s / 3600) + 'h ago';
    return Math.round(s / 86400) + 'd ago';
  }
  function stamp(iso) { return iso ? String(iso).replace('T', ' ').replace(/(:\d\d)?Z$/, 'Z') : ''; }

  var toastWrap = document.createElement('div');
  toastWrap.className = 'toasts';
  toastWrap.setAttribute('aria-live', 'polite');
  function toast(msg, kind) {
    var t = document.createElement('div');
    t.className = 'toast ' + (kind || '');
    t.textContent = msg;
    toastWrap.appendChild(t);
    setTimeout(function () { t.style.opacity = 0; setTimeout(function () { t.remove(); }, 250); }, kind === 'error' ? 9000 : 6000);
  }

  // modal({title, body(html), confirm, cancel, tone: 'sig'|'crit'|'', input: placeholder})
  // resolves false on cancel, true on confirm, or the typed text when input is set.
  function modal(o) {
    return new Promise(function (resolve) {
      var ov = document.createElement('div');
      ov.className = 'overlay';
      ov.innerHTML = '<div class="dialog" role="dialog" aria-modal="true"><header class="' + esc(o.tone || '') + '">' + esc(o.title) + '</header>' +
        '<div class="body">' + (o.body || '') + (o.input != null ? '<textarea class="field" placeholder="' + esc(o.input) + '"></textarea>' : '') + '</div>' +
        '<footer><button class="btn ghost" data-a="no">' + esc(o.cancel || 'Cancel') + '</button>' +
        '<button class="btn ' + (o.tone === 'crit' ? 'crit' : 'primary') + '" data-a="yes">' + esc(o.confirm || 'Confirm') + '</button></footer></div>';
      document.body.appendChild(ov);
      var input = ov.querySelector('textarea');
      function close(v) { ov.remove(); document.removeEventListener('keydown', key, true); resolve(v); }
      function yes() { close(input ? input.value.trim() : true); }
      function key(e) {
        if (e.key === 'Escape') { e.stopPropagation(); close(false); }
        else if (e.key === 'Enter' && (!input || e.metaKey || e.ctrlKey)) { e.preventDefault(); yes(); }
      }
      document.addEventListener('keydown', key, true);
      ov.addEventListener('click', function (e) { if (e.target === ov) close(false); });
      ov.querySelector('[data-a=no]').addEventListener('click', function () { close(false); });
      ov.querySelector('[data-a=yes]').addEventListener('click', yes);
      (input || ov.querySelector('[data-a=yes]')).focus();
    });
  }

  // Keyboard row navigation for a page: j/k move, enter/o activates.
  // rows() returns the currently visible row elements; open(row) acts on one.
  var workListeners = [], work = null;
  var navTarget = null;
  function nav(opts) { navTarget = opts; }
  function moveNav(delta) {
    if (!navTarget) return;
    var rows = navTarget.rows().filter(function (r) { return r.offsetParent !== null; });
    if (!rows.length) return;
    var cur = rows.indexOf(document.querySelector('.sel[data-nav]'));
    var next = rows[Math.max(0, Math.min(rows.length - 1, cur < 0 ? 0 : cur + delta))];
    rows.forEach(function (r) { r.classList.toggle('sel', r === next); });
    next.scrollIntoView({ block: 'nearest' });
    if (navTarget.select) navTarget.select(next);
  }

  // ---------- system bar ----------
  var bar = document.createElement('header');
  bar.className = 'sys';
  bar.innerHTML =
    '<a class="brand" href="/dashboard.html" title="Repo Steward"><i></i><span>Repo Steward</span></a>' +
    '<nav class="tabs" aria-label="Primary navigation">' + PAGES.map(function (p) {
      return '<a href="' + p.href + '"' + (p.id === page ? ' aria-current="page"' : '') +
        ' title="g ' + p.key + '">' + p.label + '</a>';
    }).join('') + '</nav>' +
    '<span class="spacer"></span>' +
    '<div class="cells">' +
      '<span class="cells-sites" style="display:flex"></span>' +
      '<button class="cell mode" data-cell="mode" title="Posting mode"><span>Mode</span><strong>—</strong></button>' +
      '<button class="cell opt" data-cell="engine" title="Engine and model for ticks, sweeps and builds"><span>Engine</span><strong>—</strong></button>' +
      '<button class="cell opt" data-cell="sched" title="Tick schedule"><span>Sched</span><strong>—</strong></button>' +
      '<button class="cell" data-cell="work" title="What the steward is doing and has queued (w)"><span>Work</span><i class="dot"></i><strong>—</strong></button>' +
      '<span class="cell" data-cell="tick" title="Last tick"><span>Tick</span><i class="dot"></i><strong>—</strong></span>' +
    '</div>' +
    '<button class="act stop" data-act="stop" hidden title="Stop the running tick">■ Stop</button>' +
    '<button class="act primary" data-act="run" title="Run one tick now">▶ Run tick</button>' +
    '<button class="act" data-act="settings" title="Settings">Settings</button>';

  var progress = document.createElement('div');
  progress.className = 'progress';
  progress.hidden = true;
  progress.innerHTML = '<span class="label">Tick</span><span class="track"><span class="fill"></span></span><span class="txt"></span>';

  document.body.insertBefore(progress, document.body.firstChild);
  document.body.insertBefore(bar, document.body.firstChild);
  document.body.appendChild(toastWrap);

  var cell = function (n) { return bar.querySelector('[data-cell=' + n + ']'); };
  var runBtn = bar.querySelector('[data-act=run]'), stopBtn = bar.querySelector('[data-act=stop]');

  function paintStatus(s) {
    var live = s.mode === 'live';
    var m = cell('mode');
    m.className = 'cell mode ' + (live ? 'live' : 'draft');
    m.querySelector('strong').textContent = live ? 'LIVE' : 'DRAFT';
    m.title = live ? 'Live: the steward posts to GitHub. Click to switch to draft.' : 'Draft: nothing is posted. Click to go live.';
    var b = s.backend || {};
    cell('engine').querySelector('strong').textContent = (b.value || '?') + (b.model ? ' · ' + b.model : '');
    cell('sched').querySelector('strong').textContent = (s.schedule && s.schedule.preset) || 'manual';
    var tick = cell('tick'), dot = tick.querySelector('.dot'), txt = tick.querySelector('strong');
    var last = s.last_tick || {};
    if (s.tick_active) {
      dot.className = 'dot run'; txt.textContent = 'RUNNING ' + fmtDur(s.elapsed_sec);
    } else if (last.ts) {
      dot.className = 'dot ' + (last.ok ? 'ok' : last.rc === 75 ? 'warn' : 'crit');
      txt.textContent = (last.ok ? 'OK ' : 'FAIL ') + ago(last.ts);
      tick.title = 'Last tick ' + stamp(last.ts) + (last.ok ? '' : ' · exit ' + last.rc);
    } else {
      dot.className = 'dot'; txt.textContent = 'NONE';
    }
    runBtn.disabled = !!s.tick_active;
    runBtn.textContent = s.tick_active ? '⟳ Running' : '▶ Run tick';
    stopBtn.hidden = !s.tick_active;
    progress.hidden = !s.tick_active;
  }

  function paintProgress(s) {
    var p = s.progress || {};
    var done = p.chunks_done != null ? p.chunks_done : (p.repos_done || 0);
    var total = p.chunks_total || p.repos_total || 0;
    progress.querySelector('.fill').style.width = Math.max(4, total ? Math.round(done / total * 100) : 6) + '%';
    var rem = p.eta_remaining_sec;
    if (rem == null && s.eta_sec && s.eta_sec > (s.elapsed_sec || 0)) rem = s.eta_sec - s.elapsed_sec;
    var parts = ['elapsed ' + fmtDur(s.elapsed_sec)];
    if (rem != null && rem > 0) parts.push('~' + fmtDur(rem) + ' left');
    if (total) parts.push(done + '/' + total + ' chunks');
    if (p.phase) parts.push(p.phase);
    if (p.note) parts.push(p.note);
    progress.querySelector('.txt').textContent = parts.join(' · ');
  }

  var wasBusy = null;
  function poll() {
    return api('/api/status').then(function (s) {
      status = s;
      paintStatus(s);
      if (s.tick_active) paintProgress(s);
      if (wasBusy && !s.tick_active) {
        var r = s.last_tick || {};
        if (r.ok) toast('Tick complete.', 'ok');
        else if (r.rc === 75) toast('Tick incomplete: GitHub rate limit or network retries exhausted.', 'warn');
        else toast('Tick failed (exit ' + (r.rc == null ? '?' : r.rc) + ').', 'error');
      }
      listeners.forEach(function (fn) { try { fn(s, wasBusy); } catch (e) { console.error(e); } });
      wasBusy = s.tick_active;
    }).catch(function () {
      var t = cell('tick'); t.querySelector('.dot').className = 'dot crit'; t.querySelector('strong').textContent = 'API DOWN';
    });
  }

  // ---------- work queue ----------
  var KIND = { tick: ['sig', 'tick'], decide: ['sig', 'decisions'], insights: ['info', 'sweep'],
    evaluation: ['info', 'evaluation'], build: ['info', 'build'], decision: ['', 'decision'], clarify: ['warn', 'needs you'] };
  function paintWork(w) {
    var c = cell('work'), dot = c.querySelector('.dot'), txt = c.querySelector('strong');
    var n = w.running.length, q = w.queued.length;
    c.classList.toggle('locked', !!w.ledger_lock);
    dot.className = 'dot' + (w.ledger_lock ? ' run' : n ? ' ok' : '');
    txt.textContent = w.ledger_lock ? 'LOCKED ' + fmtDur(w.ledger_lock.elapsed_sec) :
      n ? n + ' running' + (q ? ' · ' + q + ' queued' : '') : q ? q + ' queued' : 'idle';
    c.title = w.ledger_lock ? w.ledger_lock.title + ': merges and posts wait until it finishes (w)' : 'Nothing holds the ledgers (w)';
    if (!workPop.hidden) renderWork(w);
  }
  function pollWork() {
    return api('/api/work').then(function (w) {
      work = w; paintWork(w);
      workListeners.forEach(function (fn) { try { fn(w); } catch (e) { console.error(e); } });
    }).catch(function () {});
  }
  var workPop = document.createElement('div');
  workPop.className = 'popover work';
  workPop.hidden = true;
  document.body.appendChild(workPop);
  function renderWork(w) {
    var lock = w.ledger_lock;
    var html = '<section>' + (lock
      ? '<div class="callout sig"><p><b>Merges, posts and dismissals are waiting.</b> ' + esc(lock.title) +
        ' holds the ledgers (' + fmtDur(lock.elapsed_sec) + ' so far). They go through as soon as it finishes; nothing you clicked is lost, click again then.</p></div>'
      : '<div class="callout ok"><p><b>Nothing holds the ledgers.</b> Merges, posts and dismissals go straight through.</p></div>') + '</section>';
    html += '<section><div class="label">Running now · ' + w.running.length + '</div>' + (w.running.length ? w.running.map(function (r) {
      var k = KIND[r.kind] || ['', r.kind];
      return '<div class="job"><div class="row"><span><span class="st ' + k[0] + '">' + esc(k[1]) + '</span> <b>' + esc(r.title) + '</b></span>' +
        '<span class="num muted">' + fmtDur(r.elapsed_sec) + '</span></div>' +
        (r.detail ? '<small>' + esc(r.detail) + '</small>' : '') +
        (r.steps && r.steps.length ? '<ol class="steps">' + r.steps.map(function (x) {
          return '<li><span class="mono muted">' + esc([x.repo, x.ref].filter(Boolean).join(' ')) + '</span> ' + esc(x.msg) + '</li>';
        }).join('') + '</ol>' : '') + '</div>';
    }).join('') : '<p class="muted" style="margin:6px 0 0">Nothing is running.</p>') + '</section>';
    html += '<section><div class="label">Queued · ' + w.queued.length + '</div>' + (w.queued.length ? w.queued.map(function (q) {
      var k = KIND[q.kind] || ['', q.kind];
      return '<div class="job"><div class="row"><span><span class="st ' + k[0] + '">' + esc(k[1]) + '</span> ' + esc(q.title) + '</span>' +
        '<span class="num muted">' + esc(ago(q.since)) + '</span></div>' + (q.detail ? '<small>' + esc(q.detail) + '</small>' : '') + '</div>';
    }).join('') : '<p class="muted" style="margin:6px 0 0">Nothing is waiting.</p>') + '</section>';
    var sched = w.schedule || {};
    html += '<section><div class="label">Next</div><p style="margin:6px 0 0;font-size:12.5px">' + (sched.enabled
      ? 'Ticks run on schedule: <b>' + esc(sched.label) + '</b>.'
      : 'No scheduled ticks (<b>' + esc(sched.label || 'manual') + '</b>). Nothing runs until you press <b>▶ Run tick</b>, type a decision, or start a sweep or build.') + '</p></section>';
    html += '<section><div class="label">Recently · ' + (w.recent || []).length + '</div>' + ((w.recent || []).length ? (w.recent || []).map(function (r) {
      return '<div class="job"><div class="row"><span><span class="st ' + (r.ok ? '' : 'crit') + '">' + esc(r.kind) + (r.ok ? '' : ' failed') + '</span> ' +
        (r.repo ? '<span class="mono muted">' + esc([r.repo, r.ref].filter(Boolean).join(' ')) + '</span> ' : '') + esc(r.summary) + '</span>' +
        '<span class="num muted" title="' + esc(r.ts) + '">' + esc(ago(r.ts)) + '</span></div></div>';
    }).join('') + '<p style="margin:8px 0 0"><a href="/audit.html">Full history in Audit →</a></p>' : '<p class="muted" style="margin:6px 0 0">No finished work recorded yet.</p>') + '</section>';
    html += '<section><small class="muted">Ticks and the decision runner hold the ledgers while they run. Sweeps, evaluations and builds are read-only toward them or work in their own clones, so they never block a merge. Schedule: ' +
      esc((w.schedule && w.schedule.label) || 'manual') + '.</small></section>';
    workPop.innerHTML = html;
  }
  function openWork() {
    if (!workPop.hidden) { workPop.hidden = true; return; }
    pop.hidden = true;
    workPop.hidden = false;
    if (work) renderWork(work);
    pollWork();
  }
  cell('work').addEventListener('click', openWork);

  function pollSites() {
    // One cell for every probed site: a dot each, and a name only when down.
    api('/api/uptime').then(function (u) {
      var wrap = bar.querySelector('.cells-sites'), urls = Object.keys(u.state || {});
      if (!urls.length) { wrap.innerHTML = ''; return; }
      var down = urls.filter(function (url) { return !u.state[url].ok; });
      wrap.innerHTML = '<span class="cell' + (down.length ? '' : ' opt') + '"><span>Sites</span>' + urls.map(function (url) {
        var s = u.state[url], host = url.replace(/^https?:\/\//, '').replace(/\/$/, '');
        return '<a href="' + esc(url) + '" target="_blank" rel="noopener" title="' +
          esc(host + (s.ok ? ' up' : ' DOWN') + ' · checked ' + (s.last_checked || '?') + ' · ' + (s.last_status || s.error || '?') + ' · ' + (s.last_ms || '?') + 'ms') +
          '"><i class="dot ' + (s.ok ? 'ok' : 'crit') + '"></i></a>';
      }).join('') + (down.length ? '<strong style="color:var(--crit)">' + esc(down.map(function (x) {
        return x.replace(/^https?:\/\//, '').replace(/\/$/, ''); }).join(', ')) + ' DOWN</strong>' : '') + '</span>';
    }).catch(function () {});
  }

  // ---------- actions ----------
  runBtn.addEventListener('click', function () {
    api('/api/tick', {}).then(function () { wasBusy = true; toast('Tick started.'); poll(); })
      .catch(function (e) { toast(e.message, 'error'); });
  });
  stopBtn.addEventListener('click', function () {
    modal({
      title: 'Stop the running tick', tone: 'crit', confirm: 'Stop tick', cancel: 'Keep running',
      body: '<p>The session is killed where it stands. Work it already finished (posted comments, written ledger entries) stays. Anything in flight is lost and the board is not refreshed.</p><p>Nothing new is posted to GitHub.</p>'
    }).then(function (go) {
      if (!go) return;
      api('/api/tick', { action: 'cancel' }).then(function () { toast('Tick stopped.', 'warn'); poll(); })
        .catch(function (e) { toast(e.message, 'error'); });
    });
  });
  cell('mode').addEventListener('click', function () {
    var to = status && status.mode === 'live' ? 'draft' : 'live';
    modal(to === 'live' ? {
      title: 'Go live', tone: 'sig', confirm: 'Go live',
      body: '<p>From the next tick the steward <b>posts its reviews, replies and labels straight to GitHub</b>, under your account.</p>' +
        '<ul><li>It still never merges, closes or force-pushes on its own judgment.</li><li>Design calls and tie-breaks still wait for you under Decisions.</li></ul><p>You can switch back to draft at any time.</p>'
    } : {
      title: 'Switch to draft', confirm: 'Switch to draft',
      body: '<p>The steward stops posting to GitHub and stages everything on Operations for your approval. Anything already posted stays.</p>'
    }).then(function (go) {
      if (!go) return;
      api('/api/mode', { mode: to }).then(function (r) {
        toast(r.mode === 'live' ? 'Live: posting from the next tick.' : 'Draft: nothing will be posted.', r.mode === 'live' ? 'ok' : 'warn');
        poll();
      }).catch(function (e) { toast(e.message, 'error'); });
    });
  });

  // ---------- settings ----------
  var pop = document.createElement('div');
  pop.className = 'popover';
  pop.hidden = true;
  pop.innerHTML =
    '<section><div class="label">Engine: ticks, sweeps and builds</div>' +
      '<div class="row"><select class="field" data-s="backend" style="flex:1"></select></div>' +
      '<small class="muted" data-s="model"></small></section>' +
    '<section><div class="label">Schedule: applies now</div><div class="row"><select class="field" data-s="sched" style="flex:1">' +
      [['manual', 'Manual only'], ['hourly', 'Hourly'], ['6h', 'Every 6 hours'], ['daily', 'Daily 07:00'], ['weekly', 'Weekly, Monday']]
        .map(function (o) { return '<option value="' + o[0] + '">' + o[1] + '</option>'; }).join('') +
      '</select></div></section>' +
    '<section><div class="label">Sign-off: applies now</div><label class="row"><span>Append the steward signature to posted comments</span><input type="checkbox" data-s="sig"></label></section>' +
    '<section><div class="label">Tick size: applies next tick</div>' +
      '<label class="row"><span>Substantive<small>deep reviews, repro attempts, fix PRs</small></span><input class="field num" type="number" min="1" max="100" data-s="sub" style="width:70px"></label>' +
      '<label class="row"><span>Light<small>triage, labels, delta re-reviews</small></span><input class="field num" type="number" min="1" max="200" data-s="light" style="width:70px"></label>' +
      '<label class="row"><span>Legacy idea queue<small>items from the retired canvas advanced per tick; 0 pauses it</small></span><input class="field num" type="number" min="0" max="20" data-s="proactive" style="width:70px"></label></section>' +
    '<section><div class="label">Watched resources: applies next tick</div><div data-s="watch" class="muted" style="margin-top:6px">loading…</div></section>' +
    '<section><div class="label">Theme: this browser</div><div class="row"><select class="field" data-s="theme" style="flex:1"><option value="">Follow system</option><option value="light">Light</option><option value="dark">Dark</option></select></div></section>' +
    '<div class="save"><button class="btn primary block" data-s="save">Save: applies next tick</button></div>';
  document.body.appendChild(pop);
  var S = function (k) { return pop.querySelector('[data-s=' + k + ']'); };
  var watchData = null;

  function paintSettings(s) {
    var b = s.backend || {};
    S('backend').innerHTML = (b.options || []).map(function (o) {
      return '<option value="' + esc(o.value) + '"' + (o.available ? '' : ' disabled') + '>' + esc(o.label) + (o.available ? '' : ' (not installed)') + '</option>';
    }).join('');
    S('backend').value = b.value;
    S('backend').dataset.value = b.value;
    S('model').textContent = b.model ? 'Model pinned: ' + b.model : 'Model: the engine\'s default';
    S('sched').value = (s.schedule && s.schedule.preset) || 'manual';
    S('sig').checked = s.signature_enabled !== false;
    var l = s.limits || {};
    S('sub').value = l.substantive; S('light').value = l.light; S('proactive').value = l.proactive == null ? 1 : l.proactive;
    try { S('theme').value = localStorage.getItem(THEME_KEY) || ''; } catch (e) {}
  }
  function renderWatch(data) {
    watchData = data;
    S('watch').innerHTML = '<table class="grid"><thead><tr><th>Repo</th><th>Priority</th>' +
      data.resources.map(function (r) { return '<th>' + esc(r) + '</th>'; }).join('') + '</tr></thead><tbody>' +
      data.repos.map(function (r) {
        return '<tr data-name="' + esc(r.name) + '"><td class="mono" title="' + esc(r.name) + '">' + esc(r.short) + '</td>' +
          '<td><select class="field" data-k="priority">' + ['high', 'medium', 'low'].map(function (p) {
            return '<option' + (r.priority === p ? ' selected' : '') + '>' + p + '</option>'; }).join('') + '</select></td>' +
          data.resources.map(function (res) {
            return '<td><input type="checkbox" data-k="' + res + '"' + (r.watch.indexOf(res) !== -1 ? ' checked' : '') + '></td>';
          }).join('') + '</tr>';
      }).join('') + '</tbody></table>';
  }
  function openSettings() {
    if (!pop.hidden) { pop.hidden = true; return; }
    pop.hidden = false;
    if (status) paintSettings(status);
    api('/api/status').then(function (s) { status = s; paintSettings(s); }).catch(function () {});
    api('/api/watch').then(renderWatch).catch(function () { S('watch').textContent = 'watch config unavailable'; });
  }
  bar.querySelector('[data-act=settings]').addEventListener('click', function () { workPop.hidden = true; openSettings(); });
  cell('engine').addEventListener('click', openSettings);
  cell('sched').addEventListener('click', openSettings);
  document.addEventListener('click', function (e) {
    if (!pop.hidden && !pop.contains(e.target) && !bar.contains(e.target)) pop.hidden = true;
    if (!workPop.hidden && !workPop.contains(e.target) && !bar.contains(e.target)) workPop.hidden = true;
  });

  S('backend').addEventListener('change', function () {
    var el = S('backend'), previous = el.dataset.value;
    el.disabled = true;
    api('/api/backend', { backend: el.value }).then(function (r) {
      el.dataset.value = r.backend.value;
      toast('Engine: ' + r.backend.label + ', applies next run.' + (r.model_reset ? ' The model pin was cleared.' : ''), 'ok');
      poll();
    }).catch(function (e) { el.value = previous; toast(e.message, 'error'); })
      .then(function () { el.disabled = false; });
  });
  S('sched').addEventListener('change', function () {
    api('/api/schedule', { preset: S('sched').value }).then(function (r) {
      toast(r.schedule.preset === 'manual' ? 'Schedule off: ticks are manual.' : 'Scheduled: ' + r.schedule.label + '.', 'ok');
      poll();
    }).catch(function (e) { toast(e.message, 'error'); });
  });
  S('sig').addEventListener('change', function () {
    var el = S('sig');
    api('/api/signature', { enabled: el.checked }).then(function (r) {
      toast(r.signature_enabled ? 'Sign-off on.' : 'Sign-off off.', 'ok');
    }).catch(function (e) { el.checked = !el.checked; toast(e.message, 'error'); });
  });
  S('theme').addEventListener('change', function () {
    var v = S('theme').value;
    try { v ? localStorage.setItem(THEME_KEY, v) : localStorage.removeItem(THEME_KEY); } catch (e) {}
    if (v) document.documentElement.dataset.theme = v; else delete document.documentElement.dataset.theme;
  });
  S('save').addEventListener('click', function () {
    // Limits first, then repositories: a watch rewrite must not race the
    // limits rewrite of the same config.yaml.
    var save = api('/api/limits', { substantive: +S('sub').value, light: +S('light').value, proactive: +S('proactive').value });
    if (watchData) {
      var repos = Array.prototype.map.call(S('watch').querySelectorAll('tbody tr'), function (tr) {
        return {
          name: tr.dataset.name,
          priority: tr.querySelector('[data-k=priority]').value,
          watch: watchData.resources.filter(function (res) { return tr.querySelector('input[data-k="' + res + '"]').checked; })
        };
      });
      var empty = repos.filter(function (r) { return !r.watch.length; });
      if (empty.length) { toast(empty[0].name + ' has nothing watched. Keep at least one resource.', 'error'); return; }
      save = save.then(function () { return api('/api/watch', { repos: repos }); });
    }
    save.then(function () { pop.hidden = true; toast('Settings saved: applies next tick.', 'ok'); })
      .catch(function (e) { toast('Settings not saved: ' + e.message, 'error'); });
  });

  // ---------- keyboard ----------
  var pendingG = 0;
  function help() {
    modal({
      title: 'Keyboard', confirm: 'Close', cancel: 'Close',
      body: '<div class="keys">' + PAGES.map(function (p) { return '<div><span>' + p.label + '</span><span><kbd>g</kbd> <kbd>' + p.key + '</kbd></span></div>'; }).join('') +
        '<div><span>Next / previous row</span><span><kbd>j</kbd> <kbd>k</kbd></span></div>' +
        '<div><span>Open or act on row</span><span><kbd>enter</kbd></span></div>' +
        '<div><span>Filter</span><span><kbd>/</kbd></span></div>' +
        '<div><span>Settings</span><span><kbd>,</kbd></span></div>' +
        '<div><span>Work queue</span><span><kbd>w</kbd></span></div>' +
        '<div><span>Close</span><span><kbd>esc</kbd></span></div>' +
        '<div><span>This help</span><span><kbd>?</kbd></span></div></div>'
    });
  }
  document.addEventListener('keydown', function (e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select' || e.target.isContentEditable) {
      if (e.key === 'Escape') e.target.blur();
      return;
    }
    if (document.querySelector('.overlay')) return;
    if (e.key === 'Escape') { pop.hidden = true; workPop.hidden = true; return; }
    if (pendingG && Date.now() - pendingG < 1200) {
      pendingG = 0;
      var p = PAGES.filter(function (x) { return x.key === e.key; })[0];
      if (p) { e.preventDefault(); location.href = p.href; }
      return;
    }
    if (e.key === 'g') { pendingG = Date.now(); return; }
    if (e.key === '?') { e.preventDefault(); help(); return; }
    if (e.key === ',') { e.preventDefault(); workPop.hidden = true; openSettings(); return; }
    if (e.key === 'w') { e.preventDefault(); openWork(); return; }
    if (e.key === '/') {
      var f = document.querySelector('[data-filter-input]');
      if (f) { e.preventDefault(); f.focus(); }
      return;
    }
    if (e.key === 'j') { e.preventDefault(); moveNav(1); return; }
    if (e.key === 'k') { e.preventDefault(); moveNav(-1); return; }
    if (e.key === 'Enter' && navTarget && navTarget.open) {
      var sel = document.querySelector('.sel[data-nav]');
      if (sel) { e.preventDefault(); navTarget.open(sel); }
    }
  });

  window.Steward = {
    esc: esc, api: api, toast: toast, modal: modal, nav: nav, fmtDur: fmtDur, ago: ago, stamp: stamp,
    status: function () { return status; },
    onStatus: function (fn) { listeners.push(fn); if (status) fn(status, wasBusy); },
    onWork: function (fn) { workListeners.push(fn); if (work) fn(work); },
    openWork: openWork,
    refresh: poll
  };

  // /dashboard.html#work (or any page) opens straight onto the work queue.
  poll(); pollWork().then(function () { if (location.hash === '#work') openWork(); }); pollSites();
  setInterval(poll, 5000);
  setInterval(pollWork, 5000);
  setInterval(pollSites, 60000);
})();
