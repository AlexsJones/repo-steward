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
        secFlight = heading(/in flight|next tick/i), secStaged = heading(/staged/i);
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

    var currentFilter = '';
    function apply(repo) {
      currentFilter = repo || '';
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

    // Live reconcile: a decision you settled directly on GitHub (merged/closed)
    // shouldn't keep nagging you until the next tick. Check each decision's
    // referenced items and demote resolved ones — non-destructive, the steward
    // clears them properly on its next run.
    css.textContent +=
      '.decision.resolved{opacity:.5}' +
      '.decision .resolved-tag{display:inline-block;margin-left:8px;font-size:11px;font-weight:600;' +
      'color:var(--ok);background:var(--ok-soft);padding:1px 8px;border-radius:999px;vertical-align:middle}';
    if (secDec) secDec.querySelectorAll('.decision').forEach(function (d) {
      var refs = Array.prototype.slice.call(d.querySelectorAll('a[href*="/pull/"], a[href*="/issues/"]'))
        .map(function (a) { var m = a.getAttribute('href').match(/github\.com\/([^/]+\/[^/]+)\/(pull|issues)\/(\d+)/); return m ? { repo: m[1], kind: m[2] === 'pull' ? 'pr' : 'issue', num: m[3] } : null; })
        .filter(Boolean);
      if (!refs.length) return;
      Promise.all(refs.map(function (r) {
        return fetch('/api/ghstate?repo=' + encodeURIComponent(r.repo) + '&num=' + r.num + '&kind=' + r.kind)
          .then(function (x) { return x.json(); }).catch(function () { return { state: 'unknown' }; });
      })).then(function (states) {
        var anyMerged = states.some(function (s) { return s.merged; });
        var allClosed = states.length && states.every(function (s) { return s.state === 'closed'; });
        if (!(anyMerged || allClosed)) return;
        d.classList.add('resolved');
        d.removeAttribute('data-flt');           // exclude from counts + alert
        var h3 = d.querySelector('h3');
        if (h3 && !h3.querySelector('.resolved-tag')) {
          var t = document.createElement('span');
          t.className = 'resolved-tag';
          t.textContent = anyMerged ? '✓ merged on GitHub' : '✓ closed on GitHub';
          h3.appendChild(t);
        }
        apply(currentFilter);                    // recompute decision count + alert
      });
    });

    // Same live reconcile for "Ready for your final look": a PR you merged or
    // closed directly drops out, and a PR whose approval is already posted on
    // GitHub at its current head gets a posture badge with its age — so rows
    // that only await your merge stop masquerading as fresh recommendations.
    css.textContent +=
      'tr.resolved{opacity:.45}tr.resolved td{text-decoration:line-through}' +
      '.ready-tag{display:inline-block;margin-left:8px;font-size:11px;font-weight:600;padding:1px 8px;border-radius:999px;vertical-align:middle;white-space:nowrap}' +
      '.ready-tag.ok{color:var(--ok);background:var(--ok-soft)}' +
      '.ready-tag.old{color:var(--warn);background:var(--warn-soft)}';
    function ageDays(iso) { var t = Date.parse(iso); return isNaN(t) ? null : Math.floor((Date.now() - t) / 864e5); }
    if (secReady) secReady.querySelectorAll('tbody tr[data-flt]').forEach(function (row) {
      var a = row.querySelector('a[href*="/pull/"]');
      var m = a && a.getAttribute('href').match(/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)/);
      if (!m) return;
      fetch('/api/ghstate?repo=' + encodeURIComponent(m[1]) + '&num=' + m[2] + '&kind=pr')
        .then(function (x) { return x.json(); })
        .then(function (s) {
          var cell = row.querySelector('td');
          if (!cell) return;
          if (s.merged || s.state === 'closed') {
            row.classList.add('resolved');
            row.removeAttribute('data-flt');     // exclude from counts + badges
            cell.innerHTML += ' <span class="ready-tag ok">' + (s.merged ? '✓ merged' : '✓ closed') + ' on GitHub</span>';
            apply(currentFilter);
          } else if (s.approved_at_head) {
            var d = ageDays(s.approved_at);
            var old = d != null && d >= 2;
            cell.innerHTML += ' <span class="ready-tag ' + (old ? 'old' : 'ok') + '">✓ approved on GitHub' +
              (d != null ? ' · ' + (d === 0 ? 'today' : d + 'd ago') : '') + ' — ✓ Approve & merge finishes it</span>';
          }
        }).catch(function () {});
    });

    // Decision input: type what you want done and press Enter. The server
    // records it and, when nothing else is running, immediately spawns the
    // decision executor (decide.sh) — a focused engine run that interprets
    // your text and acts on it (posts, labels, closes/merges when you say so).
    // While a tick runs it queues instead, and the next tick executes it first.
    css.textContent +=
      '.decide-box{display:flex;gap:8px;margin-top:10px;align-items:center}' +
      '.decide-box input{flex:1;font:13px system-ui,-apple-system,sans-serif;padding:7px 11px;border-radius:8px;' +
      'border:1px solid var(--neutral-soft);background:transparent;color:inherit;min-width:0}' +
      '.decide-box input:focus{outline:none;border-color:var(--accent)}' +
      '.decide-status{font-size:12px;color:var(--muted);white-space:nowrap}' +
      '.decide-status.ok{color:var(--ok)}.decide-status.err{color:var(--crit)}';
    function watchDecision(id, d, st, inp) {
      var iv = setInterval(function () {
        fetch('/api/decisions').then(function (r) { return r.json(); }).then(function (res) {
          if (res.executing) return;
          clearInterval(iv);
          var mine = (res.decisions || []).filter(function (e) { return e.ts === id; }).pop() || {};
          if (mine.status === 'executed') {
            st.className = 'decide-status ok';
            st.textContent = '✓ done' + (mine.outcome ? ': ' + mine.outcome : '');
            d.classList.add('resolved');
            d.removeAttribute('data-flt');
            apply(currentFilter);
          } else if (mine.status === 'failed') {
            st.className = 'decide-status err';
            st.textContent = '⚠ ' + (mine.note || 'failed — see logs/decide.log');
          } else if (mine.note) {
            // Executor wants clarification — reopen the box for a clearer try.
            st.className = 'decide-status err';
            st.textContent = '⚠ needs clarification: ' + mine.note;
            inp.disabled = false;
          } else {
            st.textContent = '✓ recorded — applies when the steward is free';
          }
        }).catch(function () {});
      }, 4000);
    }
    if (secDec) secDec.querySelectorAll('.decision').forEach(function (d) {
      var ctx = d.textContent.trim().replace(/\s+/g, ' ').slice(0, 1500);
      var box = document.createElement('div');
      box.className = 'decide-box';
      box.innerHTML = '<input type="text" placeholder="Type your decision and press Enter — e.g. ‘go with #650, close #651 as superseded’">' +
        '<span class="decide-status"></span>';
      d.appendChild(box);
      var inp = box.querySelector('input'), st = box.querySelector('.decide-status');
      inp.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter' || !inp.value.trim()) return;
        var refs = Array.prototype.slice.call(d.querySelectorAll('a[href*="/pull/"], a[href*="/issues/"]'))
          .map(function (a) { return a.getAttribute('href'); });
        inp.disabled = true;
        st.className = 'decide-status';
        st.textContent = 'sending…';
        fetch('/api/decide', { method: 'POST', body: JSON.stringify({
          repo: d.dataset.repo || '', refs: refs,
          title: ((d.querySelector('h3') || {}).textContent || '').trim(),
          context: ctx, decision: inp.value.trim()
        }) }).then(function (r) { return r.json(); }).then(function (res) {
          if (res.error) { st.className = 'decide-status err'; st.textContent = '⚠ ' + res.error; inp.disabled = false; return; }
          st.className = 'decide-status ok';
          st.textContent = res.mode === 'executing' ? '🤖 acting on it…' : '⏸ queued — applies when the steward is free';
          watchDecision(res.id, d, st, inp);
        }).catch(function () { st.className = 'decide-status err'; st.textContent = '⚠ failed — try again'; inp.disabled = false; });
      });
    });
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

  // Header panels: ⚙ Settings consolidates every configurable option
  // (schedule, tick size, watched repos); 📋 Audit reviews the decision log.
  // Exclusive fixed popovers under the header — opening one closes the other.
  var openPanel = null;
  function makePanel(width) {
    var p = document.createElement('div');
    p.style.cssText = 'display:none;position:fixed;top:74px;right:20px;z-index:40;background:var(--panel);' +
      'border:1px solid var(--line);border-radius:10px;box-shadow:0 6px 24px rgba(0,0,0,.3);padding:16px;' +
      'width:' + width + ';max-width:calc(100vw - 40px);max-height:76vh;overflow:auto;';
    document.body.appendChild(p);
    return p;
  }
  function togglePanel(p, onOpen) {
    if (openPanel === p) { p.style.display = 'none'; openPanel = null; return; }
    if (openPanel) openPanel.style.display = 'none';
    openPanel = p;
    onOpen();
    p.style.display = 'block';
  }
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && openPanel) { openPanel.style.display = 'none'; openPanel = null; }
  });
  function navBtn(txt, title) {
    var b = document.createElement('button');
    b.textContent = txt; b.title = title;
    b.style.cssText = 'font:600 12px ui-monospace,Menlo,monospace;padding:9px 11px;border-radius:8px;border:1px solid var(--line);' +
      'background:var(--panel);color:var(--muted);cursor:pointer;margin-left:10px;align-self:center;flex-shrink:0;';
    return b;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  var secHdr = 'font:600 11px ui-monospace,Menlo,monospace;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 10px;';

  // ⚙ Settings: schedule (applies immediately), tick size + watched repos
  // (one Save, applies from the next tick).
  var settingsBtn = navBtn('⚙ Settings', 'Schedule, tick size, and what the steward watches');
  header.insertBefore(settingsBtn, statusline);
  var spop = makePanel('460px');

  var sched = document.createElement('select');
  sched.title = 'Tick schedule';
  sched.style.cssText = 'font:600 12px ui-monospace,Menlo,monospace;padding:7px 9px;border-radius:6px;' +
    'border:1px solid var(--line);background:var(--panel-2);color:var(--ink);cursor:pointer;';
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

  var lbl = 'display:flex;justify-content:space-between;align-items:center;font-size:13px;margin-bottom:9px;';
  var numIn = 'width:64px;font:600 13px ui-monospace,Menlo,monospace;padding:5px 7px;border-radius:6px;border:1px solid var(--line);background:var(--panel-2);color:var(--ink);';
  var hint = 'font-size:11px;color:var(--muted);margin:-4px 0 10px;';
  spop.innerHTML =
    '<div style="' + secHdr + '">Schedule — when ticks run</div>' +
    '<div data-sec="sched" style="margin-bottom:18px"></div>' +
    '<div style="' + secHdr + '">Tick size — items worked per run</div>' +
    '<label style="' + lbl + '">Substantive <input id="lim-sub" type="number" min="1" max="100" style="' + numIn + '"></label>' +
    '<div style="' + hint + '">deep PR reviews, repro attempts, fix PRs</div>' +
    '<label style="' + lbl + '">Light <input id="lim-light" type="number" min="1" max="200" style="' + numIn + '"></label>' +
    '<div style="' + hint + 'margin-bottom:18px">triage, labels, delta re-reviews</div>' +
    '<div style="' + secHdr + '">Watched resources — per repository</div>' +
    '<div data-sec="watch" style="margin-bottom:14px;font-size:12px;color:var(--muted)">loading…</div>' +
    '<button id="set-save" style="width:100%;font:600 13px system-ui,sans-serif;padding:8px;border-radius:7px;border:none;background:var(--accent);color:var(--panel);cursor:pointer">Save — applies next tick</button>';
  spop.querySelector('[data-sec=sched]').appendChild(sched);
  var limSub = spop.querySelector('#lim-sub'), limLight = spop.querySelector('#lim-light');
  function paintLimits(l) { if (l) { limSub.value = l.substantive; limLight.value = l.light; } }
  paintLimits(initial.limits);

  var watchData = null;
  function renderWatch(data) {
    watchData = data;
    var th = 'font:600 10.5px ui-monospace,Menlo,monospace;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);padding:4px 6px;text-align:center;';
    var rows = data.repos.map(function (r) {
      return '<tr data-name="' + esc(r.name) + '">' +
        '<td style="padding:5px 8px 5px 0;font:600 12.5px ui-monospace,Menlo,monospace" title="' + esc(r.name) + '">' + esc(r.short) + '</td>' +
        '<td style="padding:5px 8px 5px 0"><select data-k="priority" style="font:600 12px ui-monospace,Menlo,monospace;padding:4px 6px;border-radius:6px;border:1px solid var(--line);background:var(--panel-2);color:var(--ink)">' +
          ['high', 'medium', 'low'].map(function (p) { return '<option' + (r.priority === p ? ' selected' : '') + '>' + p + '</option>'; }).join('') +
        '</select></td>' +
        data.resources.map(function (res) {
          return '<td style="text-align:center;padding:5px 6px"><input type="checkbox" data-k="' + res + '"' +
            (r.watch.indexOf(res) !== -1 ? ' checked' : '') + ' style="accent-color:var(--accent);width:15px;height:15px;cursor:pointer"></td>';
        }).join('') + '</tr>';
    }).join('');
    spop.querySelector('[data-sec=watch]').innerHTML =
      '<table style="border-collapse:collapse;width:100%"><thead><tr>' +
      '<th style="' + th + 'text-align:left">repo</th><th style="' + th + 'text-align:left">priority</th>' +
      data.resources.map(function (res) { return '<th style="' + th + '">' + res + '</th>'; }).join('') +
      '</tr></thead><tbody>' + rows + '</tbody></table>';
  }
  spop.querySelector('#set-save').addEventListener('click', function () {
    var posts = [
      fetch('/api/limits', { method: 'POST', body: JSON.stringify({ substantive: +limSub.value, light: +limLight.value }) })
        .then(function (r) { return r.json(); })
    ];
    if (watchData) {
      var repos = Array.prototype.map.call(spop.querySelectorAll('[data-sec=watch] tbody tr'), function (tr) {
        return {
          name: tr.dataset.name,
          priority: tr.querySelector('[data-k=priority]').value,
          watch: watchData.resources.filter(function (res) { return tr.querySelector('input[data-k="' + res + '"]').checked; })
        };
      });
      var empty = repos.filter(function (r) { return !r.watch.length; });
      if (empty.length) { alert(empty[0].name + ' has nothing watched — keep at least one resource, or remove the repo from config.yaml.'); return; }
      posts.push(fetch('/api/watch', { method: 'POST', body: JSON.stringify({ repos: repos }) })
        .then(function (r) { return r.json(); }));
    }
    Promise.all(posts).then(function (results) {
      var err = results.filter(function (res) { return res.error; })[0];
      if (err) { alert(err.error); return; }
      spop.style.display = 'none'; openPanel = null;
      toast('Settings saved — applies next tick.', 'ok');
    }).catch(function () { alert('save failed — is the API up?'); });
  });
  settingsBtn.addEventListener('click', function () {
    togglePanel(spop, function () {
      fetch('/api/watch').then(function (r) { return r.json(); }).then(renderWatch)
        .catch(function () { spop.querySelector('[data-sec=watch]').textContent = 'watch config unavailable — is the API up?'; });
      fetch('/api/status').then(function (r) { return r.json(); }).then(function (s) {
        paintSched(s.schedule); paintLimits(s.limits);
      }).catch(function () {});
    });
  });

  // 📋 Audit: the decision log has its own page (audit.html, like metrics),
  // with filters and download — the header just links to it.
  var auditLink = document.createElement('a');
  auditLink.textContent = '📋 Audit';
  auditLink.href = 'audit.html';
  auditLink.title = 'The decision log — everything decided and done, by whom; downloadable';
  auditLink.style.cssText = 'font:600 12px ui-monospace,Menlo,monospace;padding:8px 13px;border-radius:8px;' +
    'border:1px solid var(--accent);background:transparent;color:var(--accent);text-decoration:none;' +
    'align-self:center;flex-shrink:0;margin-left:10px;';
  header.insertBefore(auditLink, modeChip);

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
    header.insertBefore(metricsLink, auditLink);
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

  function renderProgress(status) {
    var p = status.progress || {};
    // Deterministic position: chunks (repo ledgers + metrics + dashboard)
    // actually written this tick / total. Nothing here comes from the LLM.
    var done = p.chunks_done != null ? p.chunks_done : (p.repos_done || 0);
    var total = p.chunks_total || p.repos_total || 0;
    var pct = total ? Math.round(done / total * 100) : 6;
    progBar.style.width = Math.max(4, pct) + '%';
    var parts = ['elapsed ' + fmtDur(status.elapsed_sec)];
    // Prefer the chunk-aware estimate (median time past ticks still had to run
    // at this chunk count); fall back to median-duration minus elapsed. Both
    // are server-gated behind having enough history.
    var rem = p.eta_remaining_sec;
    if (rem == null && status.eta_sec && status.eta_sec > (status.elapsed_sec || 0)) {
      rem = status.eta_sec - status.elapsed_sec;
    }
    if (rem != null && rem > 0) parts.push('~' + fmtDur(rem) + ' left (est)');
    if (total) parts.push(done + '/' + total + ' chunks');
    if (p.phase) parts.push(p.phase);
    var label = 'Tick running · ' + parts.join(' · ');
    if (p.note) label += ' · ' + p.note;
    progLabel.textContent = label;
  }

  var seenItemKeys = {};
  function poll() {
    fetch('/api/status').then(function (r) { return r.json(); }).then(function (s) {
      setBusy(s.tick_active);
      paintSched(s.schedule);
      if (s.tick_active) {
        fetch('/api/progress').then(function (r) { return r.json(); }).then(function (p) {
          var steps = p.steps || [];
          renderProgress(s);
          // Toast each per-repo note once (keyed by content — the feed
          // carries no indices or timestamps we'd trust).
          steps.filter(function (x) { return x.phase === 'repo' && x.repo; }).forEach(function (x) {
            var k = x.repo + ':' + (x.msg || '');
            if (!seenItemKeys[k]) { seenItemKeys[k] = 1; if (wasBusy) toast('Processing ' + x.repo); }
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
      var approve = mk('✓ Approve & merge', 'ok', 'Post the staged review (if still unposted) and merge the PR as you');
      var dismiss = mk('✗ Dismiss', 'crit', 'Drop from the queue without posting');
      var more = mk('⌄', 'accent', 'Read the staged review');
      approve.className = dismiss.className = 'approve-btn';
      bar.appendChild(approve); bar.appendChild(dismiss); bar.appendChild(more);
      cell.appendChild(bar);

      function finish(b, label) { b.textContent = label; b.dataset.done = '1'; [approve, dismiss, more].forEach(function (x) { x.disabled = true; }); row.style.opacity = 0.55; }

      approve.addEventListener('click', function () {
        modal({
          title: 'Approve & merge?',
          body: 'Posts the steward\'s staged review for <strong>' + repo + ' ' + item +
            '</strong> to GitHub <strong>under your account</strong> (skipped if already posted), then ' +
            '<strong>merges the PR</strong> — this was your final look.',
          confirm: 'Approve & merge', tone: 'ok'
        }).then(function (go) {
          if (!go) return;
          approve.disabled = true; approve.textContent = 'merging…';
          fetch('/api/approve', { method: 'POST', body: JSON.stringify({ repo: repo, items: [item] }) })
            .then(function (r) { return r.json(); }).then(function (res) {
              var outs = res.outcomes ? Object.values(res.outcomes) : [];
              var ok = outs.length && outs.every(function (o) { return o.ok; });
              var merged = outs.some(function (o) { return o.merged; });
              if (ok) {
                finish(approve, merged ? '✓ merged' : '✓ posted');
                toast(repo + ' ' + item + (merged ? ' — approved & merged.' : ' — review posted.'), 'ok');
              } else {
                approve.disabled = false; approve.textContent = '✓ Approve & merge';
                alert(res.error || (outs[0] && outs[0].detail) || 'failed — see approvals.jsonl');
              }
            }).catch(function () { approve.disabled = false; approve.textContent = '✓ Approve & merge'; });
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
