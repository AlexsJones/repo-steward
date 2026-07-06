(function () {
  // Collapsible sections: every h2 toggles its section's content; state
  // persists in localStorage so it survives the 5-minute auto-reload.
  // Pure client behavior — works even on static mirrors, no API needed.
  document.querySelectorAll('main > section').forEach(function (sec) {
    var h2 = sec.querySelector('h2');
    if (!h2) return;
    var key = 'steward-collapse:' + h2.textContent.replace(/[\d·]+/g, '').trim().toLowerCase();
    var chev = document.createElement('span');
    chev.textContent = '▾';
    chev.style.cssText = 'display:inline-block;margin-right:7px;transition:transform .15s;';
    h2.insertBefore(chev, h2.firstChild);
    h2.style.cursor = 'pointer';
    h2.style.userSelect = 'none';
    var collapsed = localStorage.getItem(key) === '1';
    function apply() {
      Array.prototype.forEach.call(sec.children, function (c) {
        if (c !== h2) c.style.display = collapsed ? 'none' : '';
      });
      chev.style.transform = collapsed ? 'rotate(-90deg)' : '';
    }
    apply();
    h2.addEventListener('click', function () {
      collapsed = !collapsed;
      localStorage.setItem(key, collapsed ? '1' : '0');
      apply();
    });
  });

  // Controls only render when the steward API answers (i.e. served by server.py,
  // not a static mirror such as a claude.ai artifact).
  fetch('/api/status').then(function (r) { if (r.ok) return r.json(); throw 0; })
    .then(function (s) { init(s); }).catch(function () {});

  function init(initial) {
  var header = document.querySelector('header');
  var statusline = document.querySelector('.statusline');

  // Layout: controls (mode toggle + run button) sit on the title row, top
  // right; the chip row gets the full width below and never mixes with them.
  statusline.style.flexBasis = '100%';
  statusline.style.marginLeft = '0';

  // Mode toggle: replaces any statically-rendered draft chip. Clicking flips
  // draft <-> live via /api/mode (config.yaml is the source of truth).
  var stale = document.querySelector('.chip.draft');
  if (stale) stale.remove();
  var modeChip = document.createElement('button');
  header.insertBefore(modeChip, statusline);
  function paintMode(mode) {
    var draft = mode !== 'live';
    modeChip.textContent = draft ? 'DRAFT — click to go live' : 'LIVE — click for draft';
    modeChip.style.cssText = 'font:600 12px ui-monospace,Menlo,monospace;padding:9px 14px;' +
      'border-radius:8px;border:none;cursor:pointer;margin-left:auto;align-self:center;flex-shrink:0;' + (draft
      ? 'background:var(--warn-soft);color:var(--warn);'
      : 'background:var(--ok-soft);color:var(--ok);');
    modeChip.dataset.mode = draft ? 'draft' : 'live';
  }
  paintMode(initial.mode);
  modeChip.addEventListener('click', function () {
    var to = modeChip.dataset.mode === 'draft' ? 'live' : 'draft';
    var msg = to === 'live'
      ? 'Go LIVE? From the next tick the steward posts reviews, replies, and labels to GitHub autonomously (signed, never merging/closing).'
      : 'Back to DRAFT? The steward will stage everything here instead of posting.';
    if (!confirm(msg)) return;
    fetch('/api/mode', { method: 'POST', body: JSON.stringify({ mode: to }) })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.error) { alert(res.error); return; }
        paintMode(res.mode);
      })
      .catch(function () { alert('mode change failed — is the API up?'); });
  });

  // Live site-uptime chips (fed by uptime_check.py via /api/uptime).
  function sitesPoll() {
    fetch('/api/uptime').then(function (r) { return r.json(); }).then(function (u) {
      Object.keys(u.state || {}).forEach(function (url) {
        var s = u.state[url];
        var host = url.replace(/^https?:\/\//, '');
        var id = 'site-' + host.replace(/[^a-z0-9]/gi, '');
        var el = document.getElementById(id);
        if (!el) {
          el = document.createElement('a');
          el.id = id; el.className = 'chip'; el.href = url; el.target = '_blank';
          el.style.textDecoration = 'none';
          statusline.insertBefore(el, statusline.firstChild);
        }
        el.innerHTML = '<span style="color:' + (s.ok ? 'var(--ok)' : 'var(--crit)') + '">●</span> ' +
          host + (s.ok ? '' : ' DOWN');
        el.title = 'last check ' + (s.last_checked || '?') + ' · ' +
          (s.last_status || s.error || '?') + ' · ' + (s.last_ms || '?') + 'ms';
      });
    }).catch(function () {});
  }
  sitesPoll(); setInterval(sitesPoll, 60000);

  // Toasts: bottom-right stack, auto-dismiss. Used for tick lifecycle events.
  var toastWrap = document.createElement('div');
  toastWrap.style.cssText = 'position:fixed;right:18px;bottom:18px;display:flex;flex-direction:column;gap:8px;z-index:50;';
  document.body.appendChild(toastWrap);
  function toast(msg, kind) {
    var t = document.createElement('div');
    var tone = kind === 'ok' ? 'var(--ok)' : kind === 'warn' ? 'var(--warn)' : 'var(--accent)';
    t.style.cssText = 'background:var(--panel);color:var(--ink);border-left:3px solid ' + tone +
      ';box-shadow:0 2px 10px rgba(0,0,0,.28);border-radius:6px;padding:10px 14px;font-size:13px;max-width:340px;opacity:0;transition:opacity .2s;';
    t.textContent = msg;
    toastWrap.appendChild(t);
    requestAnimationFrame(function () { t.style.opacity = 1; });
    setTimeout(function () { t.style.opacity = 0; setTimeout(function () { t.remove(); }, 250); }, kind === 'ok' ? 8000 : 5000);
  }

  // Schedule dropdown: live-configures the systemd timer via /api/schedule.
  var sched = document.createElement('select');
  sched.title = 'Tick schedule';
  sched.style.cssText = 'font:600 12px ui-monospace,Menlo,monospace;padding:9px 10px;border-radius:8px;' +
    'border:1px solid var(--line);background:var(--panel);color:var(--muted);cursor:pointer;margin-left:10px;align-self:center;flex-shrink:0;';
  [['manual', 'Manual only'], ['hourly', 'Hourly'], ['6h', 'Every 6h'],
   ['daily', 'Daily 07:00'], ['weekly', 'Weekly Mon']].forEach(function (o) {
    var opt = document.createElement('option');
    opt.value = o[0]; opt.textContent = '⏱ ' + o[1]; sched.appendChild(opt);
  });
  function paintSched(s) { sched.value = (s && s.preset) || 'manual'; }
  paintSched(initial.schedule);
  sched.addEventListener('change', function () {
    fetch('/api/schedule', { method: 'POST', body: JSON.stringify({ preset: sched.value }) })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.error) { alert(res.error); return; }
        toast(res.schedule.preset === 'manual'
          ? 'Schedule off — ticks are manual now.'
          : 'Scheduled: ' + res.schedule.label + '.', 'ok');
      })
      .catch(function () { alert('schedule change failed — is the API up?'); });
  });
  header.insertBefore(sched, statusline);

  // The primary action: solid button, doubling as tick status indicator.
  var btn = document.createElement('button');
  var btnBase = 'font:600 14px system-ui,-apple-system,sans-serif;padding:10px 20px;' +
    'border-radius:8px;border:none;letter-spacing:.01em;margin-left:10px;align-self:center;flex-shrink:0;';
  function styleBtn(busy) {
    btn.style.cssText = btnBase + (busy
      ? 'background:var(--warn-soft);color:var(--warn);cursor:default;'
      : 'background:var(--accent);color:var(--panel);cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.25);');
  }
  header.insertBefore(btn, statusline);

  // Progress strip below the header: appears only while a tick runs.
  var strip = document.createElement('div');
  strip.style.cssText = 'display:none;margin:-16px 0 24px;';
  strip.innerHTML = '<div style="font:12px ui-monospace,Menlo,monospace;color:var(--muted);margin-bottom:5px" id="prog-label"></div>' +
    '<div style="height:6px;background:var(--panel-2);border-radius:999px;overflow:hidden">' +
    '<div id="prog-bar" style="height:100%;width:0;background:var(--accent);transition:width .4s"></div></div>';
  var main = document.querySelector('main');
  main.insertBefore(strip, main.querySelector('section'));
  var progLabel = strip.querySelector('#prog-label');
  var progBar = strip.querySelector('#prog-bar');

  function fmtDur(s) {
    if (s == null) return '?';
    var m = Math.floor(s / 60), r = s % 60;
    return m ? m + 'm ' + r + 's' : r + 's';
  }

  var wasBusy = initial.tick_active;
  function setBusy(busy) {
    btn.disabled = busy;
    btn.textContent = busy ? '⟳ Tick running…' : '▶ Run tick now';
    styleBtn(busy);
    strip.style.display = busy ? 'block' : 'none';
    sched.disabled = busy;
    document.querySelectorAll('.approve-btn').forEach(function (b) {
      if (!b.dataset.done) { b.disabled = busy; b.style.opacity = busy ? 0.5 : 1; }
    });
  }

  function renderProgress(status, steps) {
    var repoSteps = steps.filter(function (s) { return s.phase === 'repo'; });
    var last = steps[steps.length - 1] || {};
    var lastRepo = repoSteps[repoSteps.length - 1];
    var pct = lastRepo && lastRepo.total ? Math.round(lastRepo.idx / lastRepo.total * 100) : 8;
    progBar.style.width = pct + '%';
    var elapsed = 'elapsed ' + fmtDur(status.elapsed_sec);
    var eta = status.eta_sec ? ' · ~' + fmtDur(Math.max(0, status.eta_sec - (status.elapsed_sec || 0))) + ' left (est)' : '';
    var where = lastRepo ? ' · ' + lastRepo.repo + ' (repo ' + lastRepo.idx + '/' + lastRepo.total + ')'
      : (last.msg ? ' · ' + last.msg : '');
    progLabel.textContent = 'Tick running · ' + elapsed + eta + where;
  }

  var seenItemKeys = {};
  function poll() {
    fetch('/api/status').then(function (r) { return r.json(); }).then(function (s) {
      setBusy(s.tick_active);
      paintSched(s.schedule);
      if (s.tick_active) {
        fetch('/api/progress').then(function (r) { return r.json(); }).then(function (p) {
          var steps = p.steps || [];
          renderProgress(s, steps);
          // Toast the most recent per-repo transition (once each).
          steps.filter(function (x) { return x.phase === 'repo'; }).forEach(function (x) {
            var k = x.repo + ':' + x.idx;
            if (!seenItemKeys[k]) { seenItemKeys[k] = 1; if (wasBusy) toast('Processing ' + x.repo + ' (' + x.idx + '/' + x.total + ')'); }
          });
        }).catch(function () {});
      }
      if (wasBusy && !s.tick_active) {
        toast('Tick complete — refreshing the board…', 'ok');
        seenItemKeys = {};
        setTimeout(function () { location.reload(); }, 1500);
      }
      wasBusy = s.tick_active;
    }).catch(function () { btn.textContent = 'api unreachable'; });
  }
  setBusy(initial.tick_active);
  if (initial.tick_active) poll();
  setInterval(poll, 5000);

  btn.addEventListener('click', function () {
    fetch('/api/tick', { method: 'POST', body: '{}' }).then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.error) { alert(res.error); return; }
        wasBusy = true; setBusy(true); toast('Tick started.');
      });
  });

  document.querySelectorAll('details.staged[data-repo][data-items]').forEach(function (d) {
    var items = d.dataset.items.split(',');
    var a = document.createElement('button');
    a.className = 'approve-btn';
    a.textContent = 'Approve & post' + (items.length > 1 ? ' (' + items.length + ')' : '');
    a.style.cssText = 'margin-left:auto;font:600 11.5px ui-monospace,Menlo,monospace;padding:2px 10px;border-radius:999px;border:1px solid var(--ok);background:var(--ok-soft);color:var(--ok);cursor:pointer;';
    a.addEventListener('click', function (ev) {
      ev.preventDefault(); ev.stopPropagation();
      if (!confirm('Post to GitHub as you: ' + d.dataset.repo + ' ' + d.dataset.items + '?')) return;
      a.disabled = true; a.textContent = 'posting…';
      fetch('/api/approve', { method: 'POST', body: JSON.stringify({ repo: d.dataset.repo, items: items }) })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          var ok = res.outcomes && Object.values(res.outcomes).every(function (o) { return o.ok; });
          a.textContent = ok ? '✓ posted' : 'failed — see approvals.jsonl';
          a.dataset.done = '1';
          if (!ok) { a.style.borderColor = 'var(--crit)'; a.style.color = 'var(--crit)'; a.style.background = 'var(--crit-soft)'; a.disabled = false; delete a.dataset.done; }
          if (res.error) { alert(res.error); a.textContent = 'Approve & post'; a.disabled = false; }
        })
        .catch(function () { a.textContent = 'failed'; a.disabled = false; });
    });
    d.querySelector('summary').appendChild(a);
  });
  }
})();
