// Operations page behaviour. The system bar, tick control, settings, dialogs
// and toasts come from assets/steward-shell.js (window.Steward); this file
// only wires the rows render_dashboard.py emits:
//   [data-row][data-repo]     filterable rows (rail + text filter)
//   .decision[data-resolve-on] decisions, live-reconciled against GitHub
//   tr[data-item] in #ready    Merge / Dismiss / Review
//   tr[data-staged]            View / Post
(function () {
  var main = document.querySelector('main[data-ops]');
  if (!main || !window.Steward) return;
  var S = window.Steward, esc = S.esc;
  var $ = function (sel, el) { return (el || document).querySelector(sel); };
  var $$ = function (sel, el) { return Array.prototype.slice.call((el || document).querySelectorAll(sel)); };

  // ---------- relative times ----------
  $$('[data-ts]').forEach(function (el) {
    var iso = el.getAttribute('data-ts');
    if (iso) el.title = iso + ' (' + S.ago(iso) + ')';
  });

  // ---------- collapsible panels (per browser) ----------
  $$('[data-panel]').forEach(function (panel) {
    var key = 'steward-collapse:' + panel.dataset.panel;
    try { if (localStorage.getItem(key) === '1') panel.classList.add('collapsed'); } catch (e) {}
    var head = $('[data-toggle]', panel);
    head.addEventListener('click', function (e) {
      if (e.target.closest('a,button,input')) return;
      panel.classList.toggle('collapsed');
      try { localStorage.setItem(key, panel.classList.contains('collapsed') ? '1' : '0'); } catch (err) {}
    });
  });

  // ---------- filtering: repo rail + text ----------
  var repo = '', text = '';
  try { repo = sessionStorage.getItem('steward-repo') || ''; } catch (e) {}
  var input = $('[data-filter-input]');
  function visible(row) {
    if (repo && row.dataset.repo !== repo) return false;
    return !text || row.textContent.toLowerCase().indexOf(text) !== -1;
  }
  function applyFilter() {
    $$('[data-row]').forEach(function (row) {
      var show = visible(row);
      row.hidden = !show;
      var next = row.nextElementSibling;
      if (next && next.hasAttribute('data-detail') && !show) next.hidden = true;
    });
    $$('tr[data-group]').forEach(function (g) {
      var any = false, el = g.nextElementSibling;
      while (el && !el.hasAttribute('data-group')) { if (el.hasAttribute('data-row') && !el.hidden) any = true; el = el.nextElementSibling; }
      g.hidden = !any || !!repo;
    });
    $$('[data-panel]').forEach(function (panel) {
      var rows = $$('[data-row]', panel);
      var shown = rows.filter(function (r) { return !r.hidden; }).length;
      var count = $('[data-count]', panel);
      if (count) count.textContent = rows.length && shown !== rows.length ? shown + '/' + rows.length : rows.length;
      var note = $('.filtered-empty', panel);
      if (rows.length && !shown) {
        if (!note) { note = document.createElement('div'); note.className = 'empty filtered-empty'; panel.appendChild(note); }
        note.textContent = 'Nothing here for this filter.';
        note.hidden = false;
      } else if (note) note.hidden = true;
      var wrap = $('.tablewrap', panel);
      if (wrap) wrap.hidden = rows.length > 0 && !shown;
    });
    $$('[data-rail-repo]').forEach(function (r) { r.classList.toggle('sel', r.dataset.railRepo === repo); });
  }
  function attention() {
    var att = {};
    $$('#decisions [data-row], #ready [data-row], #staged [data-row]').forEach(function (r) {
      if (r.classList.contains('gone')) return;
      att[r.dataset.repo] = (att[r.dataset.repo] || 0) + 1;
      att[''] = (att[''] || 0) + 1;
    });
    $$('[data-rail-repo]').forEach(function (r) {
      var n = att[r.dataset.railRepo] || 0, cell = $('[data-att]', r);
      cell.innerHTML = n ? '<span class="st sig">' + n + '</span>' : '<span class="muted">·</span>';
    });
  }
  $$('[data-rail-repo]').forEach(function (r) {
    r.style.cursor = 'pointer';
    r.addEventListener('click', function () {
      repo = r.dataset.railRepo === repo ? '' : r.dataset.railRepo;
      try { sessionStorage.setItem('steward-repo', repo); } catch (e) {}
      applyFilter();
    });
  });
  if (input) input.addEventListener('input', function () { text = input.value.trim().toLowerCase(); applyFilter(); });
  // GitHub accepted the merge but will do it once refreshed checks pass: not
  // done yet, so the row stays (not struck through) with Merge disabled.
  function queuedRow(row) {
    var live = $('[data-live]', row);
    if (live) live.innerHTML = '<span class="st info" title="GitHub merges it when the refreshed checks pass">auto-merge queued</span>';
    var m = $('[data-act=merge]', row);
    if (m) { m.disabled = true; m.dataset.done = '1'; m.textContent = 'Queued'; m.title = 'GitHub auto-merge is queued'; }
    row.classList.add('gone');
    attention();
  }
  function settle(row, label, tone) {
    row.classList.add('gone', 'done');
    var live = $('[data-live]', row);
    if (live) live.innerHTML = '<span class="st ' + (tone || 'ok') + '">' + esc(label) + '</span>';
    $$('button, input', row).forEach(function (b) { b.disabled = true; });
    attention();
  }

  // ---------- ledger lock ----------
  // Merges, posts and dismissals write the ledger, so they wait while a tick or
  // the decision runner holds it. The Work panel (top bar) says which.
  var ACTS = '[data-act=merge], [data-act=post], [data-act=dismiss]';
  S.onWork(function (w) {
    var lock = w.ledger_lock;
    $$(ACTS).forEach(function (b) {
      if (b.dataset.done) return;
      if (!b.dataset.title) b.dataset.title = b.title;
      b.disabled = !!lock;
      b.title = lock ? 'Waiting: ' + lock.title + ' holds the ledgers. Open the Work panel (w) to see what it is doing.' : b.dataset.title;
    });
    var note = $('[data-lock-note]');
    if (!note) {
      note = document.createElement('div');
      note.setAttribute('data-lock-note', '');
      note.className = 'callout sig';
      note.style.margin = '0 0 16px';
      var readout = $('.readout');
      readout.parentNode.insertBefore(note, readout.nextSibling);
    }
    note.hidden = !lock;
    if (lock) note.innerHTML = '<p style="margin:0"><b>Merges and posts are waiting.</b> ' + esc(lock.title) +
      (lock.detail ? ': ' + esc(lock.detail) : '') + ' (' + S.fmtDur(lock.elapsed_sec) + ' so far). ' +
      '<a href="#" data-open-work>See what it is doing</a>.</p>';
    var link = $('[data-open-work]', note);
    if (link) link.onclick = function (e) { e.preventDefault(); S.openWork(); };
  });
  S.onStatus(function (s, wasBusy) {
    if (wasBusy && !s.tick_active && s.last_tick && s.last_tick.ok) setTimeout(function () { location.reload(); }, 1500);
  });

  function ghstate(url) {
    var m = url.match(/github\.com\/([^/]+\/[^/]+)\/(pull|issues)\/(\d+)/);
    if (!m) return Promise.resolve(null);
    return S.api('/api/ghstate?repo=' + encodeURIComponent(m[1]) + '&num=' + m[3] + '&kind=' + (m[2] === 'pull' ? 'pr' : 'issue'))
      .catch(function () { return { state: 'unknown' }; });
  }

  // ---------- decisions ----------
  // Only the URLs in data-resolve-on decide resolution, and every one must be
  // settled: a PR a decision merely cites is not its subject, and one merged PR
  // out of a queue does not answer the question.
  $$('.decision[data-resolve-on]').forEach(function (d) {
    var urls = d.dataset.resolveOn.split(',').map(function (u) { return u.trim(); }).filter(Boolean);
    Promise.all(urls.map(ghstate)).then(function (states) {
      states = states.filter(Boolean);
      if (!states.length || !states.every(function (s) { return s.merged || s.state === 'closed'; })) return;
      settle(d, states.some(function (s) { return s.merged; }) ? 'merged on GitHub' : 'closed on GitHub');
    });
  });
  function watchDecision(id, d, st, inp) {
    var iv = setInterval(function () {
      S.api('/api/decisions').then(function (res) {
        if (res.executing) return;
        clearInterval(iv);
        var mine = (res.decisions || []).filter(function (e) { return e.ts === id; }).pop() || {};
        if (mine.status === 'executed') {
          st.className = 'state ok'; st.textContent = 'done' + (mine.outcome ? ': ' + mine.outcome : '');
          st.title = mine.outcome || '';
          settle(d, 'decided');
        } else if (mine.status === 'failed') {
          st.className = 'state err'; st.textContent = mine.note || 'failed: see logs/decide.log';
        } else if (mine.note) {
          st.className = 'state err'; st.textContent = 'needs clarification: ' + mine.note; inp.disabled = false;
        } else {
          st.className = 'state'; st.textContent = 'recorded: applies when the steward is free';
        }
      }).catch(function () {});
    }, 4000);
  }
  $$('.decision').forEach(function (d) {
    var inp = $('[data-decide]', d), st = $('[data-decide-state]', d);
    inp.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' || !inp.value.trim()) return;
      var refs = $$('a[href*="/pull/"], a[href*="/issues/"]', d).map(function (a) { return a.getAttribute('href'); });
      inp.disabled = true; st.className = 'state'; st.textContent = 'sending…';
      S.api('/api/decide', {
        repo: d.dataset.repo || '', refs: refs,
        title: ($('[data-title]', d) || {}).textContent || '',
        context: d.textContent.trim().replace(/\s+/g, ' ').slice(0, 1500),
        decision: inp.value.trim()
      }).then(function (res) {
        st.className = 'state ok';
        st.textContent = res.mode === 'executing' ? 'acting on it…' : 'queued: applies when the steward is free';
        watchDecision(res.id, d, st, inp);
      }).catch(function (err) { st.className = 'state err'; st.textContent = err.message; inp.disabled = false; });
    });
  });

  // ---------- ready for final look ----------
  $$('#ready tr[data-item]').forEach(function (row) {
    var repoName = row.dataset.repo, item = row.dataset.item, live = $('[data-live]', row);
    var link = $('a[href*="/pull/"]', row);
    if (link) ghstate(link.getAttribute('href')).then(function (s) {
      if (!s) { live.innerHTML = ''; return; }
      if (s.merged || s.state === 'closed') { settle(row, s.merged ? 'merged' : 'closed', s.merged ? 'ok' : ''); return; }
      if (s.auto_merge && s.behind) {
        // Queued, but main moved on: GitHub's auto-merge will not update the
        // branch itself, so the queue is stuck until someone does. Merge again
        // updates the branch and re-queues.
        live.innerHTML = '<span class="st warn" title="GitHub auto-merge is queued but the branch is behind main; it will not merge until updated">queued · behind main</span>';
        var m = $('[data-act=merge]', row);
        if (m) { m.textContent = 'Update'; m.title = 'Update the branch from main so the queued auto-merge can finish'; }
        return;
      }
      if (s.auto_merge) { queuedRow(row); return; }
      if (s.approved_at_head) {
        var days = s.approved_at ? Math.floor((Date.now() - Date.parse(s.approved_at)) / 864e5) : null;
        live.innerHTML = '<span class="st ' + (days >= 2 ? 'warn' : 'ok') + '" title="approved on GitHub at the current head">approved' +
          (days != null ? ' ' + (days === 0 ? 'today' : days + 'd') : '') + '</span>';
      } else live.innerHTML = '<span class="st">open</span>';
    });

    $('[data-act=merge]', row).addEventListener('click', function () {
      var btn = this;
      S.modal({
        title: 'Approve and merge ' + repoName + ' ' + item, confirm: 'Merge',
        body: '<p>Posts the steward\'s staged review to GitHub <b>under your account</b> (skipped if already posted), then <b>merges the PR</b>. This is your final look.</p>'
      }).then(function (go) {
        if (!go) return;
        btn.disabled = true; btn.textContent = 'Merging…';
        S.api('/api/approve', { repo: repoName, items: [item] }).then(function (res) {
          var outs = res.outcomes ? Object.values(res.outcomes) : [];
          var ok = outs.length && outs.every(function (o) { return o.ok; });
          var merged = outs.some(function (o) { return o.merged; });
          var queued = outs.some(function (o) { return o.queued; });
          if (!ok) throw new Error((outs[0] && outs[0].detail) || 'failed: see approvals.jsonl');
          btn.dataset.done = '1';
          if (queued) {
            queuedRow(row);
            S.toast(repoName + ' ' + item + ': branch updated, GitHub will merge it when checks pass.', 'ok');
          } else {
            settle(row, merged ? 'merged' : 'posted');
            S.toast(repoName + ' ' + item + (merged ? ': approved and merged.' : ': review posted.'), 'ok');
          }
        }).catch(function (e) { btn.disabled = false; btn.textContent = 'Merge'; S.toast(e.message, 'error'); });
      });
    });

    $('[data-act=dismiss]', row).addEventListener('click', function () {
      S.modal({
        title: 'Dismiss ' + repoName + ' ' + item, confirm: 'Dismiss', tone: 'crit',
        body: '<p>Drops it off the queue. Nothing is posted to GitHub, and the steward won\'t re-surface it unless the PR changes.</p>'
      }).then(function (go) {
        if (!go) return;
        S.api('/api/dismiss', { repo: repoName, items: [item] }).then(function () {
          settle(row, 'dismissed', ''); S.toast(repoName + ' ' + item + ' dismissed.', 'warn');
        }).catch(function (e) { S.toast(e.message, 'error'); });
      });
    });

    var detail = null;
    $('[data-act=review]', row).addEventListener('click', function () { toggleReview(); });
    function toggleReview() {
      if (detail) { detail.remove(); detail = null; return; }
      S.api('/api/staged?repo=' + encodeURIComponent(repoName) + '&item=' + encodeURIComponent(item)).then(function (res) {
        var body = (res.item && res.item.staged_actions || [])
          .map(function (a) { return a.body || (a.labels ? 'labels: ' + a.labels.join(', ') : ''); })
          .filter(Boolean).join('\n\n— — —\n\n') || '(no staged text recorded for this item)';
        detail = document.createElement('tr');
        detail.className = 'detail';
        detail.innerHTML = '<td colspan="4"><pre></pre></td>';
        $('pre', detail).textContent = body;
        row.parentNode.insertBefore(detail, row.nextSibling);
      }).catch(function (e) { S.toast(e.message, 'error'); });
    }
    row._open = toggleReview;
  });

  // ---------- staged replies ----------
  $$('tr[data-staged]').forEach(function (row) {
    var detail = row.nextElementSibling;
    function toggle() { detail.hidden = !detail.hidden; $('[data-act=toggle]', row).textContent = detail.hidden ? 'View' : 'Hide'; }
    $('[data-act=toggle]', row).addEventListener('click', toggle);
    row._open = toggle;
    $('[data-act=post]', row).addEventListener('click', function () {
      var btn = this;
      S.modal({
        title: 'Post ' + row.dataset.repo + ' ' + row.dataset.item, confirm: 'Post',
        body: '<p>Posts the staged review or reply to GitHub <b>under your account</b>, signed per your sign-off setting. It never merges or closes.</p>'
      }).then(function (go) {
        if (!go) return;
        btn.disabled = true; btn.textContent = 'Posting…';
        S.api('/api/approve', { repo: row.dataset.repo, items: [row.dataset.item] }).then(function (res) {
          var ok = res.outcomes && Object.values(res.outcomes).every(function (o) { return o.ok; });
          if (!ok) throw new Error('failed: see approvals.jsonl');
          btn.dataset.done = '1'; btn.textContent = 'Posted';
          settle(row, 'posted');
          S.toast(row.dataset.repo + ' ' + row.dataset.item + ' posted.', 'ok');
        }).catch(function (e) { btn.disabled = false; btn.textContent = 'Post'; S.toast(e.message, 'error'); });
      });
    });
  });

  // ---------- keyboard ----------
  S.nav({
    rows: function () { return $$('[data-nav]').filter(function (r) { return !r.hidden && !r.closest('.collapsed'); }); },
    open: function (row) {
      if (row._open) row._open();
      else { var i = $('[data-decide]', row); if (i && !i.disabled) i.focus(); }
    }
  });

  attention();
  applyFilter();
})();
