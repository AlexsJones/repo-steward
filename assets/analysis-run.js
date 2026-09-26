// Run control for an out-of-band analysis job, driven by /api/analysis.
// Mounts into any element with data-analysis-job="insights|evaluation".
(function () {
  var LABELS = {
    insights: { run: 'Run build sweep', noun: 'build-candidate sweep', limit: '~20 min',
      about: 'It reads open issues and PRs from GitHub (read-only), then runs one model session (billed like a tick) ' +
        'for up to ~20 minutes to rank what is worth building. Nothing is posted, and the current list stays until a new one passes validation.' },
    evaluation: { run: 'Run self-evaluation', noun: 'self-evaluation', limit: '~10 min',
      about: 'It runs one model session (billed like a tick) for up to ~10 minutes. ' +
        'It makes no GitHub calls or queue changes, and the current result stays until a new one passes validation.' }
  };
  // Buttons and colours come from assets/steward.css; only the spinner is local.
  var css = document.createElement('style');
  css.textContent =
    '.analysis-run .spin{display:inline-block;width:8px;height:8px;margin-right:4px;background:currentColor;' +
    'animation:analysis-blink 1s steps(2,start) infinite}' +
    '@keyframes analysis-blink{to{visibility:hidden}}';
  document.head.appendChild(css);

  function ago(ts) {
    var sec = Math.max(0, (Date.now() - Date.parse(ts)) / 1000);
    if (sec < 90) return Math.round(sec) + 's';
    if (sec < 5400) return Math.round(sec / 60) + 'm';
    if (sec < 129600) return Math.round(sec / 3600) + 'h';
    return Math.round(sec / 86400) + 'd';
  }

  function mount(root) {
    var job = root.getAttribute('data-analysis-job');
    var label = LABELS[job];
    if (!label) return;
    root.classList.add('analysis-run');
    root.innerHTML = '<button type="button" class="btn primary"></button>' +
      '<button type="button" class="btn crit analysis-cancel" hidden>Cancel</button>' +
      '<span class="analysis-state" aria-live="polite"></span>';
    var button = root.querySelector('button');
    var cancel = root.querySelector('.analysis-cancel');
    var state = root.querySelector('.analysis-state');
    var timer = null;
    var seenRunning = false;
    var baseline = null;

    function render(s) {
      if (s.running) {
        seenRunning = true;
        button.disabled = true;
        cancel.hidden = false;
        cancel.disabled = false;
        button.innerHTML = '<span class="spin"></span>Running…';
        state.className = 'analysis-state';
        state.textContent = s.started_at ? 'started ' + ago(s.started_at) + ' ago · up to ' + label.limit : 'up to ' + label.limit;
        return;
      }
      button.disabled = false;
      cancel.hidden = true;
      button.textContent = label.run;
      var last = s.last;
      if (!last) {
        state.className = 'analysis-state';
        state.textContent = 'never run';
      } else if (last.outcome === 'published') {
        state.className = 'analysis-state';
        state.textContent = 'last published ' + ago(last.ts) + ' ago';
      } else if (last.outcome === 'cancelled') {
        state.className = 'analysis-state';
        state.textContent = 'last run cancelled ' + ago(last.ts) + ' ago';
      } else {
        state.className = 'analysis-state bad';
        state.textContent = 'last run ' + last.outcome + ' ' + ago(last.ts) + ' ago' + (last.detail ? ' — ' + last.detail : '');
      }
    }

    function poll() {
      fetch('/api/analysis').then(function (r) { return r.json(); }).then(function (all) {
        var s = all[job];
        if (!s) return;
        if (baseline === null) baseline = s.last ? s.last.ts : '';
        render(s);
        if (!s.running && seenRunning) {
          seenRunning = false;
          // A fresh result replaces what the page is showing.
          if (s.last && s.last.outcome === 'published' && s.last.ts !== baseline) location.reload();
          baseline = s.last ? s.last.ts : '';
        }
        clearTimeout(timer);
        timer = setTimeout(poll, s.running ? 5000 : 30000);
      }).catch(function () {
        clearTimeout(timer);
        timer = setTimeout(poll, 30000);
      });
    }

    // The shell's dialog when it is loaded; the browser's otherwise.
    function ask(title, body, confirmLabel, tone) {
      if (window.Steward) return window.Steward.modal({ title: title, body: '<p>' + body + '</p>', confirm: confirmLabel, tone: tone });
      return Promise.resolve(confirm(title + '\n\n' + body));
    }
    function fail(e) {
      if (window.Steward) window.Steward.toast(e.message, 'error'); else alert(e.message);
      poll();
    }

    cancel.addEventListener('click', function () {
      ask('Stop the running ' + label.noun, 'It is discarded. The published result does not change.', 'Stop', 'crit').then(function (go) {
      if (!go) return;
      cancel.disabled = true;
      post({ job: job, action: 'cancel' }).then(function () {
        state.className = 'analysis-state';
        state.textContent = 'stopping…';
        clearTimeout(timer);
        timer = setTimeout(poll, 1000);
      }).catch(fail);
      });
    });

    function post(body) {
      return fetch('/api/analysis', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      }).then(function (r) {
        return r.json().then(function (x) { if (!r.ok) throw new Error(x.error || 'request failed'); return x; });
      });
    }

    button.addEventListener('click', function () {
      ask('Start a ' + label.noun, label.about, 'Start', 'sig').then(function (go) {
      if (!go) return;
      button.disabled = true;
      post({ job: job }).then(function (all) {
        seenRunning = true;
        // The unit starts asynchronously; show it as running until the lock appears.
        render(all[job] && all[job].running ? all[job] : { running: true });
        clearTimeout(timer);
        timer = setTimeout(poll, 3000);
      }).catch(fail);
      });
    });

    poll();
  }

  document.querySelectorAll('[data-analysis-job]').forEach(mount);
})();
