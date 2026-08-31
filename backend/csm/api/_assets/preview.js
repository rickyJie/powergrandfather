(function() {
  var toast = document.getElementById('toast');
  var toastMsg = toast.querySelector('span');
  var toastTimer = null;
  function showToast(msg) {
    toastMsg.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="check"><polyline points="20 6 9 17 4 12"/></svg> ' + msg;
    toast.classList.add('visible');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function() { toast.classList.remove('visible'); }, 1600);
  }
  function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed'; ta.style.top = '-1000px';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch (e) {}
    document.body.removeChild(ta);
    return Promise.resolve();
  }
  // Copy-path button
  document.querySelectorAll('[data-copy-path]').forEach(function(btn) {
    btn.addEventListener('click', function() {
      copy(btn.getAttribute('data-copy-path')).then(function() { showToast('Path copied'); });
    });
  });
  // Code-toolbar copy — target the <pre> inside the same .code-container
  document.querySelectorAll('.code-container').forEach(function(container) {
    var btn = container.querySelector('[data-copy-code]');
    var pre = container.querySelector('pre');
    if (!btn || !pre) return;
    btn.addEventListener('click', function() {
      copy(pre.innerText).then(function() {
        btn.classList.add('copied');
        var orig = btn.innerHTML;
        btn.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg> Copied';
        setTimeout(function() {
          btn.classList.remove('copied');
          btn.innerHTML = orig;
        }, 1400);
      });
    });
    // Wrap toggle
    var wrapBtn = container.querySelector('[data-toggle-wrap]');
    var scroll = container.querySelector('.code-scroll');
    if (wrapBtn && scroll) {
      wrapBtn.addEventListener('click', function() {
        var wrapped = scroll.classList.toggle('wrap');
        wrapBtn.classList.toggle('copied', wrapped);
      });
    }
  });
  // Copy in-markdown <pre> blocks (no toolbar, hover-only copy button)
  document.querySelectorAll('.md-body pre').forEach(function(pre) {
    if (pre.dataset.hasCopy) return;
    pre.dataset.hasCopy = '1';
    pre.style.position = 'relative';
    var btn = document.createElement('button');
    btn.className = 'icon-btn';
    btn.type = 'button';
    btn.textContent = 'Copy';
    btn.style.cssText = 'position:absolute;top:6px;right:6px;opacity:0;transition:opacity 120ms;';
    pre.appendChild(btn);
    pre.addEventListener('mouseenter', function() { btn.style.opacity = '1'; });
    pre.addEventListener('mouseleave', function() { btn.style.opacity = '0'; });
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      var code = pre.querySelector('code') || pre;
      copy(code.innerText).then(function() {
        btn.textContent = 'Copied';
        setTimeout(function() { btn.textContent = 'Copy'; }, 1400);
      });
    });
  });
  // Line-number click → copy #Lxx anchor to clipboard
  document.querySelectorAll('.pyghi .lineno').forEach(function(el, i) {
    el.addEventListener('click', function() {
      var line = (el.textContent || '').trim().replace(/[^0-9]/g, '');
      if (!line) return;
      var url = window.location.origin + window.location.pathname + window.location.search + '#L' + line;
      copy(url).then(function() { showToast('Line link copied (#L' + line + ')'); });
    });
  });
  // Build TOC from md-body headings + scroll spy
  var mdBody = document.querySelector('.md-body');
  var mdLayout = document.querySelector('.md-layout');
  var tocEl = document.getElementById('md-toc');
  if (mdBody && tocEl) {
    var heads = mdBody.querySelectorAll('h1, h2, h3, h4');
    if (heads.length >= 2) {
      var ul = document.createElement('ul');
      var links = [];
      heads.forEach(function(h, i) {
        if (!h.id) h.id = 'toc-h-' + i;
        var li = document.createElement('li');
        li.className = h.tagName.toLowerCase();
        var a = document.createElement('a');
        a.href = '#' + h.id;
        a.textContent = h.textContent;
        li.appendChild(a);
        ul.appendChild(li);
        links.push({ a: a, h: h });
      });
      tocEl.appendChild(ul);
      // Back-to-top
      var backTop = document.createElement('div');
      backTop.className = 'back-top';
      backTop.innerHTML = '<a href="#top" title="Back to top">↑ Back to top</a>';
      tocEl.appendChild(backTop);
      backTop.querySelector('a').addEventListener('click', function(e) {
        e.preventDefault();
        window.scrollTo({ top: 0, behavior: 'smooth' });
      });
      // Scroll spy via IntersectionObserver
      if ('IntersectionObserver' in window) {
        var observer = new IntersectionObserver(function(entries) {
          entries.forEach(function(entry) {
            if (entry.isIntersecting) {
              var id = entry.target.id;
              links.forEach(function(pair) {
                pair.a.classList.toggle('active', pair.h.id === id);
              });
            }
          });
        }, { rootMargin: '-80px 0px -60% 0px' });
        heads.forEach(function(h) { observer.observe(h); });
      }
    } else {
      if (mdLayout) mdLayout.classList.add('md-no-toc');
    }
  }
  // Keyboard shortcuts
  document.addEventListener('keydown', function(e) {
    // Ignore when typing in an input / contenteditable
    var tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || e.target.isContentEditable) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === 'c') {
      var btn = document.querySelector('[data-copy-path]');
      if (btn) { btn.click(); e.preventDefault(); }
    } else if (e.key === 'd') {
      var dl = document.querySelector('.btn-primary[download]');
      if (dl) { dl.click(); e.preventDefault(); }
    } else if (e.key === 'w') {
      var wrapBtn = document.querySelector('[data-toggle-wrap]');
      if (wrapBtn) { wrapBtn.click(); e.preventDefault(); }
    } else if (e.key === 's') {
      var srcTab = document.querySelector('.tab-btn[data-view="source"]');
      var renderedTab = document.querySelector('.tab-btn[data-view="rendered"]');
      if (srcTab && renderedTab) {
        (srcTab.classList.contains('active') ? renderedTab : srcTab).click();
        e.preventDefault();
      }
    }
  });
})();
