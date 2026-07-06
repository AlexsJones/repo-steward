(function () {
  // Repository filter lens: the Repositories row becomes the page's filter.
  // Clicking a repo focuses every section (Decisions, Ready, In flight, Staged)
  // on that repo. Works by deriving each item's repo from data-repo, its PR/issue
  // link, or the in-flight group header — so it runs on the current dashboard
  // without waiting for a regenerated one.
  initRepoFilter();
  function initRepoFilter() {
    var main = document.querySelector('main');
    if (!main) return;
    function heading(re) {
      return Array.prototype.slice.call(main.querySelectorAll('section'))
        .filter(function (s) { var h = s.querySelector('h2'); return h && re.test(h.textContent); })[0];
    }
    var secRepos = heading(/^\s*(fleet|repositories)/i);
    if (!secRepos) return;
    var secDec = heading(/decisions/i), secReady = heading(/ready for/i),
        secFlight = heading(/in flight/i), secStaged = heading(/staged/i);
    function shortRepo(href) { var m = href && href.match(/github\.com\/[^/]+\/([^/]+)/); return m ? m[1] : null; }

    var css = document.createElement('style');
    css.textContent =
      'section.flt-empty > *:not(h2):not(.empty-note){display:none!important}' +
      '.empty-note{display:none;color:var(--muted);font-size:13px;padding:10px 2px}' +
      'section.flt-empty .empty-note{display:block}' +
      '.card.repo{cursor:pointer;transition:opacity .12s,border-color .12s,background .12s}' +
      '.card.repo:hover{border-color:var(--accent)}' +
      '.card.repo.sel{border-color:var(--accent);background:var(--accent-soft);box-shadow:inset 0 0 0 1px var(--accent)}' +
      '.card.repo.dim{opacity:.4}' +
      '.rbadges{display:flex;gap:5px;flex-wrap:wrap;margin-top:3px}' +
      '.rb{font-size:10.5px;font-weight:600;padding:1px 7px;border-radius:999px}' +
      '.rb.dec{background:var(--crit-soft);color:var(--crit)}' +
      '.rb.stg{background:var(--accent-soft);color:var(--accent)}' +
      '.rb.fl{background:var(--neutral-soft);color:var(--muted)}' +
      '.lens-hint{font-size:12px;color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0;margin-left:10px}' +
      '.fbar{display:none;align-items:center;gap:12px;background:var(--accent-soft);border:1px solid var(--accent);border-radius:9px;padding:9px 14px;margin:0 0 24px;font-size:13.5px}' +
      '.fbar.on{display:flex}.fbar b{color:var(--accent)}' +
      '.fbar .clear{margin-left:auto;font:600 12px ui-monospace,Menlo,monospace;color:var(--accent);background:transparent;border:1px solid var(--accent);border-radius:7px;padding:5px 11px;cursor:pointer}' +
      '.dec-alert{display:none;align-items:center;gap:10px;background:var(--crit-soft);color:var(--crit);border:1px solid var(--crit);border-radius:9px;padding:9px 15px;margin:0 0 22px;font:600 13.5px system-ui,-apple-system,sans-serif}' +
      '.dec-alert.on{display:flex}' +
      '.dec-alert button{margin-left:auto;font:600 12px ui-monospace,Menlo,monospace;background:transparent;border:1px solid var(--crit);color:var(--crit);border-radius:7px;padding:4px 11px;cursor:pointer}';
    document.head.appendChild(css);

    // Tag every filterable item with its repo (derive where not explicit).
    function tag(el, repo) { if (repo && !el.dataset.repo) el.dataset.repo = repo; el.setAttribute('data-flt', ''); }
    if (secDec) secDec.querySelectorAll('.decision').forEach(function (d) { var a = d.querySelector('a[href]'); tag(d, a && shortRepo(a.getAttribute('href'))); });
    if (secReady) secReady.querySelectorAll('tbody tr').forEach(function (r) { if (!r.querySelector('td')) return; var a = r.querySelector('a[href]'); tag(r, a && shortRepo(a.getAttribute('href'))); });
    if (secFlight) { var cur = null; secFlight.querySelectorAll('tbody tr').forEach(function (r) {
        if (r.classList.contains('grouprow')) { cur = r.textContent.trim(); r.dataset.repo = cur; r.setAttribute('data-grp', ''); return; }
        var a = r.querySelector('a[href]'); tag(r, (a && shortRepo(a.getAttribute('href'))) || cur);
      }); }
    if (secStaged) secStaged.querySelectorAll('details.staged').forEach(function (d) { d.setAttribute('data-flt', ''); });

    var flt = [secDec, secReady, secFlight, secStaged].filter(Boolean);
    flt.forEach(function (s) {
      s.setAttribute('data-filterable', '');
      var note = document.createElement('div');
      note.className = 'empty-note';
      note.textContent = 'Nothing here for this repository.';
      s.appendChild(note);
    });

    function itemsIn(sec, repo) {
      if (!sec) return 0;
      return Array.prototype.slice.call(sec.querySelectorAll('[data-flt]'))
        .filter(function (e) { return !repo || e.dataset.repo === repo; }).length;
    }

    // Rename heading + add hint (runs before the collapsible pass adds a chevron).
    var h = secRepos.querySelector('h2');
    if (h) h.innerHTML = 'Repositories <span class="lens-hint">Click a repository to focus the page on its work</span>';

    // Attention badges + click on each repo card; build an "All" card.
    var grid = secRepos.querySelector('.fleet') || secRepos;
    var cards = Array.prototype.slice.call(secRepos.querySelectorAll('.card.repo'));
    cards.forEach(function (card) {
      var nameEl = card.querySelector('.name');
      var repo = nameEl ? nameEl.textContent.trim() : null;
      card.dataset.repoName = repo || '';
      var dec = itemsIn(secDec, repo), stg = itemsIn(secStaged, repo), fl = itemsIn(secFlight, repo);
      if (dec + stg + fl > 0) {
        var bad = document.createElement('div');
        bad.className = 'rbadges';
        if (dec) bad.innerHTML += '<span class="rb dec">' + dec + ' dec</span>';
        if (stg) bad.innerHTML += '<span class="rb stg">' + stg + ' staged</span>';
        if (fl) bad.innerHTML += '<span class="rb fl">' + fl + ' in flight</span>';
        card.appendChild(bad);
      }
    });
    var allCard = document.createElement('div');
    allCard.className = 'card repo sel';
    allCard.dataset.repoName = '';
    allCard.innerHTML = '<span class="name">All repositories</span><span class="quiet">the whole fleet</span>';
    grid.insertBefore(allCard, grid.firstChild);
    var allCards = [allCard].concat(cards);

    // Slim decisions alert (urgency-first) + filter context bar.
    // Insert the lens block AFTER the header (header is main's first child).
    var hdr = main.querySelector('header');
    var anchor = hdr || main.firstChild;
    var decAlert = document.createElement('div');
    decAlert.className = 'dec-alert';
    decAlert.innerHTML = '<span data-txt></span><button>View decisions ↓</button>';
    anchor.insertAdjacentElement('afterend', decAlert);
    decAlert.insertAdjacentElement('afterend', secRepos);
    var fbar = document.createElement('div');
    fbar.className = 'fbar';
    fbar.innerHTML = '<span>Focused on <b data-name></b> — <span data-sum></span></span><button class="clear">✕ All repositories</button>';
    secRepos.insertAdjacentElement('afterend', fbar);
    decAlert.querySelector('button').addEventListener('click', function () { if (secDec) secDec.scrollIntoView({ block: 'start', behavior: 'smooth' }); });

    function apply(repo) {
      allCards.forEach(function (c) {
        var isSel = (c.dataset.repoName || '') === (repo || '');
        c.classList.toggle('sel', isSel);
        c.classList.toggle('dim', !!repo && !isSel && c !== allCard);
      });
      document.querySelectorAll('[data-flt]').forEach(function (el) {
        el.style.display = (!repo || el.dataset.repo === repo) ? '' : 'none';
      });
      document.querySelectorAll('[data-grp]').forEach(function (g) { g.style.display = repo ? 'none' : ''; });
      flt.forEach(function (sec) {
        var shown = itemsIn(sec, repo);
        sec.classList.toggle('flt-empty', shown === 0);
        var cnt = sec.querySelector('h2 .count'); if (cnt) cnt.textContent = '· ' + shown;
      });
      var decN = itemsIn(secDec, repo);
      decAlert.classList.toggle('on', decN > 0);
      decAlert.querySelector('[data-txt]').textContent = '⚠ ' + decN + (decN === 1 ? ' decision needs' : ' decisions need') + ' you';
      if (repo) {
        fbar.classList.add('on');
        fbar.querySelector('[data-name]').textContent = repo;
        fbar.querySelector('[data-sum]').textContent = itemsIn(secFlight, repo) + ' in flight · ' + itemsIn(secStaged, repo) + ' staged · ' + decN + ' decisions';
      } else { fbar.classList.remove('on'); }
    }

    allCards.forEach(function (c) {
      c.addEventListener('click', function () { apply(c.dataset.repoName || ''); });
    });
    fbar.querySelector('.clear').addEventListener('click', function () { apply(''); });
    apply('');
  }

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

  // Themed confirm modal (replaces window.confirm). Returns a Promise<bool>.
  function modal(opts) {
    return new Promise(function (resolve) {
      var overlay = document.createElement('div');
      overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;' +
        'align-items:center;justify-content:center;z-index:100;padding:20px;';
      var dlg = document.createElement('div');
      dlg.style.cssText = 'background:var(--panel);color:var(--ink);border:1px solid var(--line);' +
        'border-radius:12px;box-shadow:0 14px 44px rgba(0,0,0,.42);max-width:470px;width:100%;padding:22px 24px;';
      var tone = opts.tone || 'accent';
      dlg.innerHTML =
        '<h3 style="margin:0 0 12px;font:600 18px system-ui,-apple-system,sans-serif">' + opts.title + '</h3>' +
        '<div style="font-size:14px;line-height:1.6;color:var(--muted)">' + opts.body + '</div>' +
        '<div style="display:flex;gap:10px;justify-content:flex-end;margin-top:22px">' +
        '<button data-act="cancel" style="font:600 13px system-ui,sans-serif;padding:9px 16px;border-radius:8px;' +
        'border:1px solid var(--line);background:var(--panel);color:var(--ink);cursor:pointer">' + (opts.cancel || 'Cancel') + '</button>' +
        '<button data-act="ok" style="font:600 13px system-ui,sans-serif;padding:9px 18px;border-radius:8px;' +
        'border:none;background:var(--' + tone + ');color:var(--panel);cursor:pointer">' + (opts.confirm || 'Confirm') + '</button>' +
        '</div>';
      overlay.appendChild(dlg);
      document.body.appendChild(overlay);
      function close(v) { overlay.remove(); document.removeEventListener('keydown', onKey); resolve(v); }
      function onKey(e) { if (e.key === 'Escape') close(false); else if (e.key === 'Enter') close(true); }
      document.addEventListener('keydown', onKey);
      overlay.addEventListener('click', function (e) { if (e.target === overlay) close(false); });
      dlg.querySelector('[data-act=cancel]').addEventListener('click', function () { close(false); });
      dlg.querySelector('[data-act=ok]').addEventListener('click', function () { close(true); });
      dlg.querySelector('[data-act=ok]').focus();
    });
  }

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
      'border-radius:8px;border:none;cursor:pointer;margin-left:10px;align-self:center;flex-shrink:0;' + (draft
      ? 'background:var(--warn-soft);color:var(--warn);'
      : 'background:var(--ok-soft);color:var(--ok);');
    modeChip.dataset.mode = draft ? 'draft' : 'live';
  }
  paintMode(initial.mode);
  modeChip.addEventListener('click', function () {
    var to = modeChip.dataset.mode === 'draft' ? 'live' : 'draft';
    var opts = to === 'live' ? {
      title: 'Go live?',
      body: '<p style="margin:0 0 12px">Right now the steward <strong>stages</strong> everything for your approval and posts nothing. Going live changes that:</p>' +
        '<ul style="margin:0;padding-left:20px;display:flex;flex-direction:column;gap:7px">' +
        '<li>From the next tick, it <strong>posts its reviews, replies, and labels straight to GitHub</strong> — each signed so contributors know it\'s your steward.</li>' +
        '<li>It still <strong>never merges, closes, or force-pushes</strong> — those stay entirely yours.</li>' +
        '<li>Tie-breaks and design calls still wait for you under <em>Decisions needed</em>.</li>' +
        '</ul>' +
        '<p style="margin:12px 0 0">You can switch back to draft anytime.</p>',
      confirm: 'Go live', tone: 'accent'
    } : {
      title: 'Switch back to draft?',
      body: 'The steward will <strong>stop posting to GitHub</strong> and stage everything on the dashboard for your approval instead. Anything already posted stays as it is.',
      confirm: 'Switch to draft', tone: 'warn'
    };
    modal(opts).then(function (ok) {
      if (!ok) return;
      fetch('/api/mode', { method: 'POST', body: JSON.stringify({ mode: to }) })
        .then(function (r) { return r.json(); })
        .then(function (res) {
          if (res.error) { alert(res.error); return; }
          paintMode(res.mode);
          toast(res.mode === 'live' ? 'Live — the steward will post from the next tick.' : 'Back to draft — nothing will be posted.', res.mode === 'live' ? 'ok' : 'warn');
        })
        .catch(function () { alert('mode change failed — is the API up?'); });
    });
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

  // Tick-size settings: a gear opening a small popover with the per-tick work
  // caps (config.yaml `limits`). Applies to the next tick, editable anytime.
  var gear = document.createElement('button');
  gear.textContent = '⚙';
  gear.title = 'Tick size';
  gear.style.cssText = 'font-size:16px;padding:8px 11px;border-radius:8px;border:1px solid var(--line);' +
    'background:var(--panel);color:var(--muted);cursor:pointer;margin-left:10px;align-self:center;flex-shrink:0;';
  header.insertBefore(gear, statusline);

  var pop = document.createElement('div');
  pop.style.cssText = 'display:none;position:fixed;top:74px;right:20px;z-index:40;background:var(--panel);' +
    'border:1px solid var(--line);border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.3);padding:16px;width:270px;';
  pop.innerHTML =
    '<div style="font:600 11px ui-monospace,Menlo,monospace;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:10px">Tick size — items worked per run</div>' +
    '<label style="display:flex;justify-content:space-between;align-items:center;font-size:13px;margin-bottom:9px">Substantive <input id="lim-sub" type="number" min="1" max="100" style="width:64px;font:600 13px ui-monospace,Menlo,monospace;padding:5px 7px;border-radius:6px;border:1px solid var(--line);background:var(--panel-2);color:var(--ink)"></label>' +
    '<div style="font-size:11px;color:var(--muted);margin:-4px 0 10px">deep PR reviews, repro attempts, fix PRs</div>' +
    '<label style="display:flex;justify-content:space-between;align-items:center;font-size:13px;margin-bottom:9px">Light <input id="lim-light" type="number" min="1" max="200" style="width:64px;font:600 13px ui-monospace,Menlo,monospace;padding:5px 7px;border-radius:6px;border:1px solid var(--line);background:var(--panel-2);color:var(--ink)"></label>' +
    '<div style="font-size:11px;color:var(--muted);margin:-4px 0 12px">triage, labels, delta re-reviews</div>' +
    '<button id="lim-save" style="width:100%;font:600 13px system-ui,sans-serif;padding:8px;border-radius:7px;border:none;background:var(--accent);color:var(--panel);cursor:pointer">Save</button>';
  document.body.appendChild(pop);
  var limSub = pop.querySelector('#lim-sub'), limLight = pop.querySelector('#lim-light');
  function paintLimits(l) { if (l) { limSub.value = l.substantive; limLight.value = l.light; } }
  paintLimits(initial.limits);
  gear.addEventListener('click', function () { pop.style.display = pop.style.display === 'none' ? 'block' : 'none'; });
  pop.querySelector('#lim-save').addEventListener('click', function () {
    fetch('/api/limits', { method: 'POST', body: JSON.stringify({ substantive: +limSub.value, light: +limLight.value }) })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        if (res.error) { alert(res.error); return; }
        pop.style.display = 'none';
        toast('Tick size saved: ' + res.limits.substantive + ' substantive + ' + res.limits.light + ' light — applies next tick.', 'ok');
      })
      .catch(function () { alert('save failed — is the API up?'); });
  });

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

  // Relocate the "metrics →" link from the chip row up into the control
  // cluster, styled as a distinct outlined nav item (not another action).
  var metricsLink = document.querySelector('.statusline a[href$="metrics.html"]');
  if (metricsLink) {
    metricsLink.textContent = '📊 Metrics';
    metricsLink.style.cssText = 'font:600 12px ui-monospace,Menlo,monospace;padding:8px 13px;border-radius:8px;' +
      'border:1px solid var(--accent);background:transparent;color:var(--accent);text-decoration:none;' +
      'margin-left:auto;align-self:center;flex-shrink:0;';
    header.insertBefore(metricsLink, modeChip);
  }

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
    // Schedule and tick-size are config changes that only affect the NEXT
    // tick, so they stay editable while one runs — only the run/approve
    // actions that would race the live session are disabled.
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
      modal({
        title: 'Post to GitHub?',
        body: 'This posts the staged review/reply for <strong>' + d.dataset.repo + ' ' + d.dataset.items +
          '</strong> to GitHub <strong>under your account</strong>, signed by the steward. It never merges or closes.',
        confirm: 'Post', tone: 'ok'
      }).then(function (go) {
        if (!go) return;
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
    });
    d.querySelector('summary').appendChild(a);
  });

  // Inline row actions on the "Ready for your final look" table: approve,
  // dismiss, and expand-to-read the staged review — no click-through to GitHub.
  var readySec = Array.prototype.slice.call(document.querySelectorAll('main > section'))
    .filter(function (s) { var h = s.querySelector('h2'); return h && /ready for/i.test(h.textContent); })[0];
  if (readySec) {
    readySec.querySelectorAll('table tr').forEach(function (row) {
      if (!row.querySelector('td')) return;               // skip header
      var repo = row.dataset.repo, item = row.dataset.item;
      var link = row.querySelector('a[href*="/pull/"], a[href*="/issues/"]');
      if ((!repo || !item) && link) {
        var m = link.getAttribute('href').match(/github\.com\/[^/]+\/([^/]+)\/(pull|issues)\/(\d+)/);
        if (m) { repo = m[1]; item = (m[2] === 'pull' ? 'pr-' : 'issue-') + m[3]; }
      }
      if (!repo || !item) return;

      var cell = row.lastElementChild;
      var bar = document.createElement('div');
      bar.style.cssText = 'display:flex;gap:5px;margin-top:6px;';
      function mk(txt, tone, title) {
        var b = document.createElement('button');
        b.textContent = txt; b.title = title;
        b.style.cssText = 'font:600 12px ui-monospace,Menlo,monospace;padding:3px 9px;border-radius:6px;cursor:pointer;' +
          'border:1px solid var(--' + tone + ');background:var(--' + tone + '-soft);color:var(--' + tone + ');';
        return b;
      }
      var approve = mk('✓ Approve', 'ok', 'Post the staged review to GitHub as you');
      var dismiss = mk('✗ Dismiss', 'crit', 'Drop from the queue without posting');
      var more = mk('⌄', 'accent', 'Read the staged review');
      approve.className = dismiss.className = 'approve-btn';
      bar.appendChild(approve); bar.appendChild(dismiss); bar.appendChild(more);
      cell.appendChild(bar);

      function finish(b, label) { b.textContent = label; b.dataset.done = '1'; [approve, dismiss, more].forEach(function (x) { x.disabled = true; }); row.style.opacity = 0.55; }

      approve.addEventListener('click', function () {
        modal({
          title: 'Approve & post?',
          body: 'Posts the steward\'s staged review for <strong>' + repo + ' ' + item +
            '</strong> to GitHub <strong>under your account</strong>, signed. It approves the PR but never merges it.',
          confirm: 'Approve & post', tone: 'ok'
        }).then(function (go) {
          if (!go) return;
          approve.disabled = true; approve.textContent = 'posting…';
          fetch('/api/approve', { method: 'POST', body: JSON.stringify({ repo: repo, items: [item] }) })
            .then(function (r) { return r.json(); }).then(function (res) {
              var ok = res.outcomes && Object.values(res.outcomes).every(function (o) { return o.ok; });
              if (ok) { finish(approve, '✓ posted'); toast(repo + ' ' + item + ' — review posted.', 'ok'); }
              else { approve.disabled = false; approve.textContent = '✓ Approve'; alert(res.error || 'post failed — see approvals.jsonl'); }
            }).catch(function () { approve.disabled = false; approve.textContent = '✓ Approve'; });
        });
      });

      dismiss.addEventListener('click', function () {
        modal({
          title: 'Dismiss this?',
          body: '<strong>' + repo + ' ' + item + '</strong> drops off the queue and nothing is posted to GitHub. ' +
            'The steward won\'t re-surface it unless the PR changes.',
          confirm: 'Dismiss', tone: 'warn'
        }).then(function (go) {
          if (!go) return;
          fetch('/api/dismiss', { method: 'POST', body: JSON.stringify({ repo: repo, items: [item] }) })
            .then(function (r) { return r.json(); }).then(function (res) {
              if (res.error) { alert(res.error); return; }
              finish(dismiss, '✗ dismissed'); toast(repo + ' ' + item + ' dismissed.', 'warn');
            }).catch(function () { alert('dismiss failed — is the API up?'); });
        });
      });

      var detailRow = null;
      more.addEventListener('click', function () {
        if (detailRow) { detailRow.remove(); detailRow = null; more.textContent = '⌄'; return; }
        fetch('/api/staged?repo=' + encodeURIComponent(repo) + '&item=' + encodeURIComponent(item))
          .then(function (r) { return r.json(); }).then(function (res) {
            var body = res.item && res.item.staged_actions && res.item.staged_actions
              .map(function (a) { return a.body || (a.labels ? 'labels: ' + a.labels.join(', ') : ''); })
              .filter(Boolean).join('\n\n— — —\n\n') || '(no staged text recorded for this item)';
            detailRow = document.createElement('tr');
            var td = document.createElement('td');
            td.colSpan = row.children.length;
            td.innerHTML = '<pre style="margin:0;white-space:pre-wrap;word-break:break-word;font:12.5px/1.55 ui-monospace,Menlo,monospace;background:var(--panel-2);padding:12px 14px;border-radius:6px;color:var(--ink)"></pre>';
            td.querySelector('pre').textContent = body;
            detailRow.appendChild(td);
            row.parentNode.insertBefore(detailRow, row.nextSibling);
            more.textContent = '⌃';
          }).catch(function () { alert('could not load staged text'); });
      });
    });
  }
  }
})();
