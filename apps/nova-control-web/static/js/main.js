window.novaState = null;

const WS_URL = `ws://${location.host}/ws`;
let ws = null;
let reconnectDelay = 1000;
let taskTableSort = { col: 'name', dir: 'asc' };
let prevNetBytes = null;
let prevNetTs = null;

function connect() {
  ws = new WebSocket(WS_URL);
  ws.onopen = () => { reconnectDelay = 1000; setConnectionStatus('connected'); };
  ws.onmessage = (event) => {
    const state = JSON.parse(event.data);
    window.novaState = state;
    renderCards(state);
    document.getElementById('poll-latency').textContent = `poll: ${state.poll_duration_ms}ms`;
  };
  ws.onclose = () => {
    setConnectionStatus('disconnected');
    setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 1.5, 10000);
  };
  ws.onerror = () => ws.close();
}

function setConnectionStatus(status) {
  document.getElementById('connection-status').className = 'status-dot ' + status;
}

function formatUptime(seconds) {
  if (!seconds || seconds <= 0) return '---';
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const parts = [];
  if (d > 0) parts.push(d + 'd');
  if (h > 0) parts.push(h + 'h');
  parts.push(m + 'm');
  return parts.join(' ');
}

function formatBytes(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  if (bytes < 1024 * 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
  return (bytes / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
}

function formatRate(bytesPerSec) {
  if (bytesPerSec < 1024) return bytesPerSec.toFixed(0) + ' B/s';
  if (bytesPerSec < 1024 * 1024) return (bytesPerSec / 1024).toFixed(1) + ' KB/s';
  return (bytesPerSec / (1024 * 1024)).toFixed(1) + ' MB/s';
}

function statusClass(status) {
  if (['running', 'up', 'ok', 'live'].includes(status)) return 'healthy';
  if (['degraded', 'slow', 'warning'].includes(status)) return 'degraded';
  if (['down', 'error', 'stopped'].includes(status)) return 'down';
  return 'unknown';
}

function statRow(label, value, colorClass) {
  const cls = colorClass ? ` class="stat-value ${colorClass}"` : ' class="stat-value"';
  return `<div class="stat-row"><span class="stat-label">${label}</span><span${cls}>${value}</span></div>`;
}

function progressBar(percent, thresholds) {
  let color = 'green';
  if (thresholds) {
    if (percent >= thresholds[1]) color = 'red';
    else if (percent >= thresholds[0]) color = 'yellow';
  }
  return `<div class="progress-bar-track"><div class="progress-bar-fill ${color}" style="width:${Math.min(100, percent)}%"></div></div>`;
}

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function renderCards(state) {
  renderSystem(state.system);
  renderGateway(state.gateway);
  renderScheduler(state.scheduler);
  renderOllama(state.ollama);
  renderPostgresql(state.postgresql);
  renderRedis(state.redis);
  renderMemory(state);
  renderModelUsage(state.model_usage);
  renderGatewayQueries(state.gateway_queries);
  renderTaskHistory(state.task_history);
  renderThroughput(state.task_throughput);
  renderLatency(state.services);
  renderTaskTable(state.scheduler);
  for (const name of ['analyst', 'sentinel', 'coder', 'lookout', 'librarian']) {
    renderAgent(name, state.agents?.[name]);
  }
}

// --- System Resources ---
function renderSystem(sys) {
  const card = document.getElementById('card-system');
  if (!sys) return;
  const ok = sys.status === 'ok';
  const memPct = sys.memory?.percent || 0;
  const cpuPct = sys.cpu_percent || 0;
  card.dataset.status = ok ? (cpuPct > 90 || memPct > 90 ? 'degraded' : 'healthy') : 'down';
  const body = card.querySelector('.card-body');

  let netRate = '';
  if (sys.network) {
    const now = Date.now() / 1000;
    if (prevNetBytes) {
      const dt = now - prevNetTs;
      if (dt > 0) {
        const txRate = (sys.network.bytes_sent - prevNetBytes.sent) / dt;
        const rxRate = (sys.network.bytes_recv - prevNetBytes.recv) / dt;
        netRate = statRow('Net TX', formatRate(Math.max(0, txRate)), 'cyan') +
                  statRow('Net RX', formatRate(Math.max(0, rxRate)), 'green');
      }
    }
    prevNetBytes = { sent: sys.network.bytes_sent, recv: sys.network.bytes_recv };
    prevNetTs = now;
  }

  let html =
    statRow('CPU', cpuPct.toFixed(1) + '%', cpuPct > 80 ? 'red' : cpuPct > 50 ? 'yellow' : 'green') +
    progressBar(cpuPct, [50, 80]) +
    statRow('Memory', `${sys.memory?.used_gb || 0}/${sys.memory?.total_gb || 0} GB (${memPct}%)`,
      memPct > 85 ? 'red' : memPct > 70 ? 'yellow' : 'green') +
    progressBar(memPct, [70, 85]);

  if (sys.swap && sys.swap.total_gb > 0) {
    html += statRow('Swap', `${sys.swap.used_gb}/${sys.swap.total_gb} GB`, sys.swap.percent > 50 ? 'yellow' : '');
  }

  html += netRate;

  if (sys.disks) {
    for (const [mount, d] of Object.entries(sys.disks)) {
      const label = mount === '/' || mount === '/System/Volumes/Data' ? 'SSD' :
                    mount.replace('/Volumes/', '');
      const color = d.percent > 90 ? 'red' : d.percent > 80 ? 'yellow' : 'green';
      html += statRow(label, `${d.free_gb} GB free (${d.percent}%)`, color) +
              progressBar(d.percent, [80, 90]);
    }
  }

  body.innerHTML = html;
}

// --- Gateway ---
function renderGateway(gw) {
  const card = document.getElementById('card-gateway');
  if (!gw) return;
  card.dataset.status = gw.ok ? 'healthy' : 'down';
  const body = card.querySelector('.card-body');
  body.innerHTML =
    statRow('Status', gw.gateway_status || 'unknown', gw.ok ? 'green' : 'red') +
    statRow('WebSocket', gw.ws_reachable ? 'reachable' : 'unreachable', gw.ws_reachable ? 'green' : 'red') +
    statRow('Health', gw.ok ? 'OK' : 'FAIL', gw.ok ? 'green' : 'red') +
    (gw.error ? `<div class="error-text">${escapeHtml(gw.error)}</div>` : '');
}

// --- Scheduler ---
function renderScheduler(sched) {
  const card = document.getElementById('card-scheduler');
  if (!sched) return;
  const ok = sched.status === 'ok';
  card.dataset.status = ok ? 'healthy' : 'down';
  const body = card.querySelector('.card-body');
  const info = sched.info || {};
  const successRate = info.total_runs > 0
    ? ((info.total_runs - info.total_failures) / info.total_runs * 100).toFixed(1) : '---';

  let html =
    statRow('Uptime', formatUptime(info.uptime_s), 'cyan') +
    statRow('Total Jobs', info.tasks_total || 0) +
    statRow('Running', info.tasks_running || 0, info.tasks_running > 0 ? 'cyan' : '') +
    statRow('Total Runs', (info.total_runs || 0).toLocaleString()) +
    statRow('Failures', info.total_failures || 0, info.total_failures > 0 ? 'yellow' : 'green') +
    statRow('Success Rate', successRate + '%', parseFloat(successRate) >= 98 ? 'green' : 'yellow');

  if (sched.running_tasks?.length > 0) {
    for (const t of sched.running_tasks) {
      html += `<div style="color:var(--accent-cyan);font-size:10px;margin-top:2px">&#9654; ${escapeHtml(t)}</div>`;
    }
  }
  body.innerHTML = html;
}

// --- Ollama Models ---
function renderOllama(ol) {
  const card = document.getElementById('card-ollama');
  if (!ol) return;
  const ok = ol.status === 'ok';
  card.dataset.status = ok ? 'healthy' : 'down';
  const body = card.querySelector('.card-body');

  let html =
    statRow('Loaded Models', ol.model_count || 0, 'cyan') +
    statRow('Total VRAM', (ol.total_vram_gb || 0) + ' GB', 'magenta');

  if (ol.models) {
    for (const m of ol.models) {
      html += `<div class="model-row">
        <div>
          <div class="model-name">${escapeHtml(m.name)}</div>
          <div class="model-detail">${escapeHtml(m.family)} &middot; ${escapeHtml(m.params)} &middot; ${escapeHtml(m.quant)}</div>
        </div>
        <div style="text-align:right">
          <div class="stat-value cyan">${m.vram_gb} GB</div>
          <div class="model-detail">ctx: ${(m.context_length || 0).toLocaleString()}</div>
        </div>
      </div>`;
    }
  }
  body.innerHTML = html;
}

// --- PostgreSQL ---
function renderPostgresql(pg) {
  const card = document.getElementById('card-postgresql');
  if (!pg) return;
  card.dataset.status = pg.status === 'ok' ? 'healthy' : 'down';
  const body = card.querySelector('.card-body');

  let html =
    statRow('Database Size', pg.db_size_gb + ' GB', 'magenta') +
    statRow('Total Rows', (pg.total_rows || 0).toLocaleString(), 'cyan') +
    statRow('Indexes', pg.index_count || 0);

  if (pg.tables?.length > 0) {
    html += '<div style="margin-top:8px">';
    for (const t of pg.tables) {
      html += `<div class="pg-table-row">
        <span class="pg-table-name">${escapeHtml(t.name)}</span>
        <span class="pg-table-rows">${t.rows.toLocaleString()} rows</span>
      </div>`;
    }
    html += '</div>';
  }
  if (pg.error) html += `<div class="error-text">${escapeHtml(pg.error)}</div>`;
  body.innerHTML = html;
}

// --- Redis ---
function renderRedis(r) {
  const card = document.getElementById('card-redis');
  if (!r) return;
  const ok = r.status === 'ok';
  card.dataset.status = ok ? 'healthy' : 'down';
  const body = card.querySelector('.card-body');
  const qColor = r.ingest_queue_depth > 50 ? 'red' : r.ingest_queue_depth > 20 ? 'yellow' : 'green';
  body.innerHTML =
    statRow('Status', ok ? 'Connected' : 'Down', ok ? 'green' : 'red') +
    statRow('Keys', r.db_size || 0) +
    statRow('Ingest Queue', r.ingest_queue_depth || 0, qColor) +
    (r.error ? `<div class="error-text">${escapeHtml(r.error)}</div>` : '');
}

// --- Memory System ---
function renderMemory(state) {
  const card = document.getElementById('card-memory');
  const memSvc = state.services?.memory_server;
  const redis = state.redis;
  const memUp = memSvc?.status === 'up';
  const redisOk = redis?.status === 'ok';
  card.dataset.status = memUp && redisOk ? 'healthy' : memUp || redisOk ? 'degraded' : 'down';
  const body = card.querySelector('.card-body');
  body.innerHTML =
    statRow('Memory Server', memUp ? 'Online' : 'Offline', memUp ? 'green' : 'red') +
    statRow('Port', memSvc?.port || 18790) +
    statRow('Redis Backend', redisOk ? 'Connected' : 'Down', redisOk ? 'green' : 'red') +
    statRow('Ingest Queue', redis?.ingest_queue_depth || 0,
      (redis?.ingest_queue_depth || 0) > 20 ? 'yellow' : 'cyan');
}

// --- Model Usage ---
function renderModelUsage(mu) {
  const card = document.getElementById('card-model-usage');
  if (!mu) return;
  card.dataset.status = mu.status === 'ok' ? 'healthy' : (mu.status === 'error' ? 'down' : 'unknown');
  const body = card.querySelector('.card-body');

  let html =
    statRow('Total Sessions', mu.total_sessions || 0, 'cyan') +
    statRow('Total Tokens', (mu.total_tokens || 0).toLocaleString(), 'green') +
    statRow('Total Cost', '$' + (mu.total_cost_usd || 0).toFixed(4), 'magenta');

  // By provider
  if (mu.by_provider && Object.keys(mu.by_provider).length > 0) {
    html += '<div class="stat-label" style="margin-top:10px;margin-bottom:4px">By Provider</div>';
    for (const [prov, s] of Object.entries(mu.by_provider).sort((a,b) => b[1].sessions - a[1].sessions)) {
      if (prov === 'unknown') continue;
      const tokens = (s.input_tokens + s.output_tokens).toLocaleString();
      const color = prov === 'ollama' ? 'green' : prov === 'openrouter' ? 'magenta' : 'cyan';
      html += `<div class="model-row">
        <div>
          <div class="model-name" style="color:var(--accent-${color})">${escapeHtml(prov)}</div>
          <div class="model-detail">${s.sessions} sessions &middot; ${tokens} tokens</div>
        </div>
        <div style="text-align:right">
          <div class="stat-value ${color}">${s.cost > 0 ? '$' + s.cost.toFixed(4) : '$0'}</div>
          <div class="model-detail">${s.input_tokens.toLocaleString()} in / ${s.output_tokens.toLocaleString()} out</div>
        </div>
      </div>`;
    }
  }

  // By model (top 5)
  if (mu.by_model && Object.keys(mu.by_model).length > 0) {
    html += '<div class="stat-label" style="margin-top:10px;margin-bottom:4px">By Model</div>';
    const models = Object.entries(mu.by_model)
      .filter(([m]) => m !== 'unknown')
      .sort((a,b) => (b[1].input_tokens + b[1].output_tokens) - (a[1].input_tokens + a[1].output_tokens))
      .slice(0, 6);
    for (const [model, s] of models) {
      const tokens = (s.input_tokens + s.output_tokens).toLocaleString();
      const isLocal = s.provider === 'ollama';
      html += `<div class="pg-table-row">
        <span class="pg-table-name">${escapeHtml(model)}</span>
        <span class="pg-table-rows">${s.sessions}s &middot; ${tokens} tok ${isLocal ? '<span style="color:var(--accent-green);font-size:9px">LOCAL</span>' : '<span style="color:var(--accent-magenta);font-size:9px">CLOUD</span>'}</span>
      </div>`;
    }
  }

  body.innerHTML = html;
}

// --- Gateway Queries ---
function renderGatewayQueries(gq) {
  const card = document.getElementById('card-gateway-queries');
  if (!gq) return;
  card.dataset.status = gq.status === 'ok' ? 'healthy' : gq.status === 'empty' ? 'degraded' : 'down';
  const body = card.querySelector('.card-body');

  let html = statRow('Total Queries', gq.total_queries || 0, 'cyan');

  if (gq.backends && Object.keys(gq.backends).length > 0) {
    for (const [backend, info] of Object.entries(gq.backends)) {
      html += `<div style="margin-top:8px"><span class="stat-label" style="text-transform:uppercase">${escapeHtml(backend)}</span></div>`;
      html += statRow('Queries', info.total_queries || 0);
      html += statRow('Prompt Chars', (info.total_prompt_chars || 0).toLocaleString());
      html += statRow('Response Chars', (info.total_response_chars || 0).toLocaleString());

      for (const [model, minfo] of Object.entries(info.models || {})) {
        html += `<div class="pg-table-row">
          <span class="pg-table-name">${escapeHtml(model)}</span>
          <span class="pg-table-rows">${minfo.queries}q &middot; avg ${minfo.avg_latency_ms}ms${minfo.fallbacks > 0 ? ' &middot; ' + minfo.fallbacks + ' fallback' : ''}</span>
        </div>`;
      }
    }
  } else {
    html += '<p class="dim" style="margin-top:8px">No queries logged yet</p>';
  }

  if (gq.error) html += `<div class="error-text">${escapeHtml(gq.error)}</div>`;
  body.innerHTML = html;
}

// --- Task History ---
function renderTaskHistory(th) {
  const card = document.getElementById('card-task-history');
  if (!th) return;
  card.dataset.status = th.status === 'ok' ? 'healthy' : 'down';
  const body = card.querySelector('.card-body');
  const all = th.all_time || {};
  const day = th.last_24h || {};
  const total = (all.succeeded||0) + (all.failed||0) + (all.timed_out||0) + (all.lost||0);
  const dayTotal = (day.succeeded||0) + (day.failed||0) + (day.timed_out||0) + (day.lost||0);

  let html = '<div class="stat-label" style="margin-bottom:4px">All Time</div>' +
    statRow('Succeeded', (all.succeeded||0).toLocaleString(), 'green') +
    statRow('Failed', all.failed||0, (all.failed||0) > 0 ? 'red' : 'green') +
    statRow('Timed Out', all.timed_out||0, (all.timed_out||0) > 0 ? 'yellow' : '') +
    statRow('Lost', all.lost||0, (all.lost||0) > 0 ? 'yellow' : '');

  if (total > 0) {
    html += '<div class="bar-chart">';
    html += barSeg('succeeded', all.succeeded||0, total);
    html += barSeg('timed_out', all.timed_out||0, total);
    html += barSeg('failed', all.failed||0, total);
    html += barSeg('lost', all.lost||0, total);
    html += '</div>';
  }

  html += '<div class="stat-label" style="margin-top:10px;margin-bottom:4px">Last 24h (' + dayTotal + ' total)</div>' +
    statRow('Succeeded', (day.succeeded||0).toLocaleString(), 'green') +
    statRow('Failed', day.failed||0, (day.failed||0) > 0 ? 'red' : 'green') +
    statRow('Timed Out', day.timed_out||0, (day.timed_out||0) > 0 ? 'yellow' : '');

  if (dayTotal > 0) {
    html += '<div class="bar-chart">';
    html += barSeg('succeeded', day.succeeded||0, dayTotal);
    html += barSeg('timed_out', day.timed_out||0, dayTotal);
    html += barSeg('failed', day.failed||0, dayTotal);
    html += barSeg('lost', day.lost||0, dayTotal);
    html += '</div>';
  }
  body.innerHTML = html;
}

function barSeg(cls, count, total) {
  if (count <= 0) return '';
  return `<div class="bar-segment ${cls}" style="width:${Math.max(2, count/total*100)}%"></div>`;
}

// --- Throughput Sparkline Chart ---
function renderThroughput(data) {
  const card = document.getElementById('card-throughput');
  if (!data || data.length === 0) { card.dataset.status = 'unknown'; return; }
  card.dataset.status = 'healthy';

  const canvas = document.getElementById('throughput-chart');
  const ctx = canvas.getContext('2d');
  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = rect.width * dpr;
  canvas.height = 100 * dpr;
  canvas.style.width = rect.width + 'px';
  canvas.style.height = '100px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const W = rect.width;
  const H = 100;
  const barW = Math.max(2, (W - 20) / data.length - 1);
  const maxVal = Math.max(1, ...data.map(d => (d.succeeded||0) + (d.failed||0) + (d.timed_out||0)));

  ctx.clearRect(0, 0, W, H);

  for (let i = 0; i < data.length; i++) {
    const d = data[i];
    const x = 10 + i * (barW + 1);
    const total = (d.succeeded||0) + (d.failed||0) + (d.timed_out||0) + (d.lost||0);
    const fullH = (total / maxVal) * (H - 20);

    let y = H - 10;

    // Succeeded (green)
    const sH = ((d.succeeded||0) / maxVal) * (H - 20);
    if (sH > 0) { ctx.fillStyle = 'rgba(0, 255, 102, 0.7)'; ctx.fillRect(x, y - sH, barW, sH); y -= sH; }

    // Timed out (yellow)
    const tH = ((d.timed_out||0) / maxVal) * (H - 20);
    if (tH > 0) { ctx.fillStyle = 'rgba(255, 204, 0, 0.8)'; ctx.fillRect(x, y - tH, barW, tH); y -= tH; }

    // Failed (red)
    const fH = ((d.failed||0) / maxVal) * (H - 20);
    if (fH > 0) { ctx.fillStyle = 'rgba(255, 51, 68, 0.8)'; ctx.fillRect(x, y - fH, barW, fH); y -= fH; }
  }

  // X-axis labels
  ctx.fillStyle = 'rgba(85, 85, 112, 0.6)';
  ctx.font = '9px monospace';
  ctx.textAlign = 'center';
  for (let i = 0; i < data.length; i += 4) {
    const x = 10 + i * (barW + 1) + barW / 2;
    ctx.fillText((i - data.length) + 'h', x, H - 1);
  }
}

// --- Service Latency Sparklines ---
function renderLatency(services) {
  const card = document.getElementById('card-latency');
  if (!services) return;
  card.dataset.status = 'healthy';
  const body = card.querySelector('.card-body');

  let html = '<div class="sparkline-container">';
  for (const [name, svc] of Object.entries(services)) {
    const trend = svc.latency_trend || [];
    const current = svc.latency_ms;
    const avg = trend.length > 0 ? Math.round(trend.reduce((a, b) => a + b, 0) / trend.length) : null;
    const max = trend.length > 0 ? Math.max(...trend) : null;
    const statusColor = svc.status === 'up'
      ? (current && current > 500 ? 'yellow' : 'green')
      : 'red';

    html += `<div class="sparkline-item">
      <div class="stat-row">
        <span class="stat-label">${escapeHtml(name)}</span>
        <span class="stat-value ${statusColor}">${current != null ? current + 'ms' : 'DOWN'}</span>
      </div>`;
    if (avg != null) {
      html += `<div class="stat-row"><span class="stat-label">avg/max</span><span class="stat-value" style="font-size:10px">${avg}/${max}ms</span></div>`;
    }
    html += `<canvas class="latency-spark" data-service="${escapeHtml(name)}" data-trend='${JSON.stringify(trend)}'></canvas>`;
    html += '</div>';
  }
  html += '</div>';
  body.innerHTML = html;

  requestAnimationFrame(() => {
    for (const el of body.querySelectorAll('.latency-spark')) {
      drawSparkline(el);
    }
  });
}

function drawSparkline(canvas) {
  let trend;
  try { trend = JSON.parse(canvas.dataset.trend); } catch { return; }
  if (!trend || trend.length < 2) return;

  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const W = rect.width;
  const H = 30;
  canvas.width = W * dpr;
  canvas.height = H * dpr;
  canvas.style.width = W + 'px';
  canvas.style.height = H + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const maxV = Math.max(1, ...trend);
  const step = W / (trend.length - 1);

  // Fill
  ctx.beginPath();
  ctx.moveTo(0, H);
  for (let i = 0; i < trend.length; i++) {
    ctx.lineTo(i * step, H - (trend[i] / maxV) * (H - 4));
  }
  ctx.lineTo(W, H);
  ctx.closePath();
  ctx.fillStyle = 'rgba(0, 255, 200, 0.06)';
  ctx.fill();

  // Line
  ctx.beginPath();
  for (let i = 0; i < trend.length; i++) {
    const x = i * step;
    const y = H - (trend[i] / maxV) * (H - 4);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.strokeStyle = 'rgba(0, 255, 200, 0.5)';
  ctx.lineWidth = 1.5;
  ctx.stroke();
}

// --- Scheduler Task Table ---
function renderTaskTable(sched) {
  const card = document.getElementById('card-task-table');
  if (!sched || !sched.tasks) { card.dataset.status = 'unknown'; return; }
  card.dataset.status = sched.status === 'ok' ? 'healthy' : 'down';
  const body = document.getElementById('task-table-body');
  if (!body || body.classList.contains('collapsed')) return;

  const tasks = Object.entries(sched.tasks).map(([name, t]) => ({
    name, ...t,
    next_in: t.next_run ? Math.max(0, t.next_run - Date.now() / 1000) : null,
  }));

  const col = taskTableSort.col;
  const dir = taskTableSort.dir === 'asc' ? 1 : -1;
  tasks.sort((a, b) => {
    let av = a[col], bv = b[col];
    if (typeof av === 'string') return av.localeCompare(bv) * dir;
    return ((av || 0) - (bv || 0)) * dir;
  });

  const maxDur = Math.max(1, ...tasks.map(t => t.last_duration || 0));

  const cols = [
    { key: 'name', label: 'Job' },
    { key: 'schedule', label: 'Schedule' },
    { key: 'run_count', label: 'Runs' },
    { key: 'last_duration', label: 'Duration' },
    { key: 'consecutive_failures', label: 'Fails' },
    { key: 'next_in', label: 'Next In' },
  ];

  let html = '<table class="task-table"><thead><tr>';
  for (const c of cols) {
    const sorted = col === c.key ? ' sorted' : '';
    const arrow = col === c.key ? (dir > 0 ? ' &#9650;' : ' &#9660;') : '';
    html += `<th class="${sorted}" data-col="${c.key}">${c.label}${arrow}</th>`;
  }
  html += '</tr></thead><tbody>';

  for (const t of tasks) {
    const cls = t.running ? ' class="running"' : t.consecutive_failures > 0 ? ' class="failing"' : '';
    const durPct = Math.max(2, ((t.last_duration || 0) / maxDur) * 80);
    const durColor = (t.last_duration || 0) > 60 ? 'var(--accent-yellow)' :
                     (t.last_duration || 0) > 300 ? 'var(--accent-red)' : 'var(--accent-cyan)';
    const nextIn = t.next_in != null ? formatUptime(Math.round(t.next_in)) : '---';

    html += `<tr${cls}>
      <td>${t.running ? '&#9654; ' : ''}${escapeHtml(t.name)}</td>
      <td>${escapeHtml(t.schedule || '')}</td>
      <td>${(t.run_count || 0).toLocaleString()}</td>
      <td>${(t.last_duration || 0).toFixed(1)}s <span class="duration-bar" style="width:${durPct}px;background:${durColor}"></span></td>
      <td style="color:${t.consecutive_failures > 0 ? 'var(--accent-yellow)' : 'inherit'}">${t.consecutive_failures || 0}</td>
      <td>${nextIn}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  body.innerHTML = html;

  for (const th of body.querySelectorAll('th[data-col]')) {
    th.addEventListener('click', () => {
      const c = th.dataset.col;
      if (taskTableSort.col === c) {
        taskTableSort.dir = taskTableSort.dir === 'asc' ? 'desc' : 'asc';
      } else {
        taskTableSort = { col: c, dir: 'asc' };
      }
    });
  }
}

// --- Agent Cards ---
function renderAgent(name, agent) {
  const card = document.getElementById(`card-agent-${name}`);
  if (!card) return;
  if (!agent) { card.dataset.status = 'unknown'; return; }
  const st = statusClass(agent.status);
  card.dataset.status = st;
  const body = card.querySelector('.card-body');
  let html =
    statRow('Status', agent.status || 'unknown', st === 'healthy' ? 'green' : st === 'down' ? 'red' : '') +
    statRow('Tasks Done', agent.tasks_completed || 0) +
    statRow('Uptime', formatUptime(agent.uptime_s));
  if (agent.model) html += `<div class="model-tag">${escapeHtml(agent.model)}</div>`;
  if (agent.last_error) html += `<div class="error-text">${escapeHtml(agent.last_error.substring(0, 120))}</div>`;
  if (agent.error) html += `<div class="error-text">${escapeHtml(agent.error)}</div>`;
  body.innerHTML = html;
}

// Collapsible task table toggle
document.getElementById('task-table-toggle')?.addEventListener('click', () => {
  const body = document.getElementById('task-table-body');
  const arrow = document.querySelector('#task-table-toggle .collapse-arrow');
  if (body.classList.contains('collapsed')) {
    body.classList.remove('collapsed');
    body.classList.add('expanded');
    if (arrow) arrow.style.transform = 'rotate(0deg)';
  } else {
    body.classList.remove('expanded');
    body.classList.add('collapsed');
    if (arrow) arrow.style.transform = 'rotate(-90deg)';
  }
});

connect();
