(function () {
  // Controls only render when the steward API answers (i.e. served by server.py,
  // not a static mirror such as a claude.ai artifact).
  fetch('/api/status').then(function (r) { if (r.ok) return r.json(); throw 0; })
    .then(function (s) { init(s); }).catch(function () {});

  function init(initial) {
  var statusline = document.querySelector('.statusline');

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

  // The primary action on the page: a solid button in the header, doubling
  // as the tick status indicator (no separate "idle" chip).
  var btn = document.createElement('button');
  var btnBase = 'font:600 14px system-ui,-apple-system,sans-serif;padding:10px 20px;' +
    'border-radius:8px;border:none;letter-spacing:.01em;margin-left:16px;align-self:center;flex-shrink:0;';
  function styleBtn(busy) {
    btn.style.cssText = btnBase + (busy
      ? 'background:var(--warn-soft);color:var(--warn);cursor:default;'
      : 'background:var(--accent);color:var(--panel);cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.25);');
  }
  document.querySelector('header').appendChild(btn);

  function setBusy(busy) {
    btn.disabled = busy;
    btn.textContent = busy ? '⟳ Tick running…' : '▶ Run tick now';
    styleBtn(busy);
    document.querySelectorAll('.approve-btn').forEach(function (b) {
      if (!b.dataset.done) { b.disabled = busy; b.style.opacity = busy ? 0.5 : 1; }
    });
  }
  function poll() {
    fetch('/api/status').then(function (r) { return r.json(); })
      .then(function (s) { setBusy(s.tick_active); })
      .catch(function () { btn.textContent = 'api unreachable'; });
  }
  setBusy(initial.tick_active); setInterval(poll, 15000);

  btn.addEventListener('click', function () {
    fetch('/api/tick', { method: 'POST', body: '{}' }).then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.error) alert(res.error); else setBusy(true);
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
