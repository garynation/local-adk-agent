/**
 * ADK Personal Agent & Research Workbench — GM3 Expressive Frontend Controller (Bill Skill v2.2)
 * Handles:
 *   1. Default 'auto' Theme Mode (brightness_auto -> dark_mode -> light_mode)
 *   2. Responsive Hamburger Toggle (Desktop 240px->80px rail, Mobile <=1024px slide-over drawer + 3-way close)
 *   3. Multi-Stage / Hash / Query View Switching (#view-workbench <-> #view-settings)
 *   4. Auto-Hiding Scrollbars, Dynamic Scroll-Edge Fades & Chat Auto-Scroll to Bottom
 *   5. Live SSE Chat & Deep Research Streaming + Expandable Tool/Grounding/Screenshot Accordions
 *   6. Live Configuration & Hot-Reload API Wiring (/api/config/*, /api/auth/workspace/*, /api/browser/*)
 */

(function () {
  'use strict';

  // ---------------------------------------------------------------------------
  // 1. Default 'auto' Theme Controller (THEME_MUST_START_IN_AUTO_MODE)
  // ---------------------------------------------------------------------------
  var THEME_STORAGE_KEY = 'gm3_theme_mode';
  var themeToggleBtn = document.getElementById('theme-toggle-btn');
  var themeToggleIcon = document.getElementById('theme-toggle-icon');
  var darkMediaQuery = window.matchMedia
    ? window.matchMedia('(prefers-color-scheme: dark)')
    : { matches: false, addEventListener: function () {} };

  var currentThemeMode = localStorage.getItem(THEME_STORAGE_KEY) || 'auto';

  function applyTheme(mode) {
    currentThemeMode = mode;
    var effectiveTheme =
      mode === 'auto' ? (darkMediaQuery.matches ? 'dark' : 'light') : mode;
    document.documentElement.setAttribute('data-theme', effectiveTheme);
    if (document.body) {
      document.body.setAttribute('data-theme', effectiveTheme);
    }

    if (themeToggleBtn && themeToggleIcon) {
      var iconName = 'brightness_auto';
      var label = 'Theme: Auto (system)';
      if (mode === 'dark') {
        iconName = 'dark_mode';
        label = 'Theme: Dark';
      } else if (mode === 'light') {
        iconName = 'light_mode';
        label = 'Theme: Light';
      }
      themeToggleIcon.textContent = iconName;
      themeToggleBtn.setAttribute('aria-label', label);
      themeToggleBtn.setAttribute('title', label);
      themeToggleBtn.setAttribute('data-theme-mode', mode);
    }
  }

  applyTheme(currentThemeMode);

  if (themeToggleBtn) {
    themeToggleBtn.addEventListener('click', function () {
      var nextMode =
        currentThemeMode === 'auto'
          ? 'dark'
          : currentThemeMode === 'dark'
            ? 'light'
            : 'auto';
      localStorage.setItem(THEME_STORAGE_KEY, nextMode);
      applyTheme(nextMode);
    });
  }

  if (darkMediaQuery.addEventListener) {
    darkMediaQuery.addEventListener('change', function () {
      if (currentThemeMode === 'auto') {
        applyTheme('auto');
      }
    });
  }

  // ---------------------------------------------------------------------------
  // 2. Responsive Navigation Shell (Desktop Rail Collapse + Mobile Drawer)
  // ---------------------------------------------------------------------------
  var menuToggleBtn = document.getElementById('menu-toggle-btn');
  var sideNav = document.getElementById('side-nav');
  var navScrim = document.getElementById('nav-scrim');

  function closeMobileDrawer() {
    if (!sideNav) return;
    sideNav.classList.remove('drawer-open');
    if (navScrim) navScrim.classList.remove('drawer-open');
    if (menuToggleBtn) menuToggleBtn.setAttribute('aria-expanded', 'false');
  }

  if (menuToggleBtn && sideNav) {
    menuToggleBtn.addEventListener('click', function () {
      if (window.innerWidth <= 1024) {
        var isOpen = sideNav.classList.toggle('drawer-open');
        if (navScrim) navScrim.classList.toggle('drawer-open', isOpen);
        menuToggleBtn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
      } else {
        var isCollapsed = sideNav.classList.toggle('rail-collapsed');
        menuToggleBtn.setAttribute(
          'aria-expanded',
          isCollapsed ? 'false' : 'true'
        );
      }
    });
  }

  if (navScrim) {
    navScrim.addEventListener('click', closeMobileDrawer);
  }

  document.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') {
      closeMobileDrawer();
    }
  });

  // ---------------------------------------------------------------------------
  // 3. View Switching (#view-workbench <-> #view-schedules <-> #view-settings)
  // ---------------------------------------------------------------------------
  var viewWorkbench = document.getElementById('view-workbench');
  var viewSchedules = document.getElementById('view-schedules');
  var viewSettings = document.getElementById('view-settings');
  var navItems = Array.prototype.slice.call(
    document.querySelectorAll('.nav-item[data-view]')
  );

  function setPanelVisible(panel, isVisible) {
    if (!panel) return;
    if (isVisible) {
      panel.hidden = false;
      panel.removeAttribute('hidden');
      panel.classList.add('active');
      panel.style.display = '';
    } else {
      panel.hidden = true;
      panel.setAttribute('hidden', '');
      panel.classList.remove('active');
      panel.style.display = 'none';
    }
  }

  function switchView(targetView) {
    var normalized =
      targetView === 'settings'
        ? 'settings'
        : targetView === 'schedules'
          ? 'schedules'
          : 'workbench';
    setPanelVisible(viewWorkbench, normalized === 'workbench');
    setPanelVisible(viewSchedules, normalized === 'schedules');
    setPanelVisible(viewSettings, normalized === 'settings');

    if (normalized === 'workbench') {
      scrollChatToBottom();
    } else if (normalized === 'schedules' && typeof loadSchedulesList === 'function') {
      loadSchedulesList();
    }
    navItems.forEach(function (btn) {
      var isMatch = btn.getAttribute('data-view') === normalized;
      btn.classList.toggle('active', isMatch);
      btn.setAttribute('aria-current', isMatch ? 'page' : 'false');
    });
    closeMobileDrawer();
    refreshAllScrollFades();
  }

  navItems.forEach(function (btn) {
    btn.addEventListener('click', function (ev) {
      ev.preventDefault();
      var target = btn.getAttribute('data-view') || 'workbench';
      switchView(target);
      if (history.replaceState) {
        history.replaceState(null, '', '#' + target);
      }
    });
  });

  function syncViewFromLocation() {
    var hash = (window.location.hash || '').replace(/^#/, '').toLowerCase();
    var params = new URLSearchParams(window.location.search);
    var queryView = (params.get('view') || params.get('settings') || '').toLowerCase();
    if (hash === 'settings' || queryView === 'settings' || queryView === 'workspace') {
      switchView('settings');
    } else if (hash === 'schedules' || queryView === 'schedules') {
      switchView('schedules');
    } else {
      switchView('workbench');
    }
  }

  window.addEventListener('hashchange', syncViewFromLocation);

  // ---------------------------------------------------------------------------
  // 4. Auto-Hiding Scrollbars, Dynamic Edge Fades & Chat Auto-Scroll to Bottom
  // ---------------------------------------------------------------------------
  var chatStream = document.getElementById('chat-stream');
  var scrollTimers = new WeakMap();

  function updateScrollFade(el) {
    if (!el) return;
    var canScroll = el.scrollHeight > el.clientHeight + 8;
    if (!canScroll) {
      el.classList.remove('has-scroll-top', 'has-scroll-bottom');
      return;
    }
    var hasTop = el.scrollTop > 4;
    var hasBottom = el.scrollHeight - el.scrollTop - el.clientHeight > 4;
    el.classList.toggle('has-scroll-top', hasTop);
    el.classList.toggle('has-scroll-bottom', hasBottom);
  }

  function scrollChatToBottom() {
    if (!chatStream) return;
    chatStream.scrollTop = chatStream.scrollHeight;
    updateScrollFade(chatStream);
  }

  function refreshAllScrollFades() {
    var panels = document.querySelectorAll(
      '[data-scroll-fade], .chat-stream, .session-list, .context-pane'
    );
    Array.prototype.forEach.call(panels, function (panel) {
      updateScrollFade(panel);
    });
  }

  Array.prototype.forEach.call(
    document.querySelectorAll(
      '[data-scroll-fade], .chat-stream, .session-list, .context-pane'
    ),
    function (panel) {
      panel.addEventListener(
        'scroll',
        function () {
          panel.classList.add('is-scrolling');
          updateScrollFade(panel);
          var prevTimer = scrollTimers.get(panel);
          if (prevTimer) clearTimeout(prevTimer);
          var timer = setTimeout(function () {
            panel.classList.remove('is-scrolling');
          }, 650);
          scrollTimers.set(panel, timer);
        },
        { passive: true }
      );
    }
  );

  // Immediately scroll chat to bottom and compute scroll-edge fades on startup
  scrollChatToBottom();
  refreshAllScrollFades();
  syncViewFromLocation();
  window.addEventListener('resize', refreshAllScrollFades);

  // ---------------------------------------------------------------------------
  // 5. Interactive Session Switching (Clean State, Zero Demo Seeds)
  // ---------------------------------------------------------------------------
  var currentSessionId = 'sess_' + Math.random().toString(36).slice(2, 10);
  var currentMode = 'standard';

  function renderEmptyChatState() {
    if (!chatStream) return;
    chatStream.innerHTML = [
      '<div id="chat-empty-state" class="card-nested" style="background: var(--md-sys-color-surface-container);">',
      '  <div class="row-between">',
      '    <h4>Ready for Chat &amp; Deep Research</h4>',
      '    <span class="chat-turn-badge">Clean workspace</span>',
      '  </div>',
      '  <p class="meta" style="margin: 0;">Send a prompt below to start a new conversation, query your connected Google Workspace (Gmail, Calendar, Drive, Docs), or run multi-step Deep Research.</p>',
      '</div>'
    ].join('');
    updateScrollFade(chatStream);
  }

  function loadSessionsList() {
    var sessionListEl = document.getElementById('session-list');
    var countLabel = document.getElementById('session-count-label');
    if (!sessionListEl) return;

    fetch('/api/sessions')
      .then(function (r) {
        return r.ok ? r.json() : { sessions: [] };
      })
      .then(function (data) {
        var list = (data && Array.isArray(data.sessions)) ? data.sessions : [];
        if (countLabel) {
          countLabel.textContent = '· ' + list.length;
        }
        if (list.length === 0) {
          sessionListEl.innerHTML =
            '<p id="session-empty-state" class="meta" style="margin: 8px 4px;">No conversations yet. Send a message or click + New to start.</p>';
          return;
        }
        sessionListEl.innerHTML = list
          .map(function (s) {
            var sid = s.id || s.session_id || '';
            var title = s.title || sid || 'Conversation';
            var channel = s.origin_channel === 'gchat' ? 'Google Chat' : 'Web UI';
            var isActive = sid === currentSessionId;
            return (
              '<div class="row-between" style="gap: 4px;">' +
              '  <button type="button" class="session-item' +
              (isActive ? ' active' : '') +
              '" data-session-id="' +
              escapeHtml(sid) +
              '" style="flex: 1;">' +
              '    <span class="session-item-title">' +
              escapeHtml(title) +
              '</span>' +
              '    <span class="session-item-meta">' +
              escapeHtml(channel) +
              '</span>' +
              '  </button>' +
              '  <button type="button" class="icon-btn" data-delete-session="' +
              escapeHtml(sid) +
              '" title="Delete conversation" aria-label="Delete conversation" style="width: 32px; height: 32px;">' +
              '    <span class="material-symbols-outlined" style="font-size: 16px;">close</span>' +
              '  </button>' +
              '</div>'
            );
          })
          .join('');
        bindSessionItems();
        updateScrollFade(sessionListEl);
      })
      .catch(function () {});
  }

  function bindSessionItems() {
    var sessionButtons = document.querySelectorAll('.session-item[data-session-id]');
    Array.prototype.forEach.call(sessionButtons, function (btn) {
      btn.onclick = function () {
        var sessId = btn.getAttribute('data-session-id');
        if (!sessId) return;
        currentSessionId = sessId;
        Array.prototype.forEach.call(sessionButtons, function (b) {
          b.classList.toggle('active', b === btn);
        });
        loadRemoteSessionHistory(sessId);
      };
    });

    var deleteButtons = document.querySelectorAll('[data-delete-session]');
    Array.prototype.forEach.call(deleteButtons, function (delBtn) {
      delBtn.onclick = function (ev) {
        ev.stopPropagation();
        var sessId = delBtn.getAttribute('data-delete-session');
        if (!sessId) return;
        fetch('/api/sessions/' + encodeURIComponent(sessId), { method: 'DELETE' })
          .then(function () {
            if (currentSessionId === sessId) {
              currentSessionId = 'sess_' + Math.random().toString(36).slice(2, 10);
              renderEmptyChatState();
              var titleEl = document.getElementById('active-session-title');
              if (titleEl) {
                titleEl.innerHTML = 'New conversation <span class="header-count">· Ready</span>';
              }
            }
            loadSessionsList();
          })
          .catch(function () {});
      };
    });
  }

  loadSessionsList();

  var newSessionBtn = document.getElementById('new-session-btn');
  if (newSessionBtn) {
    newSessionBtn.addEventListener('click', function () {
      currentSessionId = 'sess_' + Math.random().toString(36).slice(2, 10);
      var titleEl = document.getElementById('active-session-title');
      var subEl = document.getElementById('active-session-subtitle');
      if (titleEl) {
        titleEl.innerHTML = 'New conversation <span class="header-count">· Ready</span>';
      }
      if (subEl) {
        subEl.textContent = 'Shared SQLite session · Web UI & Google Chat';
      }
      renderEmptyChatState();
      Array.prototype.forEach.call(
        document.querySelectorAll('.session-item.active'),
        function (b) {
          b.classList.remove('active');
        }
      );
      var chatInput = document.getElementById('chat-input');
      if (chatInput) chatInput.focus();
    });
  }

  // Event delegation for expandable trace accordions, citation pills, and inline turn actions
  document.addEventListener('click', function (ev) {
    var traceHeader = ev.target.closest('[data-trace-toggle]');
    if (traceHeader) {
      var box = traceHeader.closest('.step-trace-box');
      var body = box ? box.querySelector('.step-trace-body') : null;
      if (body) {
        var isHidden = body.hasAttribute('hidden');
        if (isHidden) {
          body.removeAttribute('hidden');
          traceHeader.setAttribute('aria-expanded', 'true');
        } else {
          body.setAttribute('hidden', '');
          traceHeader.setAttribute('aria-expanded', 'false');
        }
        updateScrollFade(chatStream);
      }
      return;
    }

    var citePill = ev.target.closest('[data-cite-id]');
    if (citePill) {
      var targetId = citePill.getAttribute('data-cite-id');
      var sourceCard = targetId ? document.getElementById(targetId) : null;
      if (sourceCard) {
        sourceCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
      return;
    }

    var actionBtn = ev.target.closest('[data-chat-action]');
    if (actionBtn) {
      var action = actionBtn.getAttribute('data-chat-action');
      if (action === 'feedback-up') {
        actionBtn.classList.toggle('is-active');
      } else if (action === 'copy') {
        var turn = actionBtn.closest('.chat-msg');
        var proseNode = turn ? turn.querySelector('.turn-prose') : null;
        var text = proseNode ? proseNode.innerText : (turn ? turn.innerText : '');
        if (navigator.clipboard && text) {
          navigator.clipboard.writeText(text).catch(function () {});
        }
        actionBtn.classList.add('is-active');
      }
      return;
    }

    var codeCopyBtn = ev.target.closest('[data-copy-code]');
    if (codeCopyBtn) {
      var container = codeCopyBtn.closest('.md-code-container');
      var codeEl = container ? container.querySelector('code') : null;
      if (navigator.clipboard && codeEl) {
        navigator.clipboard.writeText(codeEl.textContent || '').catch(function () {});
        var labelSpan = codeCopyBtn.querySelector('span:last-child');
        if (labelSpan) {
          var prev = labelSpan.textContent;
          labelSpan.textContent = 'Copied';
          setTimeout(function () {
            labelSpan.textContent = prev;
          }, 1500);
        }
      }
      return;
    }

    var suggestionChip = ev.target.closest('.prompt-suggestion-chip');
    if (suggestionChip) {
      var input = document.getElementById('chat-input');
      if (input) {
        input.value = (suggestionChip.textContent || '').trim();
        input.focus();
      }
    }
  });

  // Morphing tab bars (#deep-research-toggle & #settings-filter-tabs)
  var modeToggle = document.getElementById('deep-research-toggle');
  if (modeToggle) {
    modeToggle.addEventListener('click', function (ev) {
      var tab = ev.target.closest('.m3-pill-tab');
      if (!tab) return;
      currentMode = tab.getAttribute('data-mode') || 'standard';
      Array.prototype.forEach.call(
        modeToggle.querySelectorAll('.m3-pill-tab'),
        function (t) {
          var active = t === tab;
          t.classList.toggle('active', active);
          t.setAttribute('aria-selected', active ? 'true' : 'false');
        }
      );
    });
  }

  var settingsFilterTabs = document.getElementById('settings-filter-tabs');
  if (settingsFilterTabs) {
    settingsFilterTabs.addEventListener('click', function (ev) {
      var tab = ev.target.closest('.m3-pill-tab');
      if (!tab) return;
      var filter = tab.getAttribute('data-filter') || 'all';
      Array.prototype.forEach.call(
        settingsFilterTabs.querySelectorAll('.m3-pill-tab'),
        function (t) {
          var active = t === tab;
          t.classList.toggle('active', active);
          t.setAttribute('aria-selected', active ? 'true' : 'false');
        }
      );
      Array.prototype.forEach.call(
        document.querySelectorAll('[data-settings-section]'),
        function (card) {
          var section = card.getAttribute('data-settings-section');
          card.hidden = filter !== 'all' && section !== filter;
        }
      );
    });
  }

  // ---------------------------------------------------------------------------
  // 6. Professional GFM Markdown Renderer & Live SSE Chat Streaming
  // ---------------------------------------------------------------------------
  function escapeHtml(str) {
    return String(str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function renderInlineMarkdown(raw) {
    var codeTokens = [];
    var text = String(raw || '').replace(/`([^`\n]+)`/g, function (_, code) {
      var idx = codeTokens.length;
      codeTokens.push('<code class="md-inline-code">' + escapeHtml(code) + '</code>');
      return '\x00CODE' + idx + '\x00';
    });

    text = escapeHtml(text);

    // Markdown links: [label](https://...)
    text = text.replace(
      /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
    );

    // Bold + Italic, Bold, Italic, Strikethrough
    text = text.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
    text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/__([^_]+)__/g, '<strong>$1</strong>');
    text = text.replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,!?:;]|$)/g, '$1<em>$2</em>');
    text = text.replace(/~~([^~]+)~~/g, '<del>$1</del>');

    // Restore inline code spans
    text = text.replace(/\x00CODE(\d+)\x00/g, function (_, idx) {
      return codeTokens[Number(idx)] || '';
    });
    return text;
  }

  function renderMarkdown(md) {
    if (!md) return '';
    var normalized = String(md).replace(/\r\n/g, '\n');

    // 1. Extract fenced code blocks first so their contents are untouched
    var codeBlocks = [];
    normalized = normalized.replace(
      /```([a-zA-Z0-9_-]*)\n([\s\S]*?)```/g,
      function (_, lang, code) {
        var idx = codeBlocks.length;
        var cleanLang = (lang || 'code').trim().toLowerCase();
        var trimmedCode = code.replace(/\n$/, '');
        codeBlocks.push(
          '<div class="md-code-container">' +
            '<div class="md-code-header">' +
            '<span>' + escapeHtml(cleanLang) + '</span>' +
            '<button type="button" class="md-code-copy-btn" data-copy-code="true">' +
            '<span class="material-symbols-outlined" style="font-size:13px;">content_copy</span>' +
            '<span>Copy</span>' +
            '</button>' +
            '</div>' +
            '<pre class="md-code-block"><code>' +
            escapeHtml(trimmedCode) +
            '</code></pre>' +
            '</div>'
        );
        return '\n@@MD_CODE_BLOCK_' + idx + '@@\n';
      }
    );

    var lines = normalized.split('\n');
    var htmlOut = [];
    var i = 0;

    while (i < lines.length) {
      var line = lines[i];
      var trimmed = line.trim();

      if (!trimmed) {
        i++;
        continue;
      }

      // Fenced code block placeholder
      var codeMatch = trimmed.match(/^@@MD_CODE_BLOCK_(\d+)@@$/);
      if (codeMatch) {
        htmlOut.push(codeBlocks[Number(codeMatch[1])] || '');
        i++;
        continue;
      }

      // Horizontal rule
      if (/^(---|\*\*\*|___)\s*$/.test(trimmed)) {
        htmlOut.push('<hr class="md-hr" />');
        i++;
        continue;
      }

      // Headings (# to ####)
      var headingMatch = trimmed.match(/^(#{1,4})\s+(.+)$/);
      if (headingMatch) {
        var level = headingMatch[1].length;
        htmlOut.push(
          '<h' + level + '>' + renderInlineMarkdown(headingMatch[2]) + '</h' + level + '>'
        );
        i++;
        continue;
      }

      // GFM Table (header row with pipes + separator row with |---|)
      if (
        trimmed.indexOf('|') !== -1 &&
        i + 1 < lines.length &&
        /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1])
      ) {
        var parseRow = function (rowStr) {
          var s = rowStr.trim();
          if (s.charAt(0) === '|') s = s.slice(1);
          if (s.charAt(s.length - 1) === '|') s = s.slice(0, -1);
          return s.split('|').map(function (c) {
            return c.trim();
          });
        };
        var headers = parseRow(line);
        i += 2; // Skip header + separator
        var bodyRows = [];
        while (
          i < lines.length &&
          lines[i].trim() &&
          lines[i].indexOf('|') !== -1 &&
          !/^@@MD_CODE_BLOCK_/.test(lines[i].trim())
        ) {
          bodyRows.push(parseRow(lines[i]));
          i++;
        }
        var tableHtml =
          '<div class="md-table-wrap"><table class="md-table"><thead><tr>' +
          headers
            .map(function (h) {
              return '<th>' + renderInlineMarkdown(h) + '</th>';
            })
            .join('') +
          '</tr></thead><tbody>' +
          bodyRows
            .map(function (r) {
              return (
                '<tr>' +
                headers
                  .map(function (_, colIdx) {
                    return '<td>' + renderInlineMarkdown(r[colIdx] || '') + '</td>';
                  })
                  .join('') +
                '</tr>'
              );
            })
            .join('') +
          '</tbody></table></div>';
        htmlOut.push(tableHtml);
        continue;
      }

      // Blockquote (> ...)
      if (/^>\s?/.test(trimmed)) {
        var quoteLines = [];
        while (i < lines.length && /^>\s?/.test(lines[i].trim())) {
          quoteLines.push(lines[i].trim().replace(/^>\s?/, ''));
          i++;
        }
        htmlOut.push(
          '<blockquote class="md-blockquote">' +
            renderInlineMarkdown(quoteLines.join('<br>')) +
            '</blockquote>'
        );
        continue;
      }

      // Unordered list (-, *, +)
      if (/^[-*+]\s+/.test(trimmed)) {
        var ulItems = [];
        while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
          ulItems.push(lines[i].replace(/^\s*[-*+]\s+/, '').trim());
          i++;
        }
        htmlOut.push(
          '<ul>' +
            ulItems
              .map(function (item) {
                return '<li>' + renderInlineMarkdown(item) + '</li>';
              })
              .join('') +
            '</ul>'
        );
        continue;
      }

      // Ordered list (1. , 2. )
      if (/^\d+[.)]\s+/.test(trimmed)) {
        var olItems = [];
        while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
          olItems.push(lines[i].replace(/^\s*\d+[.)]\s+/, '').trim());
          i++;
        }
        htmlOut.push(
          '<ol>' +
            olItems
              .map(function (item) {
                return '<li>' + renderInlineMarkdown(item) + '</li>';
              })
              .join('') +
            '</ol>'
        );
        continue;
      }

      // Standard paragraph (collect consecutive non-block lines)
      var paraLines = [];
      while (
        i < lines.length &&
        lines[i].trim() &&
        !/^@@MD_CODE_BLOCK_\d+@@$/.test(lines[i].trim()) &&
        !/^(#{1,4})\s+/.test(lines[i].trim()) &&
        !/^(---|\*\*\*|___)\s*$/.test(lines[i].trim()) &&
        !/^>\s?/.test(lines[i].trim()) &&
        !/^\s*[-*+]\s+/.test(lines[i]) &&
        !/^\s*\d+[.)]\s+/.test(lines[i])
      ) {
        paraLines.push(renderInlineMarkdown(lines[i].trim()));
        i++;
      }
      if (paraLines.length > 0) {
        htmlOut.push('<p>' + paraLines.join('<br>') + '</p>');
      }
    }

    return htmlOut.join('');
  }

  function updateGroundingSourcesPane(sources) {
    var listEl = document.getElementById('grounding-sources-list');
    var countEl = document.getElementById('sources-count-label');
    if (!listEl || !Array.isArray(sources) || sources.length === 0) return;
    if (countEl) {
      countEl.textContent = '· ' + sources.length;
    }
    listEl.innerHTML = sources
      .map(function (src, idx) {
        var title = src.title || src.uri || 'Source ' + (idx + 1);
        var uri = src.uri || '';
        return (
          '<div class="card-nested" style="background: var(--md-sys-color-surface-container);">' +
          '  <div class="row-between">' +
          '    <strong>' +
          escapeHtml(title) +
          '</strong>' +
          '    <span class="chat-turn-badge">Grounded</span>' +
          '  </div>' +
          (uri
            ? '<a class="meta" href="' +
              escapeHtml(uri) +
              '" target="_blank" rel="noopener noreferrer" style="color: var(--md-sys-color-primary); text-decoration: underline;">' +
              escapeHtml(uri) +
              '</a>'
            : '') +
          '</div>'
        );
      })
      .join('');
  }

  function loadRemoteSessionHistory(sessionId) {
    fetch('/api/sessions/' + encodeURIComponent(sessionId))
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (!data || !chatStream) return;
        var titleEl = document.getElementById('active-session-title');
        if (titleEl && data.title) {
          titleEl.innerHTML =
            escapeHtml(data.title) + ' <span class="header-count">· Synced</span>';
        }
        var events = data.events || data.messages || [];
        if (Array.isArray(events) && events.length > 0) {
          chatStream.innerHTML = events
            .map(function (ev) {
              var isUser = ev.author === 'user' || ev.role === 'user';
              if (isUser) {
                return (
                  '<article class="chat-msg user"><div class="chat-turn-header"><strong class="chat-msg-author">You</strong></div><p style="margin:0;">' +
                  escapeHtml(ev.text || '') +
                  '</p></article>'
                );
              }
              return (
                '<article class="chat-msg assistant">' +
                '  <div class="chat-turn-header">' +
                '    <div class="chat-turn-identity">' +
                '      <span class="material-symbols-outlined">auto_awesome</span>' +
                '      <strong class="chat-msg-author">' +
                escapeHtml(ev.author || 'ADK Personal Agent') +
                '</strong>' +
                '      <span class="chat-turn-badge">Completed</span>' +
                '    </div>' +
                '  </div>' +
                '  <div class="turn-prose md-prose">' +
                renderMarkdown(ev.text || '') +
                '  </div>' +
                '  <div class="chat-inline-actions">' +
                '    <div class="chat-action-group"></div>' +
                '    <div class="chat-action-group">' +
                '      <button type="button" class="chat-action-btn" data-chat-action="feedback-up" aria-label="Helpful response"><span class="material-symbols-outlined">thumb_up</span></button>' +
                '      <button type="button" class="chat-action-btn" data-chat-action="copy" aria-label="Copy response"><span class="material-symbols-outlined">content_copy</span><span>Copy</span></button>' +
                '    </div>' +
                '  </div>' +
                '</article>'
              );
            })
            .join('');
          scrollChatToBottom();
        }
      })
      .catch(function () {});
  }

  var composerForm = document.getElementById('chat-composer-form');
  var chatInput = document.getElementById('chat-input');

  if (chatInput) {
    chatInput.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !ev.shiftKey) {
        ev.preventDefault();
        if (composerForm) {
          composerForm.dispatchEvent(new Event('submit', { cancelable: true }));
        }
      }
    });
  }

  if (composerForm && chatInput && chatStream) {
    composerForm.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var promptText = chatInput.value.trim();
      if (!promptText) return;
      chatInput.value = '';

      var emptyPlaceholder = document.getElementById('chat-empty-state');
      if (emptyPlaceholder && emptyPlaceholder.parentNode) {
        emptyPlaceholder.parentNode.removeChild(emptyPlaceholder);
      }

      var userTurn = document.createElement('article');
      userTurn.className = 'chat-msg user';
      userTurn.innerHTML =
        '<div class="chat-turn-header"><strong class="chat-msg-author">You (Web UI)</strong><span class="chat-msg-time">Just now</span></div><p style="margin:0;">' +
        escapeHtml(promptText) +
        '</p>';
      chatStream.appendChild(userTurn);

      var modelSelect = document.getElementById('model-select');
      var selectedModel = modelSelect ? modelSelect.value : 'gemini-4-argon';
      var isDeepResearch = currentMode === 'deep_research';

      var assistantTurn = document.createElement('article');
      assistantTurn.className = 'chat-msg assistant';
      assistantTurn.innerHTML = [
        '<div class="chat-turn-header">',
        '  <div class="chat-turn-identity">',
        '    <span class="material-symbols-outlined">auto_awesome</span>',
        '    <strong class="chat-msg-author">' +
          (isDeepResearch ? 'ADK Deep Research' : 'ADK Personal Agent') +
          '</strong>',
        '    <span class="chat-turn-badge turn-status-badge">Streaming</span>',
        '  </div>',
        '  <span class="chat-msg-time">' + escapeHtml(selectedModel) + '</span>',
        '</div>',
        '<div class="turn-traces"></div>',
        '<div class="turn-prose md-prose"><p class="meta" style="margin:0;">Running ADK agent...</p></div>',
        '<div class="chat-inline-actions" hidden>',
        '  <div class="chat-action-group"></div>',
        '  <div class="chat-action-group">',
        '    <button type="button" class="chat-action-btn" data-chat-action="feedback-up" aria-label="Helpful response"><span class="material-symbols-outlined">thumb_up</span></button>',
        '    <button type="button" class="chat-action-btn" data-chat-action="copy" aria-label="Copy response"><span class="material-symbols-outlined">content_copy</span><span>Copy</span></button>',
        '  </div>',
        '</div>'
      ].join('');
      chatStream.appendChild(assistantTurn);
      scrollChatToBottom();

      var tracesContainer = assistantTurn.querySelector('.turn-traces');
      var proseEl = assistantTurn.querySelector('.turn-prose');
      var statusBadge = assistantTurn.querySelector('.turn-status-badge');
      var actionsBar = assistantTurn.querySelector('.chat-inline-actions');
      var finalSegments = [];
      var partialBuffer = '';
      var collectedSources = [];

      fetch('/api/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: currentSessionId,
          message: promptText,
          model: selectedModel,
          deep_research: isDeepResearch
        })
      })
        .then(function (resp) {
          if (!resp.ok) {
            return resp.json().then(function (err) {
              throw new Error(err.detail || err.error || 'Request failed');
            });
          }
          var reader = resp.body.getReader();
          var decoder = new TextDecoder();
          var buffer = '';

          function pump() {
            return reader.read().then(function (result) {
              if (result.done) {
                if (statusBadge && statusBadge.textContent === 'Streaming') {
                  statusBadge.textContent = 'Completed';
                }
                if (actionsBar) {
                  actionsBar.removeAttribute('hidden');
                }
                scrollChatToBottom();
                loadSessionsList();
                return;
              }
              buffer += decoder.decode(result.value, { stream: true });
              var parts = buffer.split('\n\n');
              buffer = parts.pop() || '';
              parts.forEach(function (frame) {
                var line = frame.trim();
                if (!line.startsWith('data:')) return;
                try {
                  var payload = JSON.parse(line.slice(5).trim());
                  if (payload.error || payload.type === 'error') {
                    var errText =
                      payload.error || payload.message || 'Error during execution.';
                    proseEl.innerHTML = renderMarkdown('**Error:** ' + errText);
                    if (statusBadge) {
                      statusBadge.textContent = 'Error';
                    }
                  }
                  if (payload.text && payload.type !== 'error') {
                    if (payload.partial) {
                      partialBuffer += payload.text;
                    } else {
                      finalSegments.push(payload.text);
                      partialBuffer = '';
                    }
                    var combinedMd = finalSegments
                      .concat(partialBuffer ? [partialBuffer] : [])
                      .join('\n\n');
                    proseEl.innerHTML = renderMarkdown(combinedMd);
                  }
                  if (
                    Array.isArray(payload.grounding_sources) &&
                    payload.grounding_sources.length > 0
                  ) {
                    payload.grounding_sources.forEach(function (s) {
                      collectedSources.push(s);
                    });
                    updateGroundingSourcesPane(collectedSources);
                  }
                  if (
                    Array.isArray(payload.tool_calls) &&
                    payload.tool_calls.length > 0 &&
                    tracesContainer
                  ) {
                    payload.tool_calls.forEach(function (tc) {
                      var box = document.createElement('div');
                      box.className = 'step-trace-box';
                      box.innerHTML =
                        '<button type="button" class="step-trace-header" aria-expanded="false" data-trace-toggle="true"><span class="row-inline"><span class="material-symbols-outlined">build</span><span>Tool call: <strong>' +
                        escapeHtml(tc.name) +
                        '</strong></span></span><span class="row-inline"><span class="status-dot-inline"><span class="status-dot green"></span>Executed</span><span class="material-symbols-outlined">expand_more</span></span></button><pre class="step-trace-body" hidden>' +
                        escapeHtml(JSON.stringify(tc.args || {}, null, 2)) +
                        '</pre>';
                      tracesContainer.appendChild(box);
                    });
                  }
                } catch (_) {}
              });
              scrollChatToBottom();
              return pump();
            });
          }
          return pump();
        })
        .catch(function (err) {
          if (proseEl) {
            proseEl.innerHTML = renderMarkdown(
              '**Error:** ' + (err.message || 'Unable to stream response.')
            );
          }
          if (statusBadge) {
            statusBadge.textContent = 'Error';
          }
          scrollChatToBottom();
        });
    });
  }

  // ---------------------------------------------------------------------------
  // 7. Live Configuration Status Sync, Model Selector Sync & Button Handlers
  // ---------------------------------------------------------------------------
  function ensureModelOption(selectEl, modelId) {
    if (!selectEl || !modelId) return;
    var clean = String(modelId).replace(/^models\//, '').trim();
    if (!clean) return;
    var exists = Array.prototype.some.call(selectEl.options, function (opt) {
      return opt.value === clean;
    });
    if (!exists) {
      var opt = document.createElement('option');
      opt.value = clean;
      opt.textContent = clean;
      selectEl.appendChild(opt);
    }
  }

  function syncModelSelects(activeModel, discoveredModels) {
    var chatModelSel = document.getElementById('model-select');
    var defaultModelSel = document.getElementById('default-model-select');
    if (Array.isArray(discoveredModels)) {
      discoveredModels.forEach(function (m) {
        ensureModelOption(chatModelSel, m);
        ensureModelOption(defaultModelSel, m);
      });
    }
    if (activeModel) {
      ensureModelOption(chatModelSel, activeModel);
      ensureModelOption(defaultModelSel, activeModel);
      if (chatModelSel) chatModelSel.value = activeModel;
      if (defaultModelSel) defaultModelSel.value = activeModel;
    }
  }

  var chatModelSelect = document.getElementById('model-select');
  var defaultModelSelect = document.getElementById('default-model-select');
  if (chatModelSelect) {
    chatModelSelect.addEventListener('change', function () {
      var chosen = chatModelSelect.value;
      if (defaultModelSelect) {
        ensureModelOption(defaultModelSelect, chosen);
        defaultModelSelect.value = chosen;
      }
      fetch('/api/config/model', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ default_model: chosen })
      }).catch(function () {});
    });
  }
  if (defaultModelSelect) {
    defaultModelSelect.addEventListener('change', function () {
      var chosen = defaultModelSelect.value;
      if (chatModelSelect) {
        ensureModelOption(chatModelSelect, chosen);
        chatModelSelect.value = chosen;
      }
      fetch('/api/config/model', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ default_model: chosen })
      }).catch(function () {});
    });
  }

  function syncConfigStatus() {
    fetch('/api/config/status')
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (cfg) {
        if (!cfg) return;
        var keyBadge = document.getElementById('gemini-key-badge');
        if (keyBadge) {
          keyBadge.textContent = cfg.gemini_key_configured
            ? 'Active · AI Studio'
            : 'Key required';
        }
        var keyInput = document.getElementById('gemini-key-input');
        if (keyInput && cfg.gemini_key_masked) {
          keyInput.placeholder = cfg.gemini_key_masked;
        }
        if (cfg.default_model) {
          syncModelSelects(cfg.default_model, cfg.available_models || []);
        }
        var wsBadge = document.getElementById('workspace-status-badge');
        if (wsBadge) {
          wsBadge.textContent = cfg.workspace_token_present
            ? 'Authorized'
            : 'Configure OAuth';
        }
        var projInput = document.getElementById('gchat-project-input');
        if (projInput && cfg.gchat_project_id) {
          projInput.value = cfg.gchat_project_id;
        }
        var subInput = document.getElementById('gchat-sub-input');
        if (subInput && cfg.gchat_subscription_id) {
          subInput.value = cfg.gchat_subscription_id;
        }
      })
      .catch(function () {});
  }

  syncConfigStatus();

  var saveGeminiBtn = document.getElementById('save-gemini-key-btn');
  if (saveGeminiBtn) {
    saveGeminiBtn.addEventListener('click', function () {
      var keyInput = document.getElementById('gemini-key-input');
      var modelSel = document.getElementById('default-model-select');
      var customModelInput = document.getElementById('custom-model-input');
      var statusSpan = document.getElementById('gemini-test-status');
      var apiKey = keyInput ? keyInput.value.trim() : '';
      var customModel = customModelInput ? customModelInput.value.trim() : '';
      var chosenModel = customModel || (modelSel ? modelSel.value : 'gemini-4-argon');
      var hasMaskedKey = keyInput && keyInput.placeholder && keyInput.placeholder.indexOf('*') !== -1;
      if (!apiKey && !hasMaskedKey) {
        // Still allow saving the default model even before an API key is entered
        fetch('/api/config/model', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ default_model: chosenModel })
        })
          .then(function () {
            syncModelSelects(chosenModel, []);
            if (statusSpan) {
              statusSpan.innerHTML =
                '<span class="status-dot green"></span><span>Saved default model: ' +
                escapeHtml(chosenModel) +
                '</span>';
            }
          })
          .catch(function () {});
        return;
      }
      if (statusSpan) {
        statusSpan.innerHTML =
          '<span class="status-dot blue"></span><span>Verifying with AI Studio...</span>';
      }
      fetch('/api/config/gemini-key', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          api_key: apiKey,
          default_model: chosenModel
        })
      })
        .then(function (r) {
          return r.json().then(function (d) {
            if (!r.ok) throw new Error(d.detail || 'Verification failed');
            return d;
          });
        })
        .then(function (res) {
          syncModelSelects(chosenModel, res.models_sample || []);
          if (statusSpan) {
            statusSpan.innerHTML =
              '<span class="status-dot green"></span><span>Verified · ' +
              escapeHtml(chosenModel) +
              ' · ' +
              (res.latency_ms || 0) +
              ' ms</span>';
          }
          syncConfigStatus();
        })
        .catch(function (err) {
          if (statusSpan) {
            statusSpan.innerHTML =
              '<span class="status-dot red"></span><span>' +
              escapeHtml(err.message) +
              '</span>';
          }
        });
    });
  }

  var saveWorkspaceBtn = document.getElementById('save-workspace-btn');
  if (saveWorkspaceBtn) {
    saveWorkspaceBtn.addEventListener('click', function () {
      var clientJson = document.getElementById('workspace-client-json');
      var safetySel = document.getElementById('email-safety-select');
      fetch('/api/config/workspace', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          client_secret_json:
            clientJson && clientJson.value.trim()
              ? clientJson.value.trim()
              : null,
          email_safety_mode: safetySel ? safetySel.value : 'draft_only'
        })
      }).then(function () {
        syncConfigStatus();
      });
    });
  }

  var authWorkspaceBtn = document.getElementById('authorize-workspace-btn');
  if (authWorkspaceBtn) {
    authWorkspaceBtn.addEventListener('click', function () {
      fetch('/api/auth/workspace/start')
        .then(function (r) {
          return r.json();
        })
        .then(function (data) {
          var url = data.auth_url || data.authorization_url;
          if (url) window.location.href = url;
        })
        .catch(function () {});
    });
  }

  var saveGchatBtn = document.getElementById('save-gchat-btn');
  if (saveGchatBtn) {
    saveGchatBtn.addEventListener('click', function () {
      var projInput = document.getElementById('gchat-project-input');
      var subInput = document.getElementById('gchat-sub-input');
      var saInput = document.getElementById('gchat-sa-json');
      var allowInput = document.getElementById('gchat-allowlist-input');
      var emails =
        allowInput && allowInput.value.trim() ? [allowInput.value.trim()] : [];
      fetch('/api/config/gchat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          enabled: true,
          transport_mode: 'pubsub',
          project_id: projInput ? projInput.value.trim() : '',
          subscription_id: subInput
            ? subInput.value.trim()
            : 'adk-chat-inbound-sub',
          allowed_emails: emails,
          service_account_json:
            saInput && saInput.value.trim() ? saInput.value.trim() : null
        })
      }).then(function () {
        syncConfigStatus();
      });
    });
  }

  var testBrowserBtn = document.getElementById('test-browser-btn');
  if (testBrowserBtn) {
    testBrowserBtn.addEventListener('click', function () {
      var outEl = document.getElementById('browser-test-output');
      if (outEl) {
        outEl.innerHTML =
          '<span class="status-dot blue"></span><span>Launching Headless Chromium...</span>';
      }
      fetch('/api/browser/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: 'https://example.com' })
      })
        .then(function (r) {
          return r.json();
        })
        .then(function (res) {
          if (outEl) {
            outEl.innerHTML =
              '<span class="status-dot green"></span><span>Verified: ' +
              escapeHtml(res.page_title || 'OK') +
              ' · ' +
              (res.elapsed_ms || 0) +
              ' ms</span>';
          }
          var shotImg = document.getElementById('latest-browser-shot');
          if (shotImg && res.screenshot_url) {
            shotImg.src = res.screenshot_url;
            shotImg.removeAttribute('hidden');
          }
        })
        .catch(function () {});
    });
  }

  var clearBrowserBtn = document.getElementById('clear-browser-btn');
  if (clearBrowserBtn) {
    clearBrowserBtn.addEventListener('click', function () {
      fetch('/api/browser/clear-profile', { method: 'POST' }).catch(
        function () {}
      );
    });
  }

  var savePinBtn = document.getElementById('save-pin-btn');
  if (savePinBtn) {
    savePinBtn.addEventListener('click', function () {
      var pinInput = document.getElementById('webui-pin-input');
      fetch('/api/config/security', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pin: pinInput ? pinInput.value.trim() : '' })
      }).then(function () {
        syncConfigStatus();
      });
    });
  }

  // ---------------------------------------------------------------------------
  // 8. Scheduled Tasks & Recurring Web Scraping Console (/api/schedules/*)
  // ---------------------------------------------------------------------------
  function formatTimestamp(ts) {
    if (!ts) return 'Never';
    try {
      return new Date(Number(ts) * 1000).toLocaleString();
    } catch (_) {
      return '—';
    }
  }

  function loadSchedulesList() {
    var listEl = document.getElementById('schedules-list');
    var countEl = document.getElementById('schedules-count-label');
    if (!listEl) return;

    fetch('/api/schedules')
      .then(function (r) {
        return r.ok ? r.json() : { schedules: [] };
      })
      .then(function (data) {
        var items = data && Array.isArray(data.schedules) ? data.schedules : [];
        if (countEl) {
          countEl.textContent = '· ' + items.length;
        }
        if (items.length === 0) {
          listEl.innerHTML =
            '<p class="meta" style="margin: 0;">No scheduled tasks yet. Create one on the left or ask the agent in Chat (e.g. &ldquo;Schedule a daily 8am scrape of https://...&rdquo;).</p>';
          return;
        }
        listEl.innerHTML = items
          .map(function (t) {
            var isEnabled = t.enabled !== false;
            var statusColor =
              t.last_status === 'error'
                ? 'red'
                : t.last_status === 'running'
                  ? 'blue'
                  : isEnabled
                    ? 'green'
                    : 'amber';
            var statusLabel =
              t.last_status === 'running'
                ? 'Running now...'
                : isEnabled
                  ? 'Active · Next: ' + formatTimestamp(t.next_run_at)
                  : 'Paused';
            var lastOutputMd = (t.last_output || '').trim();
            return (
              '<div class="card-nested" style="background: var(--md-sys-color-surface-container); gap: 10px;">' +
              '  <div class="row-between" style="flex-wrap: wrap;">' +
              '    <div>' +
              '      <strong style="font-size: 14px;">' +
              escapeHtml(t.name) +
              '</strong>' +
              '      <div class="meta">Cron: <code>' +
              escapeHtml(t.schedule) +
              '</code> · Mode: <code>' +
              escapeHtml(t.mode || 'scrape') +
              '</code> · Runs: ' +
              (t.run_count || 0) +
              '</div>' +
              '    </div>' +
              '    <span class="status-dot-inline"><span class="status-dot ' +
              statusColor +
              '"></span><span>' +
              escapeHtml(statusLabel) +
              '</span></span>' +
              '  </div>' +
              (t.target_url
                ? '<div class="meta">Target URL: <a href="' +
                  escapeHtml(t.target_url) +
                  '" target="_blank" rel="noopener noreferrer" style="color: var(--md-sys-color-primary);">' +
                  escapeHtml(t.target_url) +
                  '</a></div>'
                : '') +
              (t.prompt
                ? '<div class="meta">' + escapeHtml(t.prompt) + '</div>'
                : '') +
              (lastOutputMd
                ? '<div class="step-trace-box">' +
                  '  <button type="button" class="step-trace-header" aria-expanded="false" data-trace-toggle="true">' +
                  '    <span class="row-inline"><span class="material-symbols-outlined">description</span><span>Latest scrape result (' +
                  escapeHtml(formatTimestamp(t.last_run_at)) +
                  ')</span></span>' +
                  '    <span class="material-symbols-outlined">expand_more</span>' +
                  '  </button>' +
                  '  <div class="step-trace-body md-prose" hidden>' +
                  renderMarkdown(lastOutputMd) +
                  '  </div>' +
                  '</div>'
                : '') +
              '  <div class="chat-inline-actions">' +
              '    <div class="chat-action-group">' +
              '      <button type="button" class="chat-action-btn primary-tonal" data-sched-run="' +
              escapeHtml(t.id) +
              '"><span class="material-symbols-outlined">play_arrow</span><span>Run now</span></button>' +
              '      <button type="button" class="chat-action-btn" data-sched-open="' +
              escapeHtml(t.session_id || 'sched_' + t.id) +
              '" data-sched-title="' +
              escapeHtml(t.name) +
              '"><span class="material-symbols-outlined">open_in_new</span><span>Open in Chat</span></button>' +
              '    </div>' +
              '    <div class="chat-action-group">' +
              '      <button type="button" class="chat-action-btn" data-sched-toggle="' +
              escapeHtml(t.id) +
              '"><span>' +
              (isEnabled ? 'Pause' : 'Resume') +
              '</span></button>' +
              '      <button type="button" class="chat-action-btn" data-sched-delete="' +
              escapeHtml(t.id) +
              '"><span class="material-symbols-outlined">delete</span></button>' +
              '    </div>' +
              '  </div>' +
              '</div>'
            );
          })
          .join('');
      })
      .catch(function () {});
  }

  loadSchedulesList();

  var refreshSchedulesBtn = document.getElementById('refresh-schedules-btn');
  if (refreshSchedulesBtn) {
    refreshSchedulesBtn.addEventListener('click', loadSchedulesList);
  }

  var createScheduleBtn = document.getElementById('create-schedule-btn');
  if (createScheduleBtn) {
    createScheduleBtn.addEventListener('click', function () {
      var nameIn = document.getElementById('sched-name-input');
      var urlIn = document.getElementById('sched-url-input');
      var cronSel = document.getElementById('sched-cron-select');
      var customCronIn = document.getElementById('sched-custom-cron-input');
      var modeSel = document.getElementById('sched-mode-select');
      var diffChk = document.getElementById('sched-diff-checkbox');
      var promptIn = document.getElementById('sched-prompt-input');
      var statusEl = document.getElementById('sched-create-status');

      var name = nameIn ? nameIn.value.trim() : '';
      var targetUrl = urlIn ? urlIn.value.trim() : '';
      var scheduleExpr =
        customCronIn && customCronIn.value.trim()
          ? customCronIn.value.trim()
          : cronSel
            ? cronSel.value
            : '0 8 * * *';
      var mode = modeSel ? modeSel.value : 'scrape';
      var onlyOnChange = diffChk ? Boolean(diffChk.checked) : false;
      var promptText = promptIn ? promptIn.value.trim() : '';

      if (!name) {
        if (statusEl) {
          statusEl.innerHTML =
            '<span class="status-dot red"></span><span>Please enter a task name.</span>';
        }
        return;
      }
      if (!promptText && !targetUrl) {
        if (statusEl) {
          statusEl.innerHTML =
            '<span class="status-dot red"></span><span>Enter a Target URL or scraping instructions.</span>';
        }
        return;
      }

      fetch('/api/schedules', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name,
          target_url: targetUrl,
          schedule: scheduleExpr,
          mode: mode,
          only_on_change: onlyOnChange,
          prompt:
            promptText ||
            'Extract the main content, key data points, and tables into clean Markdown.'
        })
      })
        .then(function (r) {
          return r.json().then(function (d) {
            if (!r.ok) throw new Error(d.detail || 'Failed to create schedule');
            return d;
          });
        })
        .then(function () {
          if (nameIn) nameIn.value = '';
          if (urlIn) urlIn.value = '';
          if (customCronIn) customCronIn.value = '';
          if (promptIn) promptIn.value = '';
          if (statusEl) {
            statusEl.innerHTML =
              '<span class="status-dot green"></span><span>Scheduled! Saved to <code>/data/schedules.json</code></span>';
          }
          loadSchedulesList();
        })
        .catch(function (err) {
          if (statusEl) {
            statusEl.innerHTML =
              '<span class="status-dot red"></span><span>' +
              escapeHtml(err.message) +
              '</span>';
          }
        });
    });
  }

  document.addEventListener('click', function (ev) {
    var runBtn = ev.target.closest('[data-sched-run]');
    if (runBtn) {
      var taskId = runBtn.getAttribute('data-sched-run');
      if (!taskId) return;
      runBtn.disabled = true;
      var labelSpan = runBtn.querySelector('span:last-child');
      if (labelSpan) labelSpan.textContent = 'Running...';
      fetch('/api/schedules/' + encodeURIComponent(taskId) + '/run', {
        method: 'POST'
      })
        .then(function () {
          loadSchedulesList();
          loadSessionsList();
        })
        .catch(function () {
          loadSchedulesList();
        });
      return;
    }

    var openBtn = ev.target.closest('[data-sched-open]');
    if (openBtn) {
      var sessId = openBtn.getAttribute('data-sched-open');
      var title = openBtn.getAttribute('data-sched-title') || 'Scheduled Task';
      if (!sessId) return;
      currentSessionId = sessId;
      switchView('workbench');
      var titleEl = document.getElementById('active-session-title');
      if (titleEl) {
        titleEl.innerHTML =
          escapeHtml(title) + ' <span class="header-count">· Scheduled</span>';
      }
      loadRemoteSessionHistory(sessId);
      return;
    }

    var toggleBtn = ev.target.closest('[data-sched-toggle]');
    if (toggleBtn) {
      var toggleId = toggleBtn.getAttribute('data-sched-toggle');
      if (!toggleId) return;
      fetch('/api/schedules/' + encodeURIComponent(toggleId) + '/toggle', {
        method: 'POST'
      }).then(function () {
        loadSchedulesList();
      });
      return;
    }

    var delBtn = ev.target.closest('[data-sched-delete]');
    if (delBtn) {
      var delId = delBtn.getAttribute('data-sched-delete');
      if (!delId) return;
      fetch('/api/schedules/' + encodeURIComponent(delId), {
        method: 'DELETE'
      }).then(function () {
        loadSchedulesList();
      });
    }
  });
})();
