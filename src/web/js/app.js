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
  // 3. View Switching (#view-workbench <-> #view-settings)
  // ---------------------------------------------------------------------------
  var viewWorkbench = document.getElementById('view-workbench');
  var viewSettings = document.getElementById('view-settings');
  var navItems = Array.prototype.slice.call(
    document.querySelectorAll('.nav-item[data-view]')
  );

  function switchView(targetView) {
    var normalized = targetView === 'settings' ? 'settings' : 'workbench';
    if (viewWorkbench && viewSettings) {
      if (normalized === 'settings') {
        viewWorkbench.hidden = true;
        viewWorkbench.classList.remove('active');
        viewSettings.hidden = false;
        viewSettings.classList.add('active');
      } else {
        viewSettings.hidden = true;
        viewSettings.classList.remove('active');
        viewWorkbench.hidden = false;
        viewWorkbench.classList.add('active');
        scrollChatToBottom();
      }
    }
    navItems.forEach(function (btn) {
      var isMatch = btn.getAttribute('data-view') === normalized;
      btn.classList.toggle('active', isMatch);
    });
    closeMobileDrawer();
    refreshAllScrollFades();
  }

  navItems.forEach(function (btn) {
    btn.addEventListener('click', function () {
      var target = btn.getAttribute('data-view') || 'workbench';
      switchView(target);
    });
  });

  function syncViewFromLocation() {
    var hash = (window.location.hash || '').replace(/^#/, '').toLowerCase();
    var params = new URLSearchParams(window.location.search);
    var queryView = (params.get('view') || params.get('settings') || '').toLowerCase();
    if (hash === 'settings' || queryView === 'settings' || queryView === 'workspace') {
      switchView('settings');
    } else if (hash === 'workbench') {
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
  // 5. Interactive Session Switching, Trace Accordions & Morphing Tabs
  // ---------------------------------------------------------------------------
  var currentSessionId = 'sess_mobile_gchat_01';
  var currentMode = 'standard';

  var SEED_CONVERSATIONS = {
    sess_mobile_gchat_01: {
      title: 'Tokyo flight itinerary & hotel research',
      subtitle: 'Shared SQLite session · Started on mobile Google Chat',
      html: chatStream ? chatStream.innerHTML : ''
    },
    sess_webui_02: {
      title: 'Home server UPS battery comparison',
      subtitle: 'Deep Research session · 4 web sources synthesized',
      html: [
        '<article class="chat-msg user">',
        '  <div class="chat-turn-header"><strong class="chat-msg-author">You (Web UI)</strong><span class="chat-msg-time">1h ago</span></div>',
        '  <p style="margin:0;">Compare pure sine-wave 1500VA UPS units for my Linux container host with USB NUT support and LiFePO4 battery longevity.</p>',
        '</article>',
        '<article class="chat-msg assistant">',
        '  <div class="chat-turn-header">',
        '    <div class="chat-turn-identity"><span class="material-symbols-outlined">auto_awesome</span><strong class="chat-msg-author">ADK Deep Research</strong><span class="chat-turn-badge">Verified · Grounded</span></div>',
        '    <span class="chat-msg-time">gemini-2.5-pro · 1h ago</span>',
        '  </div>',
        '  <div class="step-trace-box">',
        '    <button type="button" class="step-trace-header" aria-expanded="false" data-trace-toggle="true">',
        '      <span class="row-inline"><span class="material-symbols-outlined">travel_explore</span><span>Deep Research Loop: <strong>deep_research_workflow</strong></span></span>',
        '      <span class="row-inline"><span class="status-dot-inline"><span class="status-dot green"></span>Completed · 3 iterations</span><span class="material-symbols-outlined">expand_more</span></span>',
        '    </button>',
        '    <pre class="step-trace-body" hidden>{"iterations": 3, "tools": ["google_search_agent", "browse_webpage"], "nut_driver": "usbhid-ups"}</pre>',
        '  </div>',
        '  <p style="margin:0;">For a 24/7 Linux server running Docker and Network UPS Tools (<code>usbhid-ups</code>), LiFePO4 rack/tower units offer 2,000–3,000 charge cycles (8–10 years) compared to 3–4 years for sealed lead-acid batteries.</p>',
        '</article>'
      ].join('\n')
    },
    sess_mobile_gchat_03: {
      title: 'Summarize unread invoices in Gmail',
      subtitle: 'Shared SQLite session · Started on mobile Google Chat',
      html: [
        '<article class="chat-msg user">',
        '  <div class="chat-turn-header"><strong class="chat-msg-author">You (via mobile Google Chat)</strong><span class="chat-msg-time">Yesterday</span></div>',
        '  <p style="margin:0;">Search my Gmail for unread cloud and domain renewal invoices from the last 14 days.</p>',
        '</article>',
        '<article class="chat-msg assistant">',
        '  <div class="chat-turn-header">',
        '    <div class="chat-turn-identity"><span class="material-symbols-outlined">auto_awesome</span><strong class="chat-msg-author">ADK Personal Agent</strong><span class="chat-turn-badge">Verified · Grounded</span></div>',
        '    <span class="chat-msg-time">gemini-2.5-flash · Yesterday</span>',
        '  </div>',
        '  <p style="margin:0;">I found 2 unread billing notices in your personal Workspace inbox: Cloudflare Domain Renewal ($10.44) and Google Workspace Business Starter ($7.20).</p>',
        '</article>'
      ].join('\n')
    }
  };

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
        var titleEl = document.getElementById('active-session-title');
        var subEl = document.getElementById('active-session-subtitle');
        if (SEED_CONVERSATIONS[sessId]) {
          if (titleEl) {
            titleEl.innerHTML =
              SEED_CONVERSATIONS[sessId].title +
              ' <span class="header-count">· Synced</span>';
          }
          if (subEl) {
            subEl.textContent = SEED_CONVERSATIONS[sessId].subtitle;
          }
          if (chatStream) {
            chatStream.innerHTML = SEED_CONVERSATIONS[sessId].html;
            scrollChatToBottom();
          }
        } else {
          loadRemoteSessionHistory(sessId);
        }
      };
    });
  }

  bindSessionItems();

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
      if (chatStream) {
        chatStream.innerHTML = '';
        updateScrollFade(chatStream);
      }
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
        var text = turn ? turn.innerText : '';
        if (navigator.clipboard && text) {
          navigator.clipboard.writeText(text).catch(function () {});
        }
        actionBtn.classList.add('is-active');
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
  // 6. Live SSE Chat Streaming & Configuration Endpoints
  // ---------------------------------------------------------------------------
  function escapeHtml(str) {
    return String(str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
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
        if (Array.isArray(data.events) && data.events.length > 0) {
          chatStream.innerHTML = data.events
            .map(function (ev) {
              var isUser = ev.author === 'user';
              return (
                '<article class="chat-msg ' +
                (isUser ? 'user' : 'assistant') +
                '"><div class="chat-turn-header"><strong class="chat-msg-author">' +
                escapeHtml(isUser ? 'You' : ev.author || 'ADK Personal Agent') +
                '</strong></div><p style="margin:0;">' +
                escapeHtml(ev.text || '') +
                '</p></article>'
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

      var userTurn = document.createElement('article');
      userTurn.className = 'chat-msg user';
      userTurn.innerHTML =
        '<div class="chat-turn-header"><strong class="chat-msg-author">You (Web UI)</strong><span class="chat-msg-time">Just now</span></div><p style="margin:0;">' +
        escapeHtml(promptText) +
        '</p>';
      chatStream.appendChild(userTurn);

      var modelSelect = document.getElementById('model-select');
      var selectedModel = modelSelect ? modelSelect.value : 'gemini-2.5-pro';
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
        '    <span class="chat-turn-badge">Streaming</span>',
        '  </div>',
        '  <span class="chat-msg-time">' + escapeHtml(selectedModel) + '</span>',
        '</div>',
        '<div class="turn-traces"></div>',
        '<p class="turn-prose" style="margin:0;">Running ADK agent...</p>'
      ].join('');
      chatStream.appendChild(assistantTurn);
      scrollChatToBottom();

      var tracesContainer = assistantTurn.querySelector('.turn-traces');
      var proseEl = assistantTurn.querySelector('.turn-prose');
      var accumulatedText = '';

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
                scrollChatToBottom();
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
                    proseEl.textContent =
                      payload.error || payload.message || 'Error during execution.';
                  }
                  if (payload.text) {
                    accumulatedText += payload.text;
                    proseEl.textContent = accumulatedText;
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
            proseEl.textContent = err.message || 'Unable to stream response.';
          }
          scrollChatToBottom();
        });
    });
  }

  // ---------------------------------------------------------------------------
  // 7. Live Configuration Status Sync & Button Handlers
  // ---------------------------------------------------------------------------
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
      var statusSpan = document.getElementById('gemini-test-status');
      var apiKey = keyInput ? keyInput.value.trim() : '';
      var hasMaskedKey = keyInput && keyInput.placeholder && keyInput.placeholder.indexOf('*') !== -1;
      if (!apiKey && !hasMaskedKey) return;
      if (statusSpan) {
        statusSpan.innerHTML =
          '<span class="status-dot blue"></span><span>Verifying with AI Studio...</span>';
      }
      fetch('/api/config/gemini-key', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          api_key: apiKey,
          default_model: modelSel ? modelSel.value : 'gemini-2.5-pro'
        })
      })
        .then(function (r) {
          return r.json().then(function (d) {
            if (!r.ok) throw new Error(d.detail || 'Verification failed');
            return d;
          });
        })
        .then(function (res) {
          if (statusSpan) {
            statusSpan.innerHTML =
              '<span class="status-dot green"></span><span>Verified · ' +
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
})();
