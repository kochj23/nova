/* =====================================================================
   NOVA CONTROL — Chatroom Live Feed
   Connects to nova_chatroom.py WebSocket, displays messages in HUD style.
   Auto-reconnects. Shows last 15 messages, fades old ones.
   ===================================================================== */

(function () {
  'use strict';

  const CHATROOM_WS = 'ws://192.168.1.6:37480/ws';
  const MAX_VISIBLE = 15;
  const FADE_AFTER_MS = 60000; // fade messages older than 60s

  const container = document.getElementById('chatfeed-messages');
  if (!container) return;

  let ws = null;
  let reconnectTimer = null;
  let fadeInterval = null;

  function getSenderClass(sender) {
    const s = sender.toLowerCase();
    if (s === 'nova') return 'nova';
    if (s.includes('claude')) return 'claude';
    if (s === 'jordan') return 'jordan';
    return 'herd';
  }

  function formatTime(isoString) {
    const d = new Date(isoString);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function truncate(text, max) {
    if (text.length <= max) return text;
    return text.substring(0, max) + '…';
  }

  function appendMessage(msg) {
    const div = document.createElement('div');
    div.className = 'chatfeed-msg';
    div.dataset.ts = msg.timestamp || new Date().toISOString();

    const senderClass = getSenderClass(msg.sender);
    div.innerHTML =
      '<span class="chatfeed-sender ' + senderClass + '">' + msg.sender + '</span>' +
      '<span class="chatfeed-text">' + escapeHtml(truncate(msg.message, 200)) + '</span>' +
      '<span class="chatfeed-time">' + formatTime(msg.timestamp) + '</span>';

    container.appendChild(div);

    // Remove excess messages
    while (container.children.length > MAX_VISIBLE) {
      container.removeChild(container.firstChild);
    }
  }

  function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  function startFadeCheck() {
    if (fadeInterval) return;
    fadeInterval = setInterval(function () {
      const now = Date.now();
      const msgs = container.querySelectorAll('.chatfeed-msg');
      msgs.forEach(function (el) {
        const ts = new Date(el.dataset.ts).getTime();
        if (now - ts > FADE_AFTER_MS) {
          el.classList.add('fading');
        }
      });
    }, 10000);
  }

  function connect() {
    ws = new WebSocket(CHATROOM_WS);

    ws.onopen = function () {
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      startFadeCheck();
    };

    ws.onclose = function () {
      reconnectTimer = setTimeout(connect, 5000);
    };

    ws.onerror = function () {
      ws.close();
    };

    ws.onmessage = function (event) {
      try {
        var data = JSON.parse(event.data);
        if (data.type === 'history') {
          container.innerHTML = '';
          // Show last MAX_VISIBLE from history
          var msgs = data.messages.slice(-MAX_VISIBLE);
          msgs.forEach(appendMessage);
        } else if (data.type === 'message') {
          appendMessage(data);
        }
      } catch (e) {
        // ignore parse errors
      }
    };
  }

  connect();
})();
