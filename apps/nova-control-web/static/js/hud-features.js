/* =====================================================================
   NOVA CONTROL — HUD Visualization Features (10 new features)
   Extends the existing radial orbital HUD with rich data visualizations.
   Loads BEFORE hud.js. Hooks into hud.js via window.__hudFeaturesRender.
   ===================================================================== */

(function () {
  'use strict';

  // ---- Color palette (matching hud.js) ----
  var C = {
    cyan:    [0, 255, 200],
    green:   [0, 255, 102],
    amber:   [255, 204, 0],
    red:     [255, 51, 68],
    blue:    [0, 136, 255],
    magenta: [255, 0, 170],
    purple:  [180, 80, 255],
    gold:    [255, 215, 0],
    dim:     [40, 55, 80],
    white:   [200, 210, 230],
  };

  function rgba(c, a) { return 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + a + ')'; }

  var TWO_PI = Math.PI * 2;

  // ---- Dimensions (updated by render callback) ----
  var W = 1920, H = 1080;

  // ---- Domain color mapping ----
  var DOMAIN_COLORS = {
    tech: C.blue, technology: C.blue, programming: C.blue,
    music: C.red, audio: C.red,
    science: C.green, research: C.green,
    personal: C.gold, email_archive: C.gold, imessage: C.gold,
    livejournal: C.gold, journal: C.gold,
    entertainment: C.purple, youtube: C.red, tv_recording: C.amber,
    wikipedia: C.green, web: C.blue,
    slack: C.cyan, discord: C.purple,
    automotive: C.amber, cooking: C.green, news: C.white,
  };

  function domainColor(source) {
    if (!source) return C.cyan;
    var s = source.toLowerCase();
    for (var key in DOMAIN_COLORS) {
      if (s.indexOf(key) !== -1) return DOMAIN_COLORS[key];
    }
    var hash = 0;
    for (var i = 0; i < s.length; i++) hash = ((hash << 5) - hash) + s.charCodeAt(i);
    var hue = Math.abs(hash) % 360;
    return [
      Math.round(128 + 127 * Math.cos(hue * Math.PI / 180)),
      Math.round(128 + 127 * Math.cos((hue - 120) * Math.PI / 180)),
      Math.round(128 + 127 * Math.cos((hue - 240) * Math.PI / 180))
    ];
  }

  function escapeHtml(str) {
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // ================================================================
  //  FEATURE 1: Live Memory Ingest Visualization
  // ================================================================

  var ingestParticles = [];
  var ingestData = [];
  var MAX_INGEST_PARTICLES = 60;

  function IngestParticle(source) {
    var col = domainColor(source);
    this.source = source;
    this.col = col;
    var side = Math.floor(Math.random() * 4);
    if (side === 0) { this.sx = Math.random() * W; this.sy = -10; }
    else if (side === 1) { this.sx = W + 10; this.sy = Math.random() * H; }
    else if (side === 2) { this.sx = Math.random() * W; this.sy = H + 10; }
    else { this.sx = -10; this.sy = Math.random() * H; }
    this.tx = W * 0.5 + (Math.random() - 0.5) * W * 0.14;
    this.ty = H * 0.47 + (Math.random() - 0.5) * H * 0.1;
    this.t = 0;
    this.speed = 0.006 + Math.random() * 0.01;
    this.size = 1.8 + Math.random() * 2;
    this.alpha = 0.5 + Math.random() * 0.35;
    this.trail = [];
  }

  IngestParticle.prototype.update = function () {
    this.t += this.speed;
    var eased = 1 - Math.pow(1 - this.t, 3);
    this.cx = this.sx + (this.tx - this.sx) * eased;
    this.cy = this.sy + (this.ty - this.sy) * eased;
    this.trail.push({ x: this.cx, y: this.cy });
    if (this.trail.length > 6) this.trail.shift();
    return this.t < 1.0;
  };

  IngestParticle.prototype.draw = function (ctx) {
    var fade = this.t < 0.8 ? 1 : (1 - this.t) / 0.2;
    for (var i = 0; i < this.trail.length; i++) {
      var tp = this.trail[i];
      var ta = (i / this.trail.length) * 0.25 * fade;
      ctx.beginPath();
      ctx.arc(tp.x, tp.y, this.size * 0.4, 0, TWO_PI);
      ctx.fillStyle = rgba(this.col, ta * this.alpha);
      ctx.fill();
    }
    ctx.beginPath();
    ctx.arc(this.cx, this.cy, this.size, 0, TWO_PI);
    ctx.fillStyle = rgba(this.col, this.alpha * fade);
    ctx.fill();
    ctx.beginPath();
    ctx.arc(this.cx, this.cy, this.size * 2.5, 0, TWO_PI);
    ctx.fillStyle = rgba(this.col, 0.05 * fade);
    ctx.fill();
  };

  function pollIngestActivity() {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/ingest-activity', true);
    xhr.timeout = 5000;
    xhr.onload = function () {
      if (xhr.status === 200) {
        try { ingestData = JSON.parse(xhr.responseText); } catch (e) {}
      }
    };
    xhr.send();
  }

  function spawnIngestParticles() {
    if (ingestParticles.length >= MAX_INGEST_PARTICLES) return;
    for (var i = 0; i < ingestData.length; i++) {
      var item = ingestData[i];
      var rate = Math.min(0.08, item.count / 120);
      if (Math.random() < rate) {
        ingestParticles.push(new IngestParticle(item.source));
      }
    }
  }

  function renderIngestParticles(ctx) {
    spawnIngestParticles();
    ingestParticles = ingestParticles.filter(function (p) {
      var alive = p.update();
      if (alive) p.draw(ctx);
      return alive;
    });
  }

  // ================================================================
  //  FEATURE 2: Cross-Vector Correlation Explorer
  // ================================================================

  var correlationData = null;
  var correlationStart = 0;
  var CORRELATION_DURATION = 10000;

  function pollCorrelation() {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/random-correlation', true);
    xhr.timeout = 8000;
    xhr.onload = function () {
      if (xhr.status === 200) {
        try {
          var data = JSON.parse(xhr.responseText);
          if (data.source_a && data.source_b && data.text_a) {
            correlationData = data;
            correlationStart = performance.now();
          }
        } catch (e) {}
      }
    };
    xhr.send();
  }

  function renderCorrelation(ctx, ts) {
    if (!correlationData || !correlationStart) return;
    var elapsed = ts - correlationStart;
    if (elapsed > CORRELATION_DURATION) { correlationData = null; return; }

    var fade = elapsed < 1000 ? elapsed / 1000 :
               elapsed > CORRELATION_DURATION - 2000 ? (CORRELATION_DURATION - elapsed) / 2000 : 1;

    var colA = domainColor(correlationData.source_a);
    var colB = domainColor(correlationData.source_b);

    // Positions (upper area, avoiding center radar)
    var ax = W * 0.06;
    var ay = H * 0.14;
    var bx = W * 0.42;
    var by = H * 0.08;

    // Pulsing arc
    var pulse = 0.5 + 0.5 * Math.sin(ts / 400);
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(ax + 80, ay);
    var cpx = (ax + bx) / 2;
    var cpy = Math.min(ay, by) - H * 0.05;
    ctx.quadraticCurveTo(cpx, cpy, bx, by);
    ctx.strokeStyle = rgba(C.purple, 0.25 * fade * pulse);
    ctx.lineWidth = 1.5;
    ctx.setLineDash([5, 4]);
    ctx.stroke();
    ctx.setLineDash([]);

    // Labels
    var fontSize = Math.max(8, Math.min(W, H) * 0.009);
    ctx.font = 'bold ' + fontSize + 'px "Share Tech Mono", monospace';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';

    ctx.fillStyle = rgba(colA, 0.6 * fade);
    ctx.fillText(correlationData.source_a.toUpperCase(), ax, ay + 2);
    ctx.font = (fontSize - 1) + 'px "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(colA, 0.35 * fade);
    ctx.fillText(correlationData.text_a.substring(0, 45) + '...', ax, ay + fontSize + 4);

    ctx.font = 'bold ' + fontSize + 'px "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(colB, 0.6 * fade);
    ctx.fillText(correlationData.source_b.toUpperCase(), bx, by + 2);
    ctx.font = (fontSize - 1) + 'px "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(colB, 0.35 * fade);
    ctx.fillText(correlationData.text_b.substring(0, 45) + '...', bx, by + fontSize + 4);

    // Animated dots along arc
    for (var i = 0; i < 4; i++) {
      var t = ((i / 4) + (ts / 2500)) % 1;
      var dx = (ax + 80) * (1 - t) + bx * t;
      var dy = ay * (1 - t) * (1 - t) + cpy * 2 * t * (1 - t) + by * t * t;
      ctx.beginPath();
      ctx.arc(dx, dy, 2, 0, TWO_PI);
      ctx.fillStyle = rgba(C.purple, 0.4 * fade * (1 - Math.abs(t - 0.5) * 1.5));
      ctx.fill();
    }

    ctx.restore();
  }

  // ================================================================
  //  FEATURE 3: "This Day" Timeline Ticker
  // ================================================================

  var thisDayItems = [];

  function pollThisDay() {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/this-day', true);
    xhr.timeout = 8000;
    xhr.onload = function () {
      if (xhr.status === 200) {
        try {
          var data = JSON.parse(xhr.responseText);
          if (data.length > 0) {
            thisDayItems = data;
            updateThisDayTicker();
          }
        } catch (e) {}
      }
    };
    xhr.send();
  }

  function updateThisDayTicker() {
    var el = document.getElementById('hud-thisday-ticker');
    if (!el || thisDayItems.length === 0) return;
    var html = '';
    for (var i = 0; i < thisDayItems.length; i++) {
      var item = thisDayItems[i];
      var text = item.content.replace(/[\n\r]+/g, ' ').substring(0, 100);
      html += '<span class="thisday-item">';
      html += '<span class="thisday-year">' + item.year + '</span> ';
      html += '<span class="thisday-source">[' + escapeHtml(item.source) + ']</span> ';
      html += '<span class="thisday-text">' + escapeHtml(text) + '</span>';
      html += '</span>';
    }
    el.innerHTML = html + html;
    var contentWidth = el.scrollWidth / 2;
    var speed = Math.max(45, contentWidth / 55);
    el.style.animationDuration = speed + 's';
  }

  // ================================================================
  //  FEATURE 4: Memory Quality Weather Map (Heatmap)
  // ================================================================

  var vectorHealthData = [];

  function pollVectorHealth() {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/vector-health', true);
    xhr.timeout = 8000;
    xhr.onload = function () {
      if (xhr.status === 200) {
        try {
          var data = JSON.parse(xhr.responseText);
          vectorHealthData = data.categories || [];
        } catch (e) {}
      }
    };
    xhr.send();
  }

  function renderVectorHealthMap(ctx, ts) {
    if (vectorHealthData.length === 0) return;

    var panelPad = Math.max(16, Math.min(W, H) * 0.025);
    var startX = W - panelPad;
    var startY = H * 0.38;
    var cellSize = Math.max(9, Math.min(W, H) * 0.012);
    var gap = 2;
    var cols = 4;
    var fontSize = Math.max(7, Math.min(W, H) * 0.008);

    ctx.save();
    ctx.font = fontSize + 'px "Share Tech Mono", monospace';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'top';
    ctx.fillStyle = rgba(C.cyan, 0.2);
    ctx.fillText('VECTOR HEALTH', startX, startY - fontSize - 3);

    for (var i = 0; i < Math.min(vectorHealthData.length, 16); i++) {
      var cat = vectorHealthData[i];
      var col = Math.floor(i / cols);
      var row = i % cols;
      var cx = startX - col * (cellSize + gap) - cellSize / 2;
      var cy = startY + row * (cellSize + gap) + cellSize / 2;

      var health = cat.health || 0.5;
      var color = health >= 0.8 ? C.green : health >= 0.5 ? C.amber : C.red;
      var pulse = 0.7 + 0.3 * Math.sin(ts / 1500 + i * 0.5);

      ctx.fillStyle = rgba(color, 0.12 * pulse * health);
      ctx.fillRect(cx - cellSize / 2, cy - cellSize / 2, cellSize, cellSize);
      ctx.strokeStyle = rgba(color, 0.25 * pulse);
      ctx.lineWidth = 0.6;
      ctx.strokeRect(cx - cellSize / 2, cy - cellSize / 2, cellSize, cellSize);

      if (health > 0.6) {
        ctx.fillStyle = rgba(color, 0.25 * pulse * health);
        ctx.fillRect(cx - cellSize / 4, cy - cellSize / 4, cellSize / 2, cellSize / 2);
      }
    }
    ctx.restore();
  }

  // ================================================================
  //  FEATURE 5: Active Ingest Dashboard
  // ================================================================

  var activeIngests = [];

  function pollActiveIngests() {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/active-ingests', true);
    xhr.timeout = 5000;
    xhr.onload = function () {
      if (xhr.status === 200) {
        try { activeIngests = JSON.parse(xhr.responseText); } catch (e) {}
      }
    };
    xhr.send();
  }

  function renderActiveIngests(ctx, ts) {
    if (activeIngests.length === 0) return;

    var panelPad = Math.max(16, Math.min(W, H) * 0.025);
    var startX = panelPad;
    var startY = H * 0.72;
    var fontSize = Math.max(8, Math.min(W, H) * 0.01);
    var pulse = 0.7 + 0.3 * Math.sin(ts / 800);

    ctx.save();
    ctx.font = 'bold ' + fontSize + 'px "Share Tech Mono", monospace';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    ctx.fillStyle = rgba(C.amber, 0.35);
    ctx.fillText('ACTIVE INGESTS', startX, startY);

    ctx.font = (fontSize - 1) + 'px "Share Tech Mono", monospace';
    var y = startY + fontSize + 3;
    for (var i = 0; i < Math.min(activeIngests.length, 3); i++) {
      var job = activeIngests[i];
      var title = (job.title || 'unknown').substring(0, 28);
      var vector = job.vector || '?';
      var progress = job.progress != null ? (job.progress + '%') : '...';

      // Spinning indicator
      var angle = (ts / 300 + i * 2) % TWO_PI;
      var ix = startX + 4;
      var iy = y + fontSize / 2;
      ctx.beginPath();
      ctx.arc(ix, iy, 3, angle, angle + 1.5);
      ctx.strokeStyle = rgba(C.amber, 0.5 * pulse);
      ctx.lineWidth = 1.5;
      ctx.stroke();

      ctx.fillStyle = rgba(C.amber, 0.55 * pulse);
      ctx.fillText(title, startX + 12, y);
      ctx.fillStyle = rgba(C.cyan, 0.3);
      ctx.fillText('[' + vector + '] ' + progress, startX + 12, y + fontSize);
      y += fontSize * 2 + 3;
    }
    ctx.restore();
  }

  // ================================================================
  //  FEATURE 6: Nova's Mood / Activity State
  // ================================================================

  var novaMood = 'idle';
  var moodTransitionStart = 0;
  var prevMood = 'idle';

  function determineMood() {
    var state = window.__novaHudState;
    if (!state) { novaMood = 'idle'; return; }

    // Error state (highest priority)
    var services = state.services || {};
    for (var key in services) {
      if (services[key].status === 'down' || services[key].status === 'error') {
        setMood('error'); return;
      }
    }
    if (state.gateway && !state.gateway.ok) { setMood('error'); return; }

    // Active ingests
    if (activeIngests.length > 0) { setMood('ingesting'); return; }

    // Running tasks
    var sched = state.scheduler || {};
    if (sched.running_tasks && sched.running_tasks.length > 0) { setMood('tools'); return; }

    // Recent chat activity
    var chatMsgs = document.querySelectorAll('#chatfeed-messages .chatfeed-msg');
    if (chatMsgs.length > 0) {
      var lastMsg = chatMsgs[chatMsgs.length - 1];
      var msgTs = new Date(lastMsg.dataset.ts || 0).getTime();
      if (Date.now() - msgTs < 60000) { setMood('chatting'); return; }
    }

    setMood('idle');
  }

  function setMood(mood) {
    if (mood !== novaMood) {
      prevMood = novaMood;
      novaMood = mood;
      moodTransitionStart = performance.now();
    }
  }

  function getMoodColor() {
    switch (novaMood) {
      case 'idle': return C.blue;
      case 'chatting': return C.green;
      case 'ingesting': return C.amber;
      case 'error': return C.red;
      case 'tools': return C.purple;
      default: return C.blue;
    }
  }

  function renderMoodIndicator(ctx, ts) {
    // Draw a mood indicator ring around the gateway center
    var centerX = W / 2;
    var centerY = H * 0.47;
    var UNIT = Math.min(W, H);
    var moodR = UNIT * 0.22 * (0.35 + 6 * 0.12) + 20; // Just outside gateway outer ring

    var col = getMoodColor();
    var pulseSpeed = novaMood === 'error' ? 300 : novaMood === 'chatting' ? 600 : 1200;
    var pulse = 0.4 + 0.6 * Math.sin(ts / pulseSpeed);

    // Mood glow ring
    ctx.save();
    ctx.beginPath();
    ctx.arc(centerX, centerY, moodR, 0, TWO_PI);
    ctx.strokeStyle = rgba(col, 0.08 * pulse);
    ctx.lineWidth = 3;
    ctx.stroke();

    // Pulsing arc segment (rotating)
    var arcAngle = (ts / 2000) % TWO_PI;
    var arcLen = 0.8 + pulse * 0.4;
    ctx.beginPath();
    ctx.arc(centerX, centerY, moodR, arcAngle, arcAngle + arcLen);
    ctx.strokeStyle = rgba(col, 0.2 * pulse);
    ctx.lineWidth = 2.5;
    ctx.stroke();

    // Mood label (tiny, at top)
    var labelY = centerY - moodR - 8;
    var fontSize = Math.max(7, UNIT * 0.008);
    ctx.font = fontSize + 'px "Share Tech Mono", monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'bottom';
    ctx.fillStyle = rgba(col, 0.3 * pulse);
    ctx.fillText(novaMood.toUpperCase(), centerX, labelY);

    ctx.restore();
  }

  // ================================================================
  //  FEATURE 7: Chatroom Unread Counter
  // ================================================================

  var chatUnreadCount = 0;
  var chatLastViewedCount = 0;
  var chatBadgeFlashTs = 0;

  function updateChatUnread() {
    var msgs = document.querySelectorAll('#chatfeed-messages .chatfeed-msg');
    var total = msgs.length;
    if (total > chatLastViewedCount) {
      chatUnreadCount += (total - chatLastViewedCount);
      chatBadgeFlashTs = performance.now();
    }
    chatLastViewedCount = total;
    updateChatBadge();
  }

  function updateChatBadge() {
    var badge = document.getElementById('chatfeed-unread-badge');
    if (!badge) return;
    if (chatUnreadCount > 0) {
      badge.textContent = chatUnreadCount > 99 ? '99+' : chatUnreadCount.toString();
      badge.style.display = 'inline-block';
      var elapsed = performance.now() - chatBadgeFlashTs;
      if (elapsed < 2000) {
        badge.classList.add('flash');
      } else {
        badge.classList.remove('flash');
      }
    } else {
      badge.style.display = 'none';
    }
  }

  // Reset unread every 5 minutes
  setInterval(function () { chatUnreadCount = 0; updateChatBadge(); }, 300000);

  // ================================================================
  //  FEATURE 8: Scheduler Gantt Chart
  // ================================================================

  var schedulerTodayData = [];

  function pollSchedulerToday() {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/scheduler-today', true);
    xhr.timeout = 5000;
    xhr.onload = function () {
      if (xhr.status === 200) {
        try { schedulerTodayData = JSON.parse(xhr.responseText); } catch (e) {}
      }
    };
    xhr.send();
  }

  function renderSchedulerGantt(ctx, ts) {
    if (schedulerTodayData.length === 0) return;

    var panelPad = Math.max(16, Math.min(W, H) * 0.025);
    var chartX = W * 0.13;
    var chartY = H - panelPad - 50;
    var chartW = W * 0.5;
    var chartH = 16;
    var fontSize = Math.max(7, Math.min(W, H) * 0.008);

    ctx.save();

    // Background
    ctx.fillStyle = rgba(C.dim, 0.15);
    ctx.fillRect(chartX, chartY, chartW, chartH);

    // Hour markers
    ctx.font = fontSize + 'px "Share Tech Mono", monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (var h = 0; h <= 24; h += 4) {
      var x = chartX + (h / 24) * chartW;
      ctx.beginPath();
      ctx.moveTo(x, chartY);
      ctx.lineTo(x, chartY + chartH);
      ctx.strokeStyle = rgba(C.cyan, 0.06);
      ctx.lineWidth = 0.5;
      ctx.stroke();
      ctx.fillStyle = rgba(C.cyan, 0.18);
      ctx.fillText(h + '', x, chartY + chartH + 2);
    }

    // Now marker
    var now = new Date();
    var currentHour = now.getHours() + now.getMinutes() / 60;
    var nowX = chartX + (currentHour / 24) * chartW;
    ctx.beginPath();
    ctx.moveTo(nowX, chartY - 1);
    ctx.lineTo(nowX, chartY + chartH + 1);
    ctx.strokeStyle = rgba(C.cyan, 0.35);
    ctx.lineWidth = 1;
    ctx.stroke();

    // Task dots (seeded random vertical offset for consistent display)
    for (var i = 0; i < schedulerTodayData.length; i++) {
      var task = schedulerTodayData[i];
      var hour = task.hour || 0;
      if (hour < 0 || hour > 24) continue;
      var tx = chartX + (hour / 24) * chartW;
      // Use hash of label for consistent y position
      var labelHash = 0;
      var lbl = task.label || '';
      for (var li = 0; li < lbl.length; li++) labelHash = ((labelHash << 3) - labelHash) + lbl.charCodeAt(li);
      var ty = chartY + 3 + (Math.abs(labelHash) % (chartH - 6));

      var taskCol = task.status === 'succeeded' ? C.green :
                    task.status === 'failed' ? C.red :
                    task.status === 'timed_out' ? C.amber : C.dim;

      ctx.beginPath();
      ctx.arc(tx, ty, 2, 0, TWO_PI);
      ctx.fillStyle = rgba(taskCol, 0.55);
      ctx.fill();
    }

    // Label
    ctx.textAlign = 'left';
    ctx.textBaseline = 'bottom';
    ctx.fillStyle = rgba(C.cyan, 0.2);
    ctx.fillText('TASK TIMELINE', chartX, chartY - 2);

    // Count legend
    ctx.textAlign = 'right';
    var okCount = schedulerTodayData.filter(function (t) { return t.status === 'succeeded'; }).length;
    var failCount = schedulerTodayData.filter(function (t) { return t.status === 'failed'; }).length;
    ctx.fillStyle = rgba(C.green, 0.3);
    ctx.fillText(okCount + ' ok', chartX + chartW, chartY - 2);
    if (failCount > 0) {
      ctx.fillStyle = rgba(C.red, 0.4);
      ctx.fillText(failCount + ' fail  ', chartX + chartW - 40, chartY - 2);
    }

    ctx.restore();
  }

  // ================================================================
  //  FEATURE 9: "Nova's Brain" 3D Particle Cloud
  // ================================================================

  var brainMode = false;
  var brainModeStart = 0;
  var BRAIN_DURATION = 12000;
  var BRAIN_INTERVAL = 120000;
  var lastBrainEnd = 0;
  var brainPoints = [];
  var brainRotation = 0;

  function initBrainPoints() {
    if (vectorHealthData.length === 0) return;
    brainPoints = [];
    var count = vectorHealthData.length;
    for (var i = 0; i < count; i++) {
      var cat = vectorHealthData[i];
      var col = domainColor(cat.source);
      var size = Math.min(7, Math.max(2, Math.log10(Math.max(1, cat.total)) * 1.4));
      var phi = Math.acos(1 - 2 * (i + 0.5) / count);
      var theta = Math.PI * (1 + Math.sqrt(5)) * i;
      var radius = 0.55 + Math.random() * 0.45;
      brainPoints.push({
        x: Math.sin(phi) * Math.cos(theta) * radius,
        y: Math.sin(phi) * Math.sin(theta) * radius,
        z: Math.cos(phi) * radius,
        size: size, col: col, label: cat.source, total: cat.total,
      });
    }
  }

  function checkBrainMode(ts) {
    if (!brainMode && vectorHealthData.length > 4 && ts - lastBrainEnd > BRAIN_INTERVAL) {
      brainMode = true;
      brainModeStart = ts;
      initBrainPoints();
    }
    if (brainMode && ts - brainModeStart > BRAIN_DURATION) {
      brainMode = false;
      lastBrainEnd = ts;
    }
  }

  function renderBrainCloud(ctx, ts) {
    if (!brainMode || brainPoints.length === 0) return;
    var elapsed = ts - brainModeStart;

    var fade = elapsed < 2000 ? elapsed / 2000 :
               elapsed > BRAIN_DURATION - 2000 ? (BRAIN_DURATION - elapsed) / 2000 : 1;

    brainRotation += 0.002;
    var cosR = Math.cos(brainRotation);
    var sinR = Math.sin(brainRotation);
    var cosT = Math.cos(brainRotation * 0.4);
    var sinT = Math.sin(brainRotation * 0.4);

    var cloudR = Math.min(W, H) * 0.2;
    var cx = W * 0.5;
    var cy = H * 0.47;
    var fov = 500;

    ctx.save();

    var projected = [];
    for (var i = 0; i < brainPoints.length; i++) {
      var p = brainPoints[i];
      var rx = p.x * cosR - p.z * sinR;
      var rz = p.x * sinR + p.z * cosR;
      var ry = p.y * cosT - rz * sinT;
      var rz2 = p.y * sinT + rz * cosT;
      var scale = fov / (fov + rz2 * cloudR);
      projected.push({
        sx: cx + rx * cloudR * scale,
        sy: cy + ry * cloudR * scale,
        z: rz2, scale: scale, point: p
      });
    }
    projected.sort(function (a, b) { return a.z - b.z; });

    // Connections
    for (var i = 0; i < projected.length; i++) {
      for (var j = i + 1; j < Math.min(projected.length, i + 6); j++) {
        var dx = projected[i].sx - projected[j].sx;
        var dy = projected[i].sy - projected[j].sy;
        var dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < cloudR * 0.45) {
          ctx.beginPath();
          ctx.moveTo(projected[i].sx, projected[i].sy);
          ctx.lineTo(projected[j].sx, projected[j].sy);
          ctx.strokeStyle = rgba(C.cyan, (1 - dist / (cloudR * 0.45)) * 0.04 * fade);
          ctx.lineWidth = 0.5;
          ctx.stroke();
        }
      }
    }

    // Points
    var fontSize = Math.max(7, Math.min(W, H) * 0.007);
    for (var i = 0; i < projected.length; i++) {
      var pp = projected[i];
      var p = pp.point;
      var depthFade = 0.3 + 0.7 * ((pp.z + 1) / 2);
      var s = p.size * pp.scale;

      ctx.beginPath();
      ctx.arc(pp.sx, pp.sy, s, 0, TWO_PI);
      ctx.fillStyle = rgba(p.col, 0.55 * fade * depthFade);
      ctx.fill();

      ctx.beginPath();
      ctx.arc(pp.sx, pp.sy, s * 2, 0, TWO_PI);
      ctx.fillStyle = rgba(p.col, 0.06 * fade * depthFade);
      ctx.fill();

      if (s > 3 && depthFade > 0.55) {
        ctx.font = fontSize + 'px "Share Tech Mono", monospace';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        ctx.fillStyle = rgba(p.col, 0.3 * fade * depthFade);
        ctx.fillText(p.label.substring(0, 10), pp.sx, pp.sy + s + 2);
      }
    }

    // Title
    ctx.font = 'bold ' + Math.max(9, Math.min(W, H) * 0.011) + 'px "Orbitron", monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillStyle = rgba(C.cyan, 0.25 * fade);
    ctx.fillText('NEURAL TOPOLOGY', cx, cy - cloudR - 16);

    ctx.restore();
  }

  // ================================================================
  //  FEATURE 10: Random Memory Spotlight
  // ================================================================

  var spotlightData = null;
  var spotlightStart = 0;
  var SPOTLIGHT_DURATION = 8000;

  function pollRandomMemory() {
    var xhr = new XMLHttpRequest();
    xhr.open('GET', '/api/random-memory', true);
    xhr.timeout = 5000;
    xhr.onload = function () {
      if (xhr.status === 200) {
        try {
          var data = JSON.parse(xhr.responseText);
          if (data.text && data.text.length > 20) {
            spotlightData = data;
            spotlightStart = performance.now();
          }
        } catch (e) {}
      }
    };
    xhr.send();
  }

  function renderSpotlight(ctx, ts) {
    if (!spotlightData || !spotlightStart) return;
    var elapsed = ts - spotlightStart;
    if (elapsed > SPOTLIGHT_DURATION) { spotlightData = null; return; }

    // Don't show during brain mode
    if (brainMode) return;

    var fade = elapsed < 1500 ? elapsed / 1500 :
               elapsed > SPOTLIGHT_DURATION - 2000 ? (SPOTLIGHT_DURATION - elapsed) / 2000 : 1;

    var panelPad = Math.max(16, Math.min(W, H) * 0.025);
    var y = H - panelPad - 90;
    var maxWidth = W * 0.55;

    ctx.save();

    // Semi-transparent background
    ctx.fillStyle = rgba([10, 14, 20], 0.5 * fade);
    ctx.fillRect(panelPad - 4, y - 6, maxWidth + 24, 52);
    ctx.strokeStyle = rgba(C.cyan, 0.06 * fade);
    ctx.lineWidth = 0.5;
    ctx.strokeRect(panelPad - 4, y - 6, maxWidth + 24, 52);

    // Source label
    var fontSize = Math.max(8, Math.min(W, H) * 0.009);
    var col = domainColor(spotlightData.source);
    ctx.font = 'bold ' + fontSize + 'px "Share Tech Mono", monospace';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    ctx.fillStyle = rgba(col, 0.5 * fade);
    var yearStr = spotlightData.year > 0 ? ' (' + spotlightData.year + ')' : '';
    ctx.fillText('MEMORY: ' + spotlightData.source.toUpperCase() + yearStr, panelPad, y);

    // Text content (word-wrapped)
    var textFontSize = Math.max(10, Math.min(W, H) * 0.012);
    ctx.font = textFontSize + 'px "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(C.white, 0.6 * fade);

    var words = spotlightData.text.split(' ');
    var line = '';
    var lineY = y + fontSize + 5;
    var lineCount = 0;
    for (var i = 0; i < words.length && lineCount < 2; i++) {
      var testLine = line + words[i] + ' ';
      if (ctx.measureText(testLine).width > maxWidth && line !== '') {
        ctx.fillText(line.trim(), panelPad, lineY);
        line = words[i] + ' ';
        lineY += textFontSize + 2;
        lineCount++;
      } else {
        line = testLine;
      }
    }
    if (lineCount < 2 && line.trim()) {
      ctx.fillText(line.trim(), panelPad, lineY);
    }

    ctx.restore();
  }

  // ================================================================
  //  MAIN RENDER HOOK (called by hud.js each frame)
  // ================================================================

  window.__hudFeaturesRender = function (ctx, ts, width, height) {
    W = width;
    H = height;

    // Feature 1: Ingest particles
    renderIngestParticles(ctx);

    // Feature 2: Correlation arcs
    renderCorrelation(ctx, ts);

    // Feature 4: Vector health heatmap
    renderVectorHealthMap(ctx, ts);

    // Feature 5: Active ingests
    renderActiveIngests(ctx, ts);

    // Feature 6: Mood indicator
    renderMoodIndicator(ctx, ts);

    // Feature 8: Scheduler Gantt
    renderSchedulerGantt(ctx, ts);

    // Feature 9: Brain cloud (when active)
    checkBrainMode(ts);
    renderBrainCloud(ctx, ts);

    // Feature 10: Memory spotlight
    renderSpotlight(ctx, ts);
  };

  // ================================================================
  //  INITIALIZATION
  // ================================================================

  function init() {
    // Create unread badge for chatfeed (Feature 7)
    var chatHeader = document.getElementById('chatfeed-header');
    if (chatHeader) {
      var badge = document.createElement('span');
      badge.id = 'chatfeed-unread-badge';
      badge.className = 'chatfeed-unread-badge';
      badge.style.display = 'none';
      chatHeader.appendChild(badge);
    }

    // Start polling endpoints
    pollIngestActivity();
    setInterval(pollIngestActivity, 10000);

    pollCorrelation();
    setInterval(pollCorrelation, 30000);

    pollThisDay();
    setInterval(pollThisDay, 300000);

    pollVectorHealth();
    setInterval(pollVectorHealth, 120000);

    pollActiveIngests();
    setInterval(pollActiveIngests, 10000);

    pollSchedulerToday();
    setInterval(pollSchedulerToday, 30000);

    pollRandomMemory();
    setInterval(pollRandomMemory, 30000);

    // Update chat unread and mood every 3 seconds
    setInterval(updateChatUnread, 3000);
    setInterval(function () { determineMood(); }, 5000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

})();
