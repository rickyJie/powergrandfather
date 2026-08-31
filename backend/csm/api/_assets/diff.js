(function() {
  var sections = Array.prototype.slice.call(document.querySelectorAll('.file-section'));
  var sidebarItems = Array.prototype.slice.call(document.querySelectorAll('.sidebar-item'));
  if (!sections.length) return;
  var currentFileIdx = 0;
  var currentChangeIdx = -1;

  // ---- Collapse: click header or press 'c' when focused. ----
  sections.forEach(function(sec, i) {
    var head = sec.querySelector('.file-section-head');
    if (!head) return;
    head.addEventListener('click', function(ev) {
      // Ignore clicks on inline action buttons / links.
      if (ev.target.closest('.file-actions') || ev.target.closest('a')) return;
      sec.classList.toggle('collapsed');
    });
  });

  // ---- Copy path (per-file action). ----
  document.querySelectorAll('.file-action[data-copy-path]').forEach(function(btn) {
    btn.addEventListener('click', function(ev) {
      ev.stopPropagation();
      var val = btn.getAttribute('data-copy-path');
      var writer = (navigator.clipboard && navigator.clipboard.writeText)
        ? navigator.clipboard.writeText(val)
        : Promise.reject();
      writer.then(function() {
        btn.classList.add('copied');
        setTimeout(function() { btn.classList.remove('copied'); }, 1200);
      }).catch(function() {
        // Fallback: textarea + execCommand.
        var ta = document.createElement('textarea');
        ta.value = val; ta.style.cssText = 'position:fixed;left:-1000px;top:0';
        document.body.appendChild(ta); ta.select();
        try { document.execCommand('copy'); } catch(_) {}
        document.body.removeChild(ta);
        btn.classList.add('copied');
        setTimeout(function() { btn.classList.remove('copied'); }, 1200);
      });
    });
  });

  // ---- Scroll-spy: highlight sidebar item for the file currently in view. ----
  if ('IntersectionObserver' in window && sidebarItems.length) {
    var anchorToItem = {};
    sidebarItems.forEach(function(a) {
      var href = a.getAttribute('href') || '';
      if (href.startsWith('#')) anchorToItem[href.slice(1)] = a;
    });
    var observer = new IntersectionObserver(function(entries) {
      // Pick the entry with the largest intersection ratio in view.
      var best = null;
      entries.forEach(function(e) {
        if (!e.isIntersecting) return;
        if (!best || e.intersectionRatio > best.intersectionRatio) best = e;
      });
      if (!best) return;
      var id = best.target.id;
      sidebarItems.forEach(function(a) { a.classList.remove('spy-active'); });
      var hit = anchorToItem[id];
      if (hit) hit.classList.add('spy-active');
      var idx = sections.indexOf(best.target);
      if (idx >= 0) { currentFileIdx = idx; currentChangeIdx = -1; }
    }, { rootMargin: '-20% 0px -60% 0px', threshold: [0, 0.1, 0.5, 1] });
    sections.forEach(function(s) { observer.observe(s); });
  }

  // ---- Keyboard navigation. ----
  function scrollFile(idx) {
    if (idx < 0) idx = 0;
    if (idx >= sections.length) idx = sections.length - 1;
    currentFileIdx = idx;
    currentChangeIdx = -1;
    sections[idx].scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  function collectChanges() {
    // Any +/- row in any file, in DOM order — [ / ] walks these.
    return Array.prototype.slice.call(
      document.querySelectorAll('.diff-table tr.diff-add, .diff-table tr.diff-del, .diff-table tr.diff-hunk')
    );
  }
  var changes = null;
  function scrollChange(delta) {
    if (changes === null) changes = collectChanges();
    if (!changes.length) return;
    currentChangeIdx += delta;
    if (currentChangeIdx < 0) currentChangeIdx = 0;
    if (currentChangeIdx >= changes.length) currentChangeIdx = changes.length - 1;
    changes[currentChangeIdx].scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  document.addEventListener('keydown', function(ev) {
    var tag = (ev.target && ev.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || (ev.target && ev.target.isContentEditable)) return;
    if (ev.metaKey || ev.ctrlKey || ev.altKey) return;
    switch (ev.key) {
      case 'j': ev.preventDefault(); scrollFile(currentFileIdx + 1); break;
      case 'k': ev.preventDefault(); scrollFile(currentFileIdx - 1); break;
      case ']': ev.preventDefault(); scrollChange(1); break;
      case '[': ev.preventDefault(); scrollChange(-1); break;
      case 'c':
        ev.preventDefault();
        var sec = sections[currentFileIdx];
        if (sec) sec.classList.toggle('collapsed');
        break;
    }
  });
})();
