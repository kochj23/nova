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
  renderAlerts(state.alerts);
  renderSystem(state.system);
  renderGateway(state.gateway);
  renderScheduler(state.scheduler);
  renderOllama(state.ollama);
  renderPostgresql(state.postgresql);
  renderRedis(state.redis);
  renderMemory(state);
  renderUnifi(state.unifi);
  renderModelUsage(state.model_usage);
  renderConversations(state.conversations);
  renderGatewayQueries(state.gateway_queries);
  renderTaskHistory(state.task_history);
  renderThroughput(state.task_throughput);
  renderLatency(state.services);
  renderCostTracker(state.model_usage);
  renderMemoryGrowth(state.postgresql);
  renderDiskUsage(state.system);
  renderSearxngStats(state.searxng_stats);
  renderBackupStatus(state.backup_status);
  renderResponseTime(state.response_time);
  renderHerdActivity(state.herd_activity);
  renderMlxStatus(state.mlx_status, state.services);
  renderCronHealth(state.scheduler);
  renderTokenCounter(state.model_usage);
  renderCameras(state.cameras);
  renderHomeKit(state.homekit);
  renderDeadman(state.scheduler);
  renderAppWatchdog(state.app_watchdog);
  renderChannels(state);
  renderNas(state.synology);
  renderKnowledge(state);
  renderBriefings(state.scheduler);
  renderHomebridgeCard(state.homebridge);
  renderWeather(state.weather);
  renderDream(state.dream);
  renderNmap(state.scheduler);
  renderHealthKit2(state.healthkit);
  renderTraffic(state.traffic_flow);
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

// --- Alert Banner ---
function renderAlerts(alerts) {
  const banner = document.getElementById('alert-banner');
  if (!banner) return;
  if (!alerts || alerts.length === 0) {
    banner.innerHTML = '';
    return;
  }
  let html = '';
  for (const a of alerts) {
    const sev = (a.severity || 'warning').toLowerCase();
    const cls = sev === 'critical' ? 'critical' : 'warning';
    html += `<div class="alert-item ${cls}"><span class="alert-severity">${escapeHtml(sev)}</span><span>${escapeHtml(a.message || '')}</span></div>`;
  }
  banner.innerHTML = html;
}

// --- UniFi Network ---
function renderUnifi(data) {
  const card = document.getElementById('card-unifi');
  if (!card) return;
  if (!data) { card.dataset.status = 'unknown'; return; }
  card.dataset.status = data.status === 'ok' ? 'healthy' : data.status === 'no_key' ? 'unknown' : 'down';
  const body = card.querySelector('.card-body');
  body.innerHTML =
    statRow('Devices', data.device_count || 0, 'cyan') +
    statRow('Clients', data.client_count || 0, 'green') +
    statRow('WAN Uptime', data.wan_uptime ? formatUptime(data.wan_uptime) : '---', 'cyan') +
    (data.error ? `<div class="error-text">${escapeHtml(data.error)}</div>` : '');
}

// --- Conversation Activity ---
function renderConversations(data) {
  const card = document.getElementById('card-conversations');
  if (!card) return;
  if (!data) { card.dataset.status = 'unknown'; return; }
  card.dataset.status = data.active_sessions > 0 ? 'healthy' : 'degraded';
  const body = card.querySelector('.card-body');
  let html = statRow('Active Sessions', data.active_sessions || 0, 'cyan');
  if (data.by_channel) {
    for (const [ch, count] of Object.entries(data.by_channel)) {
      html += statRow(ch, count);
    }
  }
  if (data.sessions?.length) {
    html += '<div style="margin-top:8px">';
    for (const s of data.sessions) {
      html += `<div class="pg-table-row"><span class="pg-table-name">${escapeHtml(s.label || s.id || '?')}</span><span class="pg-table-rows">${escapeHtml(s.channel || '')}</span></div>`;
    }
    html += '</div>';
  }
  body.innerHTML = html;
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

// --- Scheduler Task Table (card-grid layout) ---
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

  tasks.sort((a, b) => {
    if (a.running && !b.running) return -1;
    if (!a.running && b.running) return 1;
    if (a.consecutive_failures > 0 && b.consecutive_failures <= 0) return -1;
    if (a.consecutive_failures <= 0 && b.consecutive_failures > 0) return 1;
    return (b.run_count || 0) - (a.run_count || 0);
  });

  const maxDur = Math.max(1, ...tasks.map(t => t.last_duration || 0));

  let html = '<div class="job-grid">';
  for (const t of tasks) {
    const status = t.running ? 'running' : t.consecutive_failures > 0 ? 'failing' :
                   !t.enabled ? 'disabled' : 'ok';
    const borderColor = status === 'running' ? 'var(--accent-cyan)' :
                        status === 'failing' ? 'var(--accent-yellow)' :
                        status === 'disabled' ? 'var(--text-dim)' : 'var(--border-glow)';
    const durPct = Math.min(100, ((t.last_duration || 0) / maxDur) * 100);
    const durColor = (t.last_duration || 0) > 300 ? 'red' :
                     (t.last_duration || 0) > 60 ? 'yellow' : 'cyan';
    const nextIn = t.next_in != null ? formatUptime(Math.round(t.next_in)) : '---';

    html += `<div class="job-card" style="border-top-color:${borderColor}">
      <div class="job-card-header">
        <span class="job-name">${t.running ? '<span style="color:var(--accent-cyan)">&#9654; </span>' : ''}${escapeHtml(t.name)}</span>
        <span class="model-tag">${escapeHtml(t.schedule || '?')}</span>
      </div>
      ${statRow('Runs', (t.run_count || 0).toLocaleString(), 'cyan')}
      ${statRow('Duration', (t.last_duration || 0).toFixed(1) + 's', durColor)}
      <div class="progress-bar-track"><div class="progress-bar-fill ${durColor}" style="width:${durPct}%"></div></div>
      ${t.consecutive_failures > 0 ? statRow('Failures', t.consecutive_failures + 'x consecutive', 'yellow') : ''}
      ${statRow('Next', nextIn)}
      ${t.last_exit_code !== 0 && t.last_exit_code != null ? statRow('Exit Code', t.last_exit_code, 'red') : ''}
    </div>`;
  }
  html += '</div>';
  body.innerHTML = html;
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

// --- Token counter delta tracking ---
let _prevTokens = null;
let _prevTokenTs = null;

// --- Card 1: OpenRouter Cost Tracker ---
function renderCostTracker(mu) {
  const card = document.getElementById('card-cost-tracker');
  if (!card || !mu) return;
  const or_data = mu.by_provider?.openrouter;
  card.dataset.status = or_data ? 'healthy' : 'unknown';
  const body = card.querySelector('.card-body');
  const cost = or_data?.cost || 0;
  const sessions = or_data?.sessions || 0;
  const tokens = (or_data?.input_tokens || 0) + (or_data?.output_tokens || 0);
  body.innerHTML =
    statRow('Cost', '$' + cost.toFixed(4), cost > 1 ? 'red' : cost > 0.1 ? 'yellow' : 'green') +
    statRow('Sessions', sessions, 'cyan') +
    statRow('Tokens', tokens.toLocaleString()) +
    statRow('Total Cost', '$' + (mu.total_cost_usd || 0).toFixed(4), 'magenta');
}

// --- Card 2: Memory Growth ---
function renderMemoryGrowth(pg) {
  const card = document.getElementById('card-memory-growth');
  if (!card || !pg) return;
  card.dataset.status = pg.status === 'ok' ? 'healthy' : 'down';
  const body = card.querySelector('.card-body');
  const total = pg.total_rows || 0;
  body.innerHTML =
    statRow('Total Memories', total.toLocaleString(), 'cyan') +
    statRow('Database', pg.db_size_gb + ' GB', 'magenta') +
    statRow('Tables', pg.tables?.length || 0);
}

// --- Card 3: Disk Usage ---
function renderDiskUsage(sys) {
  const card = document.getElementById('card-disk-usage');
  if (!card || !sys) return;
  const disks = sys.disks || {};
  const hasRed = Object.values(disks).some(d => d.percent > 90);
  const hasYellow = Object.values(disks).some(d => d.percent > 80);
  card.dataset.status = hasRed ? 'down' : hasYellow ? 'degraded' : 'healthy';
  const body = card.querySelector('.card-body');
  let html = '';
  for (const [mount, d] of Object.entries(disks)) {
    const label = mount === '/' || mount === '/System/Volumes/Data' ? 'SSD' :
                  mount.replace('/Volumes/', '');
    const color = d.percent > 90 ? 'red' : d.percent > 80 ? 'yellow' : 'green';
    html += `<div class="disk-usage-item">` +
      statRow(label, `${d.free_gb} GB free (${d.percent}%)`, color) +
      progressBar(d.percent, [80, 90]) +
      `<div class="disk-detail">${d.used_gb}/${d.total_gb} GB used</div></div>`;
  }
  if (!html) html = '<p class="dim">No disk data</p>';
  body.innerHTML = html;
}

// --- Card 4: SearXNG Stats ---
function renderSearxngStats(data) {
  const card = document.getElementById('card-searxng-stats');
  if (!card) return;
  if (!data) { card.dataset.status = 'unknown'; return; }
  card.dataset.status = data.status === 'up' ? 'healthy' : data.status === 'down' ? 'down' : 'unknown';
  const body = card.querySelector('.card-body');
  body.innerHTML =
    statRow('Status', data.status || 'unknown', data.status === 'up' ? 'green' : 'red') +
    statRow('Engines', data.engine_count || 0, 'cyan') +
    (data.error ? `<div class="error-text">${escapeHtml(data.error)}</div>` : '');
}

// --- Card 5: Backup Status ---
function renderBackupStatus(data) {
  const card = document.getElementById('card-backup-status');
  if (!card) return;
  if (!data) { card.dataset.status = 'unknown'; return; }
  const ok = data.status === 'ok';
  const stale = data.stale;
  card.dataset.status = ok ? 'healthy' : data.status === 'warning' ? 'degraded' : 'down';
  const body = card.querySelector('.card-body');
  let timeStr = data.last_backup || 'Never';
  if (data.last_backup) {
    try {
      const dt = new Date(data.last_backup.replace(' ', 'T'));
      const ago = Math.floor((Date.now() - dt.getTime()) / 1000);
      timeStr = formatUptime(ago) + ' ago';
    } catch(e) { timeStr = data.last_backup; }
  }
  body.innerHTML =
    statRow('Last Backup', timeStr, ok ? 'green' : stale ? 'yellow' : 'red') +
    statRow('Result', data.success ? 'PASS' : 'FAIL', data.success ? 'green' : 'red') +
    (data.size ? statRow('Size', data.size) : '') +
    (data.status === 'no_log' ? '<div class="dim" style="margin-top:4px">No backup log found</div>' : '');
}

// --- Card 6: Nova Response Time ---
function renderResponseTime(data) {
  const card = document.getElementById('card-response-time');
  if (!card) return;
  if (!data) { card.dataset.status = 'unknown'; return; }
  card.dataset.status = data.status === 'ok' ? 'healthy' : 'degraded';
  const body = card.querySelector('.card-body');
  body.innerHTML =
    statRow('Replies Today', data.replies_today || 0, 'cyan') +
    statRow('Avg/Hour', data.avg_per_hour || 0, 'green') +
    (data.status === 'no_log' ? '<div class="dim" style="margin-top:4px">No gateway log</div>' : '');
}

// --- Card 7: Herd Activity ---
function renderHerdActivity(data) {
  const card = document.getElementById('card-herd-activity');
  if (!card) return;
  if (!data) { card.dataset.status = 'unknown'; return; }
  card.dataset.status = data.status === 'ok' ? 'healthy' : 'degraded';
  const body = card.querySelector('.card-body');
  const channels = data.today || data.channels || {};
  const colors = { slack: 'magenta', discord: 'blue', signal: 'green' };
  let html = '';
  for (const [ch, count] of Object.entries(channels)) {
    html += statRow(ch, count, colors[ch] || 'cyan');
  }
  html += statRow('Total', data.total_events || 0, 'cyan');
  body.innerHTML = html;
}

// --- Card 8: MLX Server Status ---
function renderMlxStatus(data, services) {
  const card = document.getElementById('card-mlx-status');
  if (!card) return;
  if (!data) { card.dataset.status = 'unknown'; return; }
  const svcStatus = services?.mlx_chat?.status || data.status || 'unknown';
  card.dataset.status = svcStatus === 'up' ? 'healthy' : 'down';
  const body = card.querySelector('.card-body');
  body.innerHTML =
    statRow('Status', svcStatus, svcStatus === 'up' ? 'green' : 'red') +
    statRow('Model', data.model || '?', 'cyan') +
    (data.error ? `<div class="error-text">${escapeHtml(data.error)}</div>` : '');
}

// --- Card 9: Cron Job Health ---
function renderCronHealth(sched) {
  const card = document.getElementById('card-cron-health');
  if (!card) return;
  if (!sched || !sched.tasks) { card.dataset.status = 'unknown'; return; }
  card.dataset.status = sched.status === 'ok' ? 'healthy' : 'down';
  const body = card.querySelector('.card-body');
  const tasks = sched.tasks;
  let okCount = 0, failCount = 0, neverCount = 0;
  let dots = '';
  for (const [name, t] of Object.entries(tasks).sort((a, b) => a[0].localeCompare(b[0]))) {
    let cls = 'cron-dot-ok';
    if (!t.enabled) {
      cls = 'cron-dot-gray';
    } else if (t.consecutive_failures > 0 && t.run_count > 0) {
      cls = t.consecutive_failures > 2 ? 'cron-dot-red' : 'cron-dot-yellow';
      failCount++;
    } else if (t.run_count === 0) {
      cls = 'cron-dot-gray';
      neverCount++;
    } else {
      okCount++;
    }
    dots += `<span class="cron-dot ${cls}" title="${escapeHtml(name)}: ${t.run_count || 0} runs, ${t.consecutive_failures || 0} failures"></span>`;
  }
  const total = Object.keys(tasks).length;
  body.innerHTML =
    `<div class="cron-dot-grid">${dots}</div>` +
    `<div style="margin-top:8px;font-size:11px;color:var(--text-dim)">${okCount} OK / ${failCount} failing / ${neverCount} never</div>`;
}

// --- Card 10: Live Token Counter ---
function renderTokenCounter(mu) {
  const card = document.getElementById('card-token-counter');
  if (!card || !mu) return;
  card.dataset.status = mu.status === 'ok' ? 'healthy' : 'unknown';
  const body = card.querySelector('.card-body');
  const totalIn = Object.values(mu.by_provider || {}).reduce((a, p) => a + (p.input_tokens || 0), 0);
  const totalOut = Object.values(mu.by_provider || {}).reduce((a, p) => a + (p.output_tokens || 0), 0);
  const total = totalIn + totalOut;
  let rateStr = '---';
  const now = Date.now() / 1000;
  if (_prevTokens !== null && _prevTokenTs !== null) {
    const dt = now - _prevTokenTs;
    if (dt > 0) {
      const delta = total - _prevTokens;
      const rate = Math.max(0, delta / dt);
      rateStr = rate.toFixed(0) + ' tok/s';
    }
  }
  _prevTokens = total;
  _prevTokenTs = now;
  body.innerHTML =
    statRow('IN', totalIn.toLocaleString(), 'green') +
    statRow('OUT', totalOut.toLocaleString(), 'magenta') +
    statRow('Total', total.toLocaleString(), 'cyan') +
    statRow('Rate', rateStr);
}

// --- Card 11: Camera Activity ---
function renderCameras(data) {
  const card = document.getElementById('card-cameras');
  if (!card) return;
  if (!data) { card.dataset.status = 'unknown'; return; }
  if (data.status === 'no_file') {
    card.dataset.status = 'unknown';
    card.querySelector('.card-body').innerHTML = '<p class="dim">No camera state file</p>';
    return;
  }
  card.dataset.status = data.disconnected > 0 ? 'degraded' : data.total > 0 ? 'healthy' : 'unknown';
  const body = card.querySelector('.card-body');
  let html = statRow('Cameras', data.total || 0, 'cyan') +
    statRow('Connected', data.connected || 0, 'green') +
    statRow('Disconnected', data.disconnected || 0, data.disconnected > 0 ? 'red' : 'green');
  if (data.cameras?.length) {
    html += '<div class="camera-list">';
    for (const c of data.cameras) {
      const dotCls = c.connected ? 'cam-dot-on' : 'cam-dot-off';
      html += `<div class="camera-item"><span class="cam-dot ${dotCls}"></span><span>${escapeHtml(c.name || '?')}</span></div>`;
    }
    html += '</div>';
  }
  body.innerHTML = html;
}

// --- Card 12: HomeKit Scenes ---
function renderHomeKit(data) {
  const card = document.getElementById('card-homekit');
  if (!card) return;
  if (!data) { card.dataset.status = 'unknown'; return; }
  if (data.status === 'unavailable') {
    card.dataset.status = 'unknown';
    card.querySelector('.card-body').innerHTML = '<p class="dim">Not configured</p>';
    return;
  }
  card.dataset.status = data.status === 'ok' ? 'healthy' : 'degraded';
  const body = card.querySelector('.card-body');
  body.innerHTML =
    statRow('Scenes', data.scene_count || 0, 'cyan') +
    statRow('Accessories', data.accessory_count || 0, 'green') +
    (data.error ? `<div class="error-text">${escapeHtml(data.error)}</div>` : '');
}

// --- 12 New Dashboard Cards ---

// 1. Dead Man's Switch
function renderDeadman(sched) {
  const card = document.getElementById('card-deadman');
  if (!card || !sched || !sched.tasks) return;
  const dms = sched.tasks.dead_mans_switch || sched.tasks.dead_man_switch;
  if (!dms) { card.dataset.status = 'unknown'; card.querySelector('.card-body').innerHTML = '<p class="dim">No dead man\'s switch task</p>'; return; }

  const lastRun = dms.last_run || 0;
  const ageS = lastRun > 0 ? (Date.now() / 1000 - lastRun) : Infinity;
  const ageH = ageS / 3600;
  const failures = dms.consecutive_failures || 0;

  let status = 'healthy';
  if (ageH > 48 || failures > 0) status = 'down';
  else if (ageH > 36) status = 'degraded';

  card.dataset.status = status;
  const agoStr = lastRun > 0 ? formatUptime(Math.round(ageS)) + ' ago' : 'Never';
  const body = card.querySelector('.card-body');
  body.innerHTML =
    statRow('Last Fired', agoStr, status === 'healthy' ? 'green' : status === 'degraded' ? 'yellow' : 'red') +
    statRow('Status', failures > 0 ? 'FAIL' : 'PASS', failures > 0 ? 'red' : 'green') +
    statRow('Streak', (dms.run_count || 0) + ' runs', 'cyan') +
    (failures > 0 ? statRow('Failures', failures + 'x', 'red') : '');
}

// 2. App Ports / Watchdog
function renderAppWatchdog(data) {
  const card = document.getElementById('card-app-watchdog');
  if (!card) return;
  if (!data) { card.dataset.status = 'unknown'; return; }
  card.dataset.status = data.status === 'ok' ? 'healthy' : data.status === 'degraded' ? 'degraded' : 'down';
  const body = card.querySelector('.card-body');
  let html = statRow('Apps', (data.up_count || 0) + '/' + (data.total || 0) + ' up',
    data.up_count === data.total ? 'green' : 'yellow');

  if (data.apps?.length) {
    html += '<div style="margin-top:6px">';
    for (const app of data.apps) {
      const dotCls = app.alive ? 'green' : 'red';
      const upStr = app.alive && app.uptime_s > 0 ? formatUptime(Math.round(app.uptime_s)) : '';
      html += `<div class="stat-row">
        <span class="stat-label"><span style="color:var(--accent-${dotCls});font-size:8px">&#9679;</span> ${escapeHtml(app.name)} <span class="dim" style="font-size:9px">:${escapeHtml(app.port)}</span></span>
        <span class="stat-value ${dotCls}">${app.alive ? 'UP' : 'DOWN'}${upStr ? ' <span class="dim" style="font-size:9px">' + upStr + '</span>' : ''}</span>
      </div>`;
    }
    html += '</div>';
  }

  if (data.recent_restarts?.length) {
    html += '<div style="margin-top:6px;font-size:10px;color:var(--text-dim)">Recent restarts:';
    for (const r of data.recent_restarts) {
      html += ` ${escapeHtml(r.app || '?')}`;
    }
    html += '</div>';
  }
  body.innerHTML = html;
}

// 3. Messaging Channels
function renderChannels(state) {
  const card = document.getElementById('card-channels');
  if (!card) return;
  const gw = state.gateway;
  const herd = state.herd_activity;
  const flows = state.flows;

  if (!gw && !herd) { card.dataset.status = 'unknown'; return; }

  const channels = [
    { name: 'Slack', key: 'slack', color: 'magenta' },
    { name: 'Discord', key: 'discord', color: 'cyan' },
    { name: 'Signal', key: 'signal', color: 'green' },
    { name: 'iMessage', key: 'imessage', color: 'yellow' },
    { name: 'Email', key: 'email', color: 'blue' },
  ];

  const todayCounts = herd?.today || {};
  const services = state.services || {};
  let upCount = 0;

  let html = '<div style="display:flex;flex-wrap:wrap;gap:8px">';
  for (const ch of channels) {
    const svc = services[ch.key];
    const isUp = svc?.status === 'up';
    if (isUp) upCount++;
    const msgCount = todayCounts[ch.key] || 0;
    const dotColor = isUp ? 'green' : 'red';

    html += `<div style="flex:1;min-width:80px;padding:4px 0">
      <div class="stat-row">
        <span class="stat-label"><span style="color:var(--accent-${dotColor});font-size:8px">&#9679;</span> ${ch.name}</span>
        <span class="stat-value ${ch.color}">${msgCount}</span>
      </div>
    </div>`;
  }
  html += '</div>';

  card.dataset.status = upCount >= 3 ? 'healthy' : upCount >= 1 ? 'degraded' : 'down';
  card.querySelector('.card-body').innerHTML = html;
}

// 4. Synology NAS
function renderNas(data) {
  const card = document.getElementById('card-nas');
  if (!card) return;
  if (!data || data.status === 'unavailable') {
    card.dataset.status = 'unknown';
    card.querySelector('.card-body').innerHTML = '<p class="dim">No NAS data</p>';
    return;
  }
  card.dataset.status = data.status === 'ok' ? (data.problem_count > 0 ? 'degraded' : 'healthy') : 'down';
  const body = card.querySelector('.card-body');
  const ramColor = (data.ram_pct || 0) > 90 ? 'red' : (data.ram_pct || 0) > 75 ? 'yellow' : 'green';

  let lastCheck = data.last_check || '---';
  if (lastCheck && lastCheck.includes('T')) {
    try {
      const dt = new Date(lastCheck);
      const ago = Math.floor((Date.now() - dt.getTime()) / 1000);
      lastCheck = formatUptime(ago) + ' ago';
    } catch(e) {}
  }

  body.innerHTML =
    statRow('Status', data.status === 'ok' ? 'Awake' : 'Down', data.status === 'ok' ? 'green' : 'red') +
    statRow('Model', data.model || '?', 'cyan') +
    statRow('Firmware', data.firmware || '?') +
    statRow('RAM', (data.ram_pct || 0) + '%', ramColor) +
    progressBar(data.ram_pct || 0, [75, 90]) +
    statRow('Volumes', data.volumes || '?') +
    statRow('Last Check', lastCheck) +
    (data.problem_count > 0 ? statRow('Problems', data.problem_count, 'red') : '');
}

// 5. Knowledge Pipeline
function renderKnowledge(state) {
  const card = document.getElementById('card-knowledge');
  if (!card) return;
  const sched = state.scheduler;
  const pg = state.postgresql;
  if (!sched) { card.dataset.status = 'unknown'; return; }

  const tasks = sched.tasks || {};
  const knowledgeTasks = ['reddit_ingest', 'sam_blog_ingest', 'context_bridge_am', 'context_bridge_pm', 'this_day'];
  let failCount = 0;
  let lastRunTs = 0;

  let html = '';
  if (pg) {
    html += statRow('Today Ingested', (pg.today_count || 0).toLocaleString(), 'cyan');
    if (pg.today_sources?.length) {
      const top3 = pg.today_sources.slice(0, 3);
      html += statRow('Top Sources', top3.map(s => s.source + '(' + s.count + ')').join(', '), 'green');
    }
  }

  html += '<div style="margin-top:6px">';
  for (const name of knowledgeTasks) {
    const t = tasks[name];
    if (!t) continue;
    const ok = (t.consecutive_failures || 0) === 0 && (t.run_count || 0) > 0;
    if (!ok && t.run_count > 0) failCount++;
    const lr = t.last_run || 0;
    if (lr > lastRunTs) lastRunTs = lr;
    const agoStr = lr > 0 ? formatUptime(Math.round(Date.now() / 1000 - lr)) + ' ago' : 'never';
    const dotColor = ok ? 'green' : (t.run_count || 0) === 0 ? 'dim' : 'red';
    html += `<div class="stat-row">
      <span class="stat-label"><span style="color:var(--accent-${dotColor});font-size:8px">&#9679;</span> ${escapeHtml(name)}</span>
      <span class="stat-value" style="font-size:10px">${agoStr}</span>
    </div>`;
  }
  html += '</div>';

  card.dataset.status = failCount > 0 ? 'degraded' : 'healthy';
  card.querySelector('.card-body').innerHTML = html;
}

// 6. Briefings & Reports
function renderBriefings(sched) {
  const card = document.getElementById('card-briefings');
  if (!card || !sched) return;
  const tasks = sched.tasks || {};
  const briefingNames = ['morning_brief', 'nightly_report', 'daily_journal', 'weekly_journal', 'dream_pipeline'];
  let failCount = 0;

  let html = '';
  for (const name of briefingNames) {
    const t = tasks[name];
    if (!t) continue;
    const ok = (t.consecutive_failures || 0) === 0 && (t.run_count || 0) > 0;
    if (!ok && t.run_count > 0) failCount++;
    const lr = t.last_run || 0;
    const agoStr = lr > 0 ? formatUptime(Math.round(Date.now() / 1000 - lr)) + ' ago' : 'never';
    const dotColor = ok ? 'green' : (t.run_count || 0) === 0 ? 'dim' : 'red';
    const displayName = name.replace(/_/g, ' ');
    html += `<div class="stat-row">
      <span class="stat-label"><span style="color:var(--accent-${dotColor});font-size:8px">&#9679;</span> ${escapeHtml(displayName)}</span>
      <span class="stat-value" style="font-size:10px">${agoStr}</span>
    </div>`;
  }

  card.dataset.status = failCount > 0 ? 'degraded' : 'healthy';
  card.querySelector('.card-body').innerHTML = html || '<p class="dim">No briefing tasks found</p>';
}

// 7. Homebridge
function renderHomebridgeCard(data) {
  const card = document.getElementById('card-homebridge');
  if (!card) return;
  if (!data) { card.dataset.status = 'unknown'; return; }
  card.dataset.status = data.status === 'ok' ? 'healthy' : data.status === 'degraded' ? 'degraded' : 'down';
  const body = card.querySelector('.card-body');
  body.innerHTML =
    statRow('Status', data.status === 'ok' ? 'Online' : data.status === 'degraded' ? 'Partial' : 'Offline',
      data.status === 'ok' ? 'green' : data.status === 'degraded' ? 'yellow' : 'red') +
    statRow('LaunchAgent', data.launchd ? 'Running' : 'Stopped', data.launchd ? 'green' : 'red') +
    statRow('Port 8581', data.port_reachable ? 'Reachable' : 'Unreachable', data.port_reachable ? 'green' : 'red') +
    (data.pid ? statRow('PID', data.pid, 'cyan') : '') +
    (data.error ? `<div class="error-text">${escapeHtml(data.error)}</div>` : '');
}

// 8. Weather / Sky
function renderWeather(data) {
  const card = document.getElementById('card-weather');
  if (!card) return;
  if (!data || data.status === 'unavailable') {
    card.dataset.status = 'unknown';
    card.querySelector('.card-body').innerHTML = '<p class="dim">No weather data</p>';
    return;
  }
  card.dataset.status = data.status === 'ok' ? 'healthy' : 'degraded';
  const body = card.querySelector('.card-body');
  let html = '';

  if (data.temp_f) html += statRow('Temperature', data.temp_f + '°F', 'cyan');
  if (data.conditions) html += statRow('Conditions', data.conditions, 'green');
  if (data.moon_phase) html += statRow('Moon', data.moon_phase);
  if (data.frames_today) html += statRow('Sky Frames', data.frames_today, 'cyan');
  if (data.sessions_today?.length) {
    html += statRow('Sessions', data.sessions_today.join(', '));
  }
  if (data.last_capture) {
    let captureStr = data.last_capture;
    try {
      const dt = new Date(captureStr);
      const ago = Math.floor((Date.now() - dt.getTime()) / 1000);
      captureStr = formatUptime(ago) + ' ago';
    } catch(e) {}
    html += statRow('Last Capture', captureStr);
  }
  if (!html) html = '<p class="dim">Weather data partial</p>';
  body.innerHTML = html;
}

// 9. Dream Pipeline
function renderDream(data) {
  const card = document.getElementById('card-dream');
  if (!card) return;
  if (!data || data.status === 'unavailable') {
    card.dataset.status = 'unknown';
    card.querySelector('.card-body').innerHTML = '<p class="dim">No dream data</p>';
    return;
  }
  card.dataset.status = data.status === 'ok' ? 'healthy' : data.status === 'degraded' ? 'degraded' : 'down';
  const body = card.querySelector('.card-body');

  let lastRunStr = '---';
  if (data.last_run > 0) {
    const ago = Math.floor(Date.now() / 1000 - data.last_run);
    lastRunStr = formatUptime(ago) + ' ago';
  }

  let html =
    statRow('Last Run', lastRunStr, (data.consecutive_failures || 0) > 0 ? 'red' : 'green') +
    statRow('Runs', data.run_count || 0, 'cyan') +
    statRow('Images', data.image_count || 0, data.has_images ? 'green' : 'dim') +
    statRow('Dream Entries', data.dream_entries || 0, 'cyan');

  if (data.last_dream_words) html += statRow('Last Words', data.last_dream_words);
  if (data.last_dream_file) html += statRow('Last File', data.last_dream_file);
  if (data.last_image_ts) {
    const imgAgo = Math.floor(Date.now() / 1000 - data.last_image_ts);
    html += statRow('Last Image', formatUptime(imgAgo) + ' ago');
  }
  if (data.consecutive_failures > 0) html += statRow('Failures', data.consecutive_failures + 'x', 'red');

  body.innerHTML = html;
}

// 10. NMAP Scan
function renderNmap(sched) {
  const card = document.getElementById('card-nmap');
  if (!card || !sched) return;
  const tasks = sched.tasks || {};
  const nmap = tasks.weekly_nmap;
  if (!nmap) {
    card.dataset.status = 'unknown';
    card.querySelector('.card-body').innerHTML = '<p class="dim">No NMAP task</p>';
    return;
  }

  const ok = (nmap.consecutive_failures || 0) === 0 && (nmap.run_count || 0) > 0;
  card.dataset.status = ok ? 'healthy' : nmap.run_count > 0 ? 'degraded' : 'unknown';

  const lr = nmap.last_run || 0;
  const agoStr = lr > 0 ? formatUptime(Math.round(Date.now() / 1000 - lr)) + ' ago' : 'Never';

  const body = card.querySelector('.card-body');
  body.innerHTML =
    statRow('Last Scan', agoStr, ok ? 'green' : 'yellow') +
    statRow('Total Runs', nmap.run_count || 0, 'cyan') +
    statRow('Schedule', nmap.schedule || '?') +
    statRow('Duration', (nmap.last_duration || 0).toFixed(1) + 's') +
    (nmap.consecutive_failures > 0 ? statRow('Failures', nmap.consecutive_failures + 'x', 'red') : '');
}

// 11. HealthKit
function renderHealthKit2(data) {
  const card = document.getElementById('card-healthkit');
  if (!card) return;
  if (!data) { card.dataset.status = 'unknown'; return; }
  card.dataset.status = data.status === 'ok' ? 'healthy' : 'down';
  const body = card.querySelector('.card-body');
  body.innerHTML =
    statRow('Status', data.running ? 'Running' : 'Stopped', data.running ? 'green' : 'red') +
    statRow('LaunchAgent', data.running ? 'Active' : 'Inactive', data.running ? 'green' : 'red') +
    (data.last_sync ? statRow('Last Sync', data.last_sync) : statRow('Last Sync', 'Unknown', 'dim')) +
    (data.error ? `<div class="error-text">${escapeHtml(data.error)}</div>` : '');
}

// 12. Traffic Overview
function renderTraffic(traffic) {
  const card = document.getElementById('card-traffic');
  if (!card) return;
  if (!traffic || typeof traffic !== 'object') { card.dataset.status = 'unknown'; return; }

  // traffic_flow is a dict of node -> flow rate
  const nodes = Object.entries(traffic)
    .filter(([k, v]) => typeof v === 'number' && v > 0)
    .sort((a, b) => b[1] - a[1]);

  if (nodes.length === 0) {
    card.dataset.status = 'unknown';
    card.querySelector('.card-body').innerHTML = '<p class="dim">No traffic data</p>';
    return;
  }

  const totalFlow = nodes.reduce((a, [_, v]) => a + v, 0);
  card.dataset.status = totalFlow > 0 ? 'healthy' : 'degraded';

  let html = statRow('Total Flow', totalFlow.toFixed(3), 'cyan');
  const top5 = nodes.slice(0, 5);
  html += '<div style="margin-top:6px">';
  for (const [node, rate] of top5) {
    const pct = (rate / totalFlow * 100).toFixed(0);
    html += `<div class="stat-row">
      <span class="stat-label">${escapeHtml(node)}</span>
      <span class="stat-value green">${rate.toFixed(3)} <span class="dim" style="font-size:9px">${pct}%</span></span>
    </div>`;
  }
  html += '</div>';

  card.querySelector('.card-body').innerHTML = html;
}

// --- Graph Node Click Handler ---
window.openNodeDetail = function(nodeId, nodeLabel) {
  const NODE_SERVICE_MAP = {
    slack: 'slack', discord: 'discord', signal: 'signal',
    imessage: 'imessage', email: 'email',
    gateway: 'gateway',
    ollama: 'ollama', openrouter: 'openrouter', searxng: 'searxng',
    mlx_chat: 'mlx_chat', tinychat: 'tinychat', openwebui: 'openwebui',
    swarmui: 'swarmui', comfyui: 'comfyui',
    redis: 'redis', postgresql: 'postgresql',
    memory_server: 'memory_server', scheduler: 'scheduler',
    unifi: 'unifi',
  };
  const svc = NODE_SERVICE_MAP[nodeId];
  if (svc) fetchDetail(svc, nodeLabel);
};

// --- Detail Modal ---
const CARD_SERVICE_MAP = {
  'card-gateway': 'gateway', 'card-scheduler': 'scheduler',
  'card-system': 'system', 'card-ollama': 'ollama',
  'card-postgresql': 'postgresql', 'card-redis': 'redis',
  'card-memory': 'memory', 'card-task-history': 'task_history',
  'card-model-usage': 'model_usage',
  'card-gateway-queries': 'gateway_queries',
  'card-latency': 'latency',
  'card-throughput': 'throughput',
  'card-unifi': 'unifi', 'card-conversations': 'conversations',
  'card-agent-analyst': 'agent-analyst', 'card-agent-sentinel': 'agent-sentinel',
  'card-agent-coder': 'agent-coder', 'card-agent-lookout': 'agent-lookout',
  'card-agent-librarian': 'agent-librarian',
  'card-cost-tracker': 'cost_tracker', 'card-memory-growth': 'memory_growth',
  'card-disk-usage': 'disk_usage', 'card-searxng-stats': 'searxng_stats',
  'card-backup-status': 'backup_status', 'card-response-time': 'response_time',
  'card-herd-activity': 'herd_activity', 'card-mlx-status': 'mlx_status',
  'card-cron-health': 'cron_health', 'card-token-counter': 'token_counter',
  'card-cameras': 'cameras', 'card-homekit': 'homekit',
  'card-deadman': 'deadman', 'card-app-watchdog': 'app_watchdog',
  'card-channels': 'channels', 'card-nas': 'synology',
  'card-knowledge': 'knowledge', 'card-briefings': 'briefings',
  'card-homebridge': 'homebridge', 'card-weather': 'weather',
  'card-dream': 'dream', 'card-nmap': 'nmap',
  'card-healthkit': 'healthkit_status', 'card-traffic': 'traffic',
};

function openModal(title, html) {
  document.getElementById('modal-title').textContent = title;
  document.getElementById('modal-body').innerHTML = html;
  document.getElementById('modal-overlay').classList.add('active');
}

function closeModal() {
  document.getElementById('modal-overlay').classList.remove('active');
}

document.getElementById('modal-close').addEventListener('click', closeModal);
document.getElementById('modal-overlay').addEventListener('click', (e) => {
  if (e.target === e.currentTarget) closeModal();
});
/* keydown handler moved to keyboard shortcuts section below */

function fetchDetail(service, title) {
  openModal(title, '<p class="dim">Loading...</p>');
  fetch(`/api/detail/${service}`)
    .then(r => r.json())
    .then(data => {
      if (data.error) { openModal(title, `<div class="error-text">${escapeHtml(data.error)}</div>`); return; }
      const renderer = DETAIL_RENDERERS[service];
      if (renderer) {
        openModal(title, renderer(data));
        requestAnimationFrame(() => loadModalCharts(document.getElementById('modal-body')));
      } else {
        openModal(title, `<pre style="color:var(--text-dim);font-size:10px;white-space:pre-wrap">${escapeHtml(JSON.stringify(data, null, 2))}</pre>`);
      }
    })
    .catch(err => openModal(title, `<div class="error-text">${escapeHtml(err.message)}</div>`));
}

document.getElementById('cards-section').addEventListener('click', (e) => {
  const card = e.target.closest('.card');
  if (!card) return;
  if (e.target.closest('.collapsible-header')) return;
  if (e.target.closest('th')) return;
  const svc = CARD_SERVICE_MAP[card.id];
  if (!svc) return;
  const title = card.querySelector('.card-title')?.textContent?.replace(/[▲▼]/g, '').trim() || svc;
  fetchDetail(svc, title);
});

const DETAIL_RENDERERS = {
  postgresql(d) {
    let h = statRow('Total Memories', (d.total || 0).toLocaleString(), 'cyan') +
      statRow('Stored Today', (d.today || 0).toLocaleString(), 'green') +
      statRow('Stored Yesterday', (d.yesterday || 0).toLocaleString()) +
      statRow('This Week', (d.this_week || 0).toLocaleString()) +
      statRow('Database Size', formatBytesDetail(d.db_size), 'magenta') +
      statRow('Index Size', formatBytesDetail(d.index_size)) +
      statRow('Table Size', formatBytesDetail(d.table_size));

    if (d.daily_counts?.length) {
      h += '<div class="modal-section"><div class="modal-section-title">Daily Ingestion (7 days)</div>';
      const max = Math.max(1, ...d.daily_counts.map(x => x.count));
      for (const dc of d.daily_counts) {
        const pct = (dc.count / max * 100).toFixed(0);
        h += `<div class="stat-row"><span class="stat-label">${dc.date}</span><span class="stat-value">${dc.count.toLocaleString()}</span></div>` +
          `<div class="progress-bar-track"><div class="progress-bar-fill cyan" style="width:${pct}%"></div></div>`;
      }
      h += '</div>';
    }

    if (d.today_sources?.length) {
      h += '<div class="modal-section"><div class="modal-section-title">Today\'s Sources</div><table class="modal-table"><thead><tr><th>Source</th><th>Count</th></tr></thead><tbody>';
      for (const s of d.today_sources) h += `<tr><td>${escapeHtml(s.source)}</td><td>${s.count.toLocaleString()}</td></tr>`;
      h += '</tbody></table></div>';
    }

    if (d.top_sources?.length) {
      h += '<div class="modal-section"><div class="modal-section-title">All-Time Top Sources</div><table class="modal-table"><thead><tr><th>Source</th><th>Count</th></tr></thead><tbody>';
      for (const s of d.top_sources) h += `<tr><td>${escapeHtml(s.source)}</td><td>${s.count.toLocaleString()}</td></tr>`;
      h += '</tbody></table></div>';
    }

    h += '<div class="modal-section"><div class="modal-section-title">Trends</div>';
    h += '<div class="time-range-btns" data-metric="memories"><button class="time-range-btn active" data-range="6h">6h</button><button class="time-range-btn" data-range="24h">24h</button><button class="time-range-btn" data-range="7d">7d</button></div>';
    h += '<canvas class="modal-trend-chart" data-metric="memories" height="140"></canvas></div>';
    return h;
  },

  redis(d) {
    const hitRate = d.hit_rate || 0;
    let h = statRow('Version', d.redis_version, 'cyan') +
      statRow('Memory Used', d.memory_used) +
      statRow('Memory Peak', d.memory_peak) +
      statRow('Max Memory', d.max_memory) +
      statRow('Uptime', formatUptime(d.uptime_seconds)) +
      statRow('Connected Clients', d.connected_clients) +
      statRow('Total Commands', (d.total_commands || 0).toLocaleString(), 'green') +
      statRow('Total Connections', (d.total_connections || 0).toLocaleString()) +
      statRow('Cache Hit Rate', hitRate + '%', hitRate > 90 ? 'green' : hitRate > 70 ? 'yellow' : 'red') +
      statRow('Hits / Misses', `${(d.keyspace_hits||0).toLocaleString()} / ${(d.keyspace_misses||0).toLocaleString()}`);

    if (d.keys?.length) {
      h += '<div class="modal-section"><div class="modal-section-title">Keys</div><table class="modal-table"><thead><tr><th>Key</th><th>Type</th><th>Size</th><th>TTL</th></tr></thead><tbody>';
      for (const k of d.keys) {
        const ttl = k.ttl === -1 ? 'persistent' : k.ttl === -2 ? 'expired' : k.ttl + 's';
        h += `<tr><td style="max-width:280px;overflow:hidden;text-overflow:ellipsis">${escapeHtml(k.key)}</td><td>${k.type}</td><td>${k.size ?? '?'}</td><td>${ttl}</td></tr>`;
      }
      h += '</tbody></table></div>';
    }
    return h;
  },

  ollama(d) {
    let h = statRow('Total Models', d.model_count, 'cyan') +
      statRow('Active VRAM', d.total_vram_gb + ' GB', 'magenta');

    if (d.running?.length) {
      h += '<div class="modal-section"><div class="modal-section-title">Running Models</div>';
      for (const m of d.running) {
        h += `<div class="model-row"><div><div class="model-name">${escapeHtml(m.name)}</div>` +
          `<div class="model-detail">${m.family} &middot; ${m.params} &middot; ctx: ${m.context_length.toLocaleString()}</div></div>` +
          `<div style="text-align:right"><div class="stat-value cyan">${m.vram_gb} GB VRAM</div>` +
          `<div class="model-detail">expires: ${m.expires}</div></div></div>`;
      }
      h += '</div>';
    }

    if (d.all_models?.length) {
      h += '<div class="modal-section"><div class="modal-section-title">All Installed Models</div><table class="modal-table"><thead><tr><th>Model</th><th>Family</th><th>Params</th><th>Quant</th><th>Size</th><th>Modified</th></tr></thead><tbody>';
      for (const m of d.all_models) h += `<tr><td>${escapeHtml(m.name)}</td><td>${m.family}</td><td>${m.params}</td><td>${m.quant}</td><td>${m.size_gb}GB</td><td>${m.modified}</td></tr>`;
      h += '</tbody></table></div>';
    }
    return h;
  },

  scheduler(d) {
    let h = statRow('Status', d.info?.status || '?', 'green') +
      statRow('Uptime', formatUptime(d.info?.uptime_s)) +
      statRow('Total Tasks', d.info?.tasks_total || 0) +
      statRow('Total Runs', (d.info?.total_runs || 0).toLocaleString(), 'cyan') +
      statRow('Total Failures', d.info?.total_failures || 0, (d.info?.total_failures || 0) > 0 ? 'yellow' : 'green');

    if (d.tasks?.length) {
      h += '<div class="modal-section"><div class="modal-section-title">All Jobs</div><table class="modal-table"><thead><tr><th>Job</th><th>Schedule</th><th>Runs</th><th>Last Dur</th><th>Fails</th><th>Status</th></tr></thead><tbody>';
      for (const t of d.tasks) {
        const cls = t.running ? 'style="color:var(--accent-cyan)"' : t.consecutive_failures > 0 ? 'style="color:var(--accent-yellow)"' : '';
        h += `<tr ${cls}><td>${escapeHtml(t.name)}</td><td>${escapeHtml(t.schedule||'')}</td><td>${(t.run_count||0).toLocaleString()}</td><td>${(t.last_duration||0).toFixed(1)}s</td><td>${t.consecutive_failures||0}</td><td>${t.running ? 'RUNNING' : t.enabled ? 'enabled' : 'disabled'}</td></tr>`;
      }
      h += '</tbody></table></div>';
    }
    return h;
  },

  gateway(d) {
    const gw = d.current_state || {};
    let h = statRow('Status', gw.gateway_status || '?', gw.ok ? 'green' : 'red') +
      statRow('WebSocket', gw.ws_reachable ? 'reachable' : 'unreachable', gw.ws_reachable ? 'green' : 'red');

    if (d.recent_logs?.length) {
      h += '<div class="modal-section"><div class="modal-section-title">Recent Gateway Logs</div>' +
        `<pre style="font-size:10px;color:var(--text-dim);white-space:pre-wrap;max-height:400px;overflow-y:auto;line-height:1.6">${d.recent_logs.map(escapeHtml).join('\n')}</pre></div>`;
    }
    return h;
  },

  system(d) {
    const cur = d.current || {};
    let h = statRow('CPU Cores', `${d.cpu_count_physical} physical / ${d.cpu_count} logical`) +
      statRow('CPU Frequency', (d.cpu_freq_mhz || '?') + ' MHz') +
      statRow('Load Average', (d.load_avg || []).join(' / ')) +
      statRow('System Uptime', formatUptime(d.uptime_seconds), 'cyan') +
      statRow('Boot Time', d.boot_time || '?');

    if (cur.memory) {
      h += statRow('RAM', `${cur.memory.used_gb}/${cur.memory.total_gb} GB (${cur.memory.percent}%)`,
        cur.memory.percent > 85 ? 'red' : cur.memory.percent > 70 ? 'yellow' : 'green');
    }

    if (d.top_processes?.length) {
      h += '<div class="modal-section"><div class="modal-section-title">Top Processes by CPU</div><table class="modal-table"><thead><tr><th>PID</th><th>Name</th><th>CPU %</th><th>MEM %</th></tr></thead><tbody>';
      for (const p of d.top_processes) h += `<tr><td>${p.pid}</td><td>${escapeHtml(p.name)}</td><td>${p.cpu}%</td><td>${p.mem}%</td></tr>`;
      h += '</tbody></table></div>';
    }
    return h;
  },

  task_history(d) {
    let h = '';
    if (d.by_agent) {
      h += '<div class="modal-section-title">By Agent</div><table class="modal-table"><thead><tr><th>Agent</th><th>Succeeded</th><th>Timed Out</th><th>Failed</th><th>Lost</th></tr></thead><tbody>';
      for (const [agent, statuses] of Object.entries(d.by_agent)) {
        h += `<tr><td>${escapeHtml(agent)}</td><td style="color:var(--accent-green)">${statuses.succeeded||0}</td><td style="color:var(--accent-yellow)">${statuses.timed_out||0}</td><td style="color:var(--accent-red)">${statuses.failed||0}</td><td>${statuses.lost||0}</td></tr>`;
      }
      h += '</tbody></table>';
    }
    if (d.recent_runs?.length) {
      h += '<div class="modal-section"><div class="modal-section-title">Recent Task Runs</div><table class="modal-table"><thead><tr><th>Task</th><th>Status</th><th>Duration</th><th>Time</th></tr></thead><tbody>';
      for (const r of d.recent_runs) {
        const color = r.status === 'succeeded' ? 'var(--accent-green)' : r.status === 'timed_out' ? 'var(--accent-yellow)' : r.status === 'failed' ? 'var(--accent-red)' : '';
        h += `<tr><td>${escapeHtml(r.label)}</td><td style="color:${color}">${r.status}</td><td>${r.duration_s != null ? r.duration_s + 's' : '?'}</td><td style="font-size:10px">${r.created_at||''}</td></tr>`;
      }
      h += '</tbody></table></div>';
    }
    return h;
  },

  memory(d) {
    let h = statRow('Stored Today', (d.today_stored || 0).toLocaleString(), 'green') +
      statRow('Ingest Queue', d.ingest_queue || 0, (d.ingest_queue || 0) > 20 ? 'yellow' : 'cyan');

    if (d.tiers) {
      h += '<div class="modal-section"><div class="modal-section-title">Memory Tiers</div>';
      for (const [tier, count] of Object.entries(d.tiers)) {
        h += statRow(tier, count.toLocaleString(), tier === 'long_term' ? 'cyan' : tier === 'working' ? 'green' : 'yellow');
      }
      h += '</div>';
    }

    if (d.today_sources?.length) {
      h += '<div class="modal-section"><div class="modal-section-title">Today\'s Sources</div><table class="modal-table"><thead><tr><th>Source</th><th>Count</th></tr></thead><tbody>';
      for (const s of d.today_sources) h += `<tr><td>${escapeHtml(s.source)}</td><td>${s.count.toLocaleString()}</td></tr>`;
      h += '</tbody></table></div>';
    }

    h += '<div class="modal-section"><div class="modal-section-title">Trends</div>';
    h += '<div class="time-range-btns" data-metric="memories"><button class="time-range-btn active" data-range="6h">6h</button><button class="time-range-btn" data-range="24h">24h</button><button class="time-range-btn" data-range="7d">7d</button></div>';
    h += '<canvas class="modal-trend-chart" data-metric="memories" height="140"></canvas></div>';
    return h;
  },

  model_usage(d) {
    let h = '';
    if (d.sessions?.length) {
      h += '<table class="modal-table"><thead><tr><th>Session</th><th>Provider</th><th>Model</th><th>In Tok</th><th>Out Tok</th><th>Cost</th><th>Updated</th></tr></thead><tbody>';
      for (const s of d.sessions) {
        const isLocal = s.provider === 'ollama';
        h += `<tr><td style="max-width:140px;overflow:hidden;text-overflow:ellipsis;font-size:10px" title="${escapeHtml(s.key)}">${escapeHtml(s.label || s.key)}</td>` +
          `<td style="color:${isLocal ? 'var(--accent-green)' : 'var(--accent-magenta)'}">${s.provider}</td>` +
          `<td style="font-size:10px">${escapeHtml(s.model)}</td>` +
          `<td>${s.input_tokens.toLocaleString()}</td><td>${s.output_tokens.toLocaleString()}</td>` +
          `<td>${s.cost > 0 ? '$' + s.cost.toFixed(4) : '$0'}</td>` +
          `<td style="font-size:10px">${s.updated}</td></tr>`;
      }
      h += '</tbody></table>';
    }
    return h || '<p class="dim">No session data</p>';
  },
};

// --- Channel detail renderers ---
['slack', 'discord', 'signal', 'imessage', 'email'].forEach(ch => {
  DETAIL_RENDERERS[ch] = function(d) {
    const s = d.stats || {};
    let h = statRow('Channel', d.channel, 'cyan') +
      statRow('Total Log Events', (d.total_log_events || 0).toLocaleString()) +
      statRow('Traffic Flow', (d.traffic_flow || 0).toFixed(3), d.traffic_flow > 0.1 ? 'green' : '');

    h += '<div class="modal-section"><div class="modal-section-title">Event Summary</div>';
    h += statRow('Connected', s.connected || 0, 'green') +
      statRow('Disconnected', s.disconnected || 0, (s.disconnected || 0) > 0 ? 'yellow' : '') +
      statRow('Restarts', s.restarts || 0, (s.restarts || 0) > 5 ? 'yellow' : '') +
      statRow('Messages Delivered', s.messages_delivered || 0, 'cyan') +
      statRow('Errors', s.errors || 0, (s.errors || 0) > 0 ? 'red' : 'green') +
      statRow('Other Events', s.other || 0);
    h += '</div>';

    if (d.recent_logs?.length) {
      h += '<div class="modal-section"><div class="modal-section-title">Recent Log Lines</div>' +
        `<pre style="font-size:10px;color:var(--text-dim);white-space:pre-wrap;max-height:300px;overflow-y:auto;line-height:1.6">${d.recent_logs.map(escapeHtml).join('\n')}</pre></div>`;
    }
    return h;
  };
});

DETAIL_RENDERERS.openrouter = function(d) {
  let h = statRow('Provider', 'OpenRouter', 'magenta') +
    statRow('Sessions', d.total_sessions || 0, 'cyan') +
    statRow('Input Tokens', (d.total_input_tokens || 0).toLocaleString()) +
    statRow('Output Tokens', (d.total_output_tokens || 0).toLocaleString()) +
    statRow('Total Cost', '$' + (d.total_cost_usd || 0).toFixed(4), 'magenta');

  if (d.sessions?.length) {
    h += '<div class="modal-section"><div class="modal-section-title">Recent Sessions</div><table class="modal-table"><thead><tr><th>Session</th><th>Model</th><th>In</th><th>Out</th><th>Cost</th><th>Updated</th></tr></thead><tbody>';
    for (const s of d.sessions) {
      h += `<tr><td style="max-width:120px;overflow:hidden;text-overflow:ellipsis;font-size:10px">${escapeHtml(s.label || s.key)}</td>` +
        `<td style="font-size:10px">${escapeHtml(s.model)}</td>` +
        `<td>${s.input_tokens.toLocaleString()}</td><td>${s.output_tokens.toLocaleString()}</td>` +
        `<td>${s.cost > 0 ? '$' + s.cost.toFixed(4) : '$0'}</td>` +
        `<td style="font-size:10px">${s.updated}</td></tr>`;
    }
    h += '</tbody></table></div>';
  }

  h += '<div class="modal-section"><div class="modal-section-title">Cost Trends</div>';
  h += '<div class="time-range-btns" data-metric="costs"><button class="time-range-btn active" data-range="6h">6h</button><button class="time-range-btn" data-range="24h">24h</button><button class="time-range-btn" data-range="7d">7d</button><button class="time-range-btn" data-range="30d">30d</button></div>';
  h += '<canvas class="modal-trend-chart" data-metric="costs" height="140"></canvas></div>';
  return h;
};

// --- Service detail renderers (tinychat, mlx_chat, openwebui, comfyui, swarmui) ---
['tinychat', 'mlx_chat', 'openwebui', 'comfyui', 'swarmui'].forEach(svc => {
  DETAIL_RENDERERS[svc] = function(d) {
    const gw = d.gateway_config || {};
    let h = statRow('Service', d.service, 'cyan') +
      statRow('Status', d.status || '?', d.status === 'up' ? 'green' : 'red') +
      statRow('Port', d.port || '?');

    if (gw.model) h += statRow('Gateway Model', gw.model, 'magenta');
    if (gw.role) h += statRow('Role', gw.role);

    h += '<div class="modal-section"><div class="modal-section-title">Latency</div>';
    h += statRow('Current', d.current_latency_ms != null ? d.current_latency_ms + 'ms' : '?', 'green') +
      statRow('Average', d.avg_latency_ms != null ? d.avg_latency_ms + 'ms' : '?') +
      statRow('Min / Max', (d.min_latency_ms ?? '?') + ' / ' + (d.max_latency_ms ?? '?') + 'ms') +
      statRow('Data Points', d.latency_points || 0);
    h += '</div>';

    if (d.process && d.process.pid) {
      const p = d.process;
      h += '<div class="modal-section"><div class="modal-section-title">Process</div>';
      h += statRow('PID', p.pid) +
        statRow('Memory (RSS)', p.rss_mb + ' MB', p.rss_mb > 500 ? 'yellow' : '') +
        statRow('Virtual Memory', p.vms_mb + ' MB') +
        statRow('Threads', p.num_threads) +
        statRow('Uptime', formatUptime(p.uptime_s), 'cyan') +
        statRow('Started', p.create_time);
      if (p.cmdline) h += `<div class="model-tag" style="margin-top:4px;max-width:100%;word-break:break-all">${escapeHtml(p.cmdline)}</div>`;
      h += '</div>';
    }

    if (d.comfyui_version) {
      h += '<div class="modal-section"><div class="modal-section-title">ComfyUI Info</div>';
      h += statRow('Version', d.comfyui_version, 'cyan') +
        statRow('PyTorch', d.pytorch_version || '?') +
        statRow('Python', d.python_version || '?') +
        statRow('RAM Total', (d.ram_total_gb || 0) + ' GB') +
        statRow('RAM Free', (d.ram_free_gb || 0) + ' GB');
      h += '</div>';
    }
    return h;
  };
});

DETAIL_RENDERERS.memory_server = function(d) {
  const h1 = d.health || {};
  const s = d.stats || {};
  let h = statRow('Status', h1.status || '?', h1.status === 'ok' ? 'green' : 'red') +
    statRow('Total Memories', (h1.count || s.count || 0).toLocaleString(), 'cyan') +
    statRow('Embedding Model', h1.model || s.model || '?') +
    statRow('Backend', h1.backend || s.backend || '?') +
    statRow('Dimensions', s.dims || '?') +
    statRow('Database Size', s.db_size || '?', 'magenta') +
    statRow('Queue Length', h1.queue_length ?? s.queue_length ?? '?',
      (h1.queue_length || 0) > 20 ? 'yellow' : 'green');

  if (d.process && d.process.pid) {
    h += '<div class="modal-section"><div class="modal-section-title">Process</div>';
    h += statRow('PID', d.process.pid) +
      statRow('Memory (RSS)', d.process.rss_mb + ' MB') +
      statRow('Threads', d.process.num_threads) +
      statRow('Uptime', formatUptime(d.process.uptime_s), 'cyan');
    h += '</div>';
  }

  if (s.by_source) {
    const sources = Object.entries(s.by_source).sort((a, b) => b[1] - a[1]).slice(0, 20);
    h += '<div class="modal-section"><div class="modal-section-title">Top Sources</div><table class="modal-table"><thead><tr><th>Source</th><th>Count</th></tr></thead><tbody>';
    for (const [src, cnt] of sources) {
      h += `<tr><td>${escapeHtml(src)}</td><td>${cnt.toLocaleString()}</td></tr>`;
    }
    h += '</tbody></table></div>';
  }
  return h;
};

DETAIL_RENDERERS.gateway_queries = function(d) {
  if (d.status === 'empty') return '<p class="dim">No query data yet</p>';
  let h = statRow('Total Queries', (d.total_queries || 0).toLocaleString(), 'cyan') +
    statRow('Reqs/sec', d.reqs_per_sec || 0, d.reqs_per_sec > 0.1 ? 'green' : 'dim') +
    statRow('Reqs/min (5m avg)', d.reqs_per_min || 0, 'magenta') +
    statRow('Last Hour', (d.last_hour || 0).toLocaleString(), 'green');
  if (d.backends && Object.keys(d.backends).length > 0) {
    h += '<div class="modal-section"><div class="modal-section-title">By Backend</div><table class="modal-table"><thead><tr><th>Backend</th><th>Model</th><th>Queries</th><th>Avg Latency</th><th>Fallbacks</th></tr></thead><tbody>';
    for (const [backend, info] of Object.entries(d.backends)) {
      for (const [model, stats] of Object.entries(info.models || {})) {
        h += `<tr><td>${escapeHtml(backend)}</td><td style="font-size:10px">${escapeHtml(model)}</td><td>${stats.queries}</td><td>${stats.avg_latency_ms}ms</td><td>${stats.fallbacks || 0}</td></tr>`;
      }
    }
    h += '</tbody></table></div>';
  }
  return h;
};

DETAIL_RENDERERS.latency = function(d) {
  if (!d.services || Object.keys(d.services).length === 0) return '<p class="dim">No latency data</p>';
  let h = '<div class="modal-section"><div class="modal-section-title">Service Latency (recent samples)</div><table class="modal-table"><thead><tr><th>Service</th><th>Last</th><th>Min</th><th>Max</th><th>Avg</th><th>Samples</th></tr></thead><tbody>';
  for (const [svc, points] of Object.entries(d.services)) {
    const nums = points.filter(p => p !== null && p > 0);
    if (nums.length === 0) { h += `<tr><td>${escapeHtml(svc)}</td><td colspan="5" class="dim">no data</td></tr>`; continue; }
    const last = nums[nums.length - 1];
    const min = Math.min(...nums);
    const max = Math.max(...nums);
    const avg = Math.round(nums.reduce((a,b) => a+b, 0) / nums.length);
    h += `<tr><td>${escapeHtml(svc)}</td><td>${last}ms</td><td>${min}ms</td><td>${max}ms</td><td>${avg}ms</td><td>${nums.length}</td></tr>`;
  }
  h += '</tbody></table></div>';
  return h;
};

DETAIL_RENDERERS.throughput = function(d) {
  const hours = d.hours || [];
  if (hours.length === 0) return '<p class="dim">No throughput data</p>';
  const total = hours.reduce((a, h) => a + (h.succeeded||0) + (h.failed||0) + (h.timed_out||0), 0);
  const succeeded = hours.reduce((a, h) => a + (h.succeeded||0), 0);
  const failed = hours.reduce((a, h) => a + (h.failed||0), 0);
  const timed_out = hours.reduce((a, h) => a + (h.timed_out||0), 0);
  let h = statRow('Total Tasks (24h)', total.toLocaleString(), 'cyan') +
    statRow('Succeeded', succeeded.toLocaleString(), 'green') +
    statRow('Failed', failed.toLocaleString(), failed > 0 ? 'red' : '') +
    statRow('Timed Out', timed_out.toLocaleString(), timed_out > 0 ? 'yellow' : '') +
    statRow('Success Rate', total > 0 ? Math.round(succeeded/total*100) + '%' : '---', 'green');
  h += '<div class="modal-section"><div class="modal-section-title">Hourly Breakdown</div><table class="modal-table"><thead><tr><th>Hour</th><th>OK</th><th>Fail</th><th>Timeout</th></tr></thead><tbody>';
  for (const bucket of hours) {
    const t = (bucket.succeeded||0) + (bucket.failed||0) + (bucket.timed_out||0);
    if (t === 0) continue;
    h += `<tr><td>H-${bucket.hour}</td><td style="color:var(--accent-green)">${bucket.succeeded||0}</td><td style="color:var(--accent-red)">${bucket.failed||0}</td><td style="color:var(--accent-yellow)">${bucket.timed_out||0}</td></tr>`;
  }
  h += '</tbody></table></div>';
  return h;
};

DETAIL_RENDERERS.unifi = function(d) {
  if (d.status === 'no_key') return '<p class="dim">UniFi API key not configured in Keychain</p>';
  if (d.error) return `<div class="error-text">${escapeHtml(d.error)}</div>`;
  const uptime = d.wan_uptime_s || d.wan_uptime || 0;
  const uptimeStr = uptime > 0 ? (typeof formatUptime === 'function' ? formatUptime(uptime) : Math.round(uptime/3600) + 'h') : '---';
  let h = statRow('Status', d.status || '?', d.status === 'ok' ? 'green' : 'red') +
    statRow('Devices', d.device_count || 0, 'cyan') +
    statRow('Clients', d.client_count || 0, 'green') +
    statRow('WAN Uptime', uptimeStr, 'cyan');

  if (d.devices?.length) {
    h += '<div class="modal-section"><div class="modal-section-title">Devices</div><table class="modal-table"><thead><tr><th>Name</th><th>Model</th><th>Type</th><th>Status</th><th>IP</th><th>Clients</th></tr></thead><tbody>';
    for (const dev of d.devices) {
      const color = dev.status === 'online' ? 'var(--accent-green)' : 'var(--accent-red)';
      h += `<tr><td>${escapeHtml(dev.name || '?')}</td><td style="font-size:10px">${escapeHtml(dev.model || '')}</td><td>${escapeHtml(dev.type || '')}</td><td style="color:${color}">${dev.status || '?'}</td><td style="font-size:10px">${escapeHtml(dev.ip || '')}</td><td>${dev.num_clients || 0}</td></tr>`;
    }
    h += '</tbody></table></div>';
  }
  return h;
};

DETAIL_RENDERERS.conversations = function(d) {
  let h = statRow('Active Sessions', d.active_sessions || 0, 'cyan');
  if (d.by_channel) {
    h += '<div class="modal-section"><div class="modal-section-title">By Channel</div>';
    for (const [ch, count] of Object.entries(d.by_channel)) {
      h += statRow(ch, count);
    }
    h += '</div>';
  }
  if (d.sessions?.length) {
    h += '<div class="modal-section"><div class="modal-section-title">Active Sessions</div><table class="modal-table"><thead><tr><th>Session</th><th>Channel</th><th>Started</th><th>Messages</th></tr></thead><tbody>';
    for (const s of d.sessions) {
      h += `<tr><td>${escapeHtml(s.label || s.id || '?')}</td><td>${escapeHtml(s.channel || '')}</td><td style="font-size:10px">${escapeHtml(s.started || '')}</td><td>${s.message_count || 0}</td></tr>`;
    }
    h += '</tbody></table></div>';
  }
  return h || '<p class="dim">No conversation data</p>';
};

['agent-analyst', 'agent-sentinel', 'agent-coder', 'agent-lookout', 'agent-librarian'].forEach(a => {
  DETAIL_RENDERERS[a] = function(d) {
    let h = statRow('Status', d.status || '?', d.status === 'running' ? 'green' : 'red');
    if (d.meta) {
      for (const [k, v] of Object.entries(d.meta)) h += statRow(k, v);
    }
    if (d.recent_tasks?.length) {
      h += '<div class="modal-section"><div class="modal-section-title">Recent Tasks</div><table class="modal-table"><thead><tr><th>Task</th><th>Status</th><th>Duration</th><th>Time</th></tr></thead><tbody>';
      for (const r of d.recent_tasks) {
        const color = r.status === 'succeeded' ? 'var(--accent-green)' : r.status === 'timed_out' ? 'var(--accent-yellow)' : 'var(--accent-red)';
        h += `<tr><td>${escapeHtml(r.label)}</td><td style="color:${color}">${r.status}</td><td>${r.duration_s != null ? r.duration_s + 's' : '?'}</td><td style="font-size:10px">${r.created_at||''}</td></tr>`;
      }
      h += '</tbody></table></div>';
    }
    return h;
  };
});

// --- Detail Renderers for 12 New Cards ---

DETAIL_RENDERERS.cost_tracker = function(d) {
  let h = statRow('Today Cost', '$' + (d.today_cost || 0).toFixed(6), 'magenta') +
    statRow('Total Cost', '$' + (d.total_cost || 0).toFixed(6), 'red') +
    statRow('Today Sessions', d.today_sessions || 0, 'cyan') +
    statRow('Today Tokens', (d.today_tokens || 0).toLocaleString(), 'green');

  if (d.sessions?.length) {
    h += '<div class="modal-section"><div class="modal-section-title">Recent Sessions</div><table class="modal-table"><thead><tr><th>Session</th><th>Model</th><th>Cost</th><th>In</th><th>Out</th><th>Updated</th></tr></thead><tbody>';
    for (const s of d.sessions) {
      h += `<tr><td style="max-width:120px;overflow:hidden;text-overflow:ellipsis;font-size:10px">${escapeHtml(s.key)}</td>` +
        `<td style="font-size:10px">${escapeHtml(s.model)}</td>` +
        `<td>${s.cost > 0 ? '$' + s.cost.toFixed(4) : '$0'}</td>` +
        `<td>${(s.input_tokens||0).toLocaleString()}</td><td>${(s.output_tokens||0).toLocaleString()}</td>` +
        `<td style="font-size:10px">${s.updated}</td></tr>`;
    }
    h += '</tbody></table></div>';
  }

  h += '<div class="modal-section"><div class="modal-section-title">Cost Trends</div>';
  h += '<div class="time-range-btns" data-metric="costs"><button class="time-range-btn active" data-range="6h">6h</button><button class="time-range-btn" data-range="24h">24h</button><button class="time-range-btn" data-range="7d">7d</button></div>';
  h += '<canvas class="modal-trend-chart" data-metric="costs" height="140"></canvas></div>';
  return h;
};

DETAIL_RENDERERS.memory_growth = function(d) {
  let h = statRow('Total Memories', (d.total || 0).toLocaleString(), 'cyan');

  if (d.daily_trend?.length) {
    h += '<div class="modal-section"><div class="modal-section-title">Daily Growth (14 days)</div>';
    const max = Math.max(1, ...d.daily_trend.map(x => x.count));
    for (const dc of d.daily_trend) {
      const pct = (dc.count / max * 100).toFixed(0);
      h += statRow(dc.date, dc.count.toLocaleString()) +
        `<div class="progress-bar-track"><div class="progress-bar-fill cyan" style="width:${pct}%"></div></div>`;
    }
    h += '</div>';
  }

  if (d.by_source?.length) {
    h += '<div class="modal-section"><div class="modal-section-title">By Source</div><table class="modal-table"><thead><tr><th>Source</th><th>Count</th></tr></thead><tbody>';
    for (const s of d.by_source) h += `<tr><td>${escapeHtml(s.source)}</td><td>${s.count.toLocaleString()}</td></tr>`;
    h += '</tbody></table></div>';
  }

  h += '<div class="modal-section"><div class="modal-section-title">Trends</div>';
  h += '<div class="time-range-btns" data-metric="memories"><button class="time-range-btn active" data-range="6h">6h</button><button class="time-range-btn" data-range="24h">24h</button><button class="time-range-btn" data-range="7d">7d</button></div>';
  h += '<canvas class="modal-trend-chart" data-metric="memories" height="140"></canvas></div>';
  return h;
};

DETAIL_RENDERERS.disk_usage = function(d) {
  const disks = d.disks || {};
  if (Object.keys(disks).length === 0) return '<p class="dim">No disk data</p>';
  let h = '';
  for (const [mount, info] of Object.entries(disks)) {
    const label = mount === '/' || mount === '/System/Volumes/Data' ? 'SSD (' + mount + ')' : mount;
    const color = info.percent > 90 ? 'red' : info.percent > 80 ? 'yellow' : 'green';
    h += statRow(label, `${info.used_gb}/${info.total_gb} GB (${info.percent}%)`, color) +
      progressBar(info.percent, [80, 90]) +
      statRow('Free', info.free_gb + ' GB', color);
    h += '<div style="margin-bottom:12px"></div>';
  }
  h += '<div class="modal-section"><div class="modal-section-title">Disk Trends</div>';
  h += '<div class="time-range-btns" data-metric="disk"><button class="time-range-btn active" data-range="6h">6h</button><button class="time-range-btn" data-range="24h">24h</button><button class="time-range-btn" data-range="7d">7d</button></div>';
  h += '<canvas class="modal-trend-chart" data-metric="disk" height="140"></canvas></div>';
  return h;
};

DETAIL_RENDERERS.searxng_stats = function(d) {
  let h = statRow('Status', d.status || '?', d.status === 'up' ? 'green' : 'red') +
    statRow('Active Engines', d.engine_count || 0, 'cyan') +
    statRow('Total Engines', d.total_engines || 0) +
    statRow('Queries (total)', d.queries_total || 'n/a', 'magenta') +
    statRow('Avg Response', d.avg_response_ms ? d.avg_response_ms + 'ms' : 'n/a', 'green');

  // Engine categories breakdown
  if (d.categories && Object.keys(d.categories).length) {
    h += '<div class="modal-section"><div class="modal-section-title">Engine Categories</div><table class="modal-table"><thead><tr><th>Category</th><th>Count</th></tr></thead><tbody>';
    for (const [cat, count] of Object.entries(d.categories).sort((a,b) => b[1] - a[1])) {
      h += `<tr><td>${escapeHtml(cat)}</td><td>${count}</td></tr>`;
    }
    h += '</tbody></table></div>';
  }

  // Engine list
  if (d.engines?.length) {
    h += '<div class="modal-section"><div class="modal-section-title">Engines (' + d.engines.length + ')</div><table class="modal-table"><thead><tr><th>Engine</th><th>Shortcut</th><th>Category</th></tr></thead><tbody>';
    for (const e of d.engines) {
      const cat = Array.isArray(e.categories) ? e.categories[0] || '' : '';
      h += `<tr><td>${escapeHtml(e.name)}</td><td>${escapeHtml(e.shortcut)}</td><td style="color:var(--text-dim)">${escapeHtml(cat)}</td></tr>`;
    }
    h += '</tbody></table></div>';
  }
  if (d.error) h += `<div class="error-text">${escapeHtml(d.error)}</div>`;
  return h;
};

DETAIL_RENDERERS.backup_status = function(d) {
  let h = statRow('Status', d.status || '?', d.status === 'ok' ? 'green' : d.status === 'warning' ? 'yellow' : 'red') +
    statRow('Last Backup', d.last_backup || 'Never', d.success ? 'green' : 'red') +
    statRow('Result', d.success ? 'PASS' : 'FAIL', d.success ? 'green' : 'red') +
    (d.size ? statRow('Size', d.size) : '') +
    (d.stale ? '<div style="color:var(--accent-yellow);font-size:11px;margin-top:4px">Backup is stale (>24h old)</div>' : '') +
    (d.last_error ? statRow('Last Error', d.last_error, 'red') : '');

  if (d.lines?.length) {
    h += '<div class="modal-section"><div class="modal-section-title">Recent Log Lines</div>' +
      `<pre style="font-size:10px;color:var(--text-dim);white-space:pre-wrap;max-height:300px;overflow-y:auto;line-height:1.6">${d.lines.map(escapeHtml).join('\n')}</pre></div>`;
  }
  return h;
};

DETAIL_RENDERERS.response_time = function(d) {
  let h = statRow('Replies Today', d.replies_today || 0, 'cyan') +
    statRow('Avg Replies/Hour', d.avg_per_hour || 0, 'green');

  if (d.hourly_breakdown && Object.keys(d.hourly_breakdown).length > 0) {
    h += '<div class="modal-section"><div class="modal-section-title">Hourly Breakdown</div><table class="modal-table"><thead><tr><th>Hour</th><th>Replies</th></tr></thead><tbody>';
    for (const [hour, count] of Object.entries(d.hourly_breakdown).sort((a,b) => parseInt(a[0]) - parseInt(b[0]))) {
      h += `<tr><td>${hour}:00</td><td>${count}</td></tr>`;
    }
    h += '</tbody></table></div>';
  }

  if (d.recent_replies?.length) {
    h += '<div class="modal-section"><div class="modal-section-title">Recent Replies</div>' +
      `<pre style="font-size:10px;color:var(--text-dim);white-space:pre-wrap;max-height:300px;overflow-y:auto;line-height:1.6">${d.recent_replies.map(escapeHtml).join('\n')}</pre></div>`;
  }
  return h;
};

DETAIL_RENDERERS.herd_activity = function(d) {
  const colors = { slack: 'magenta', discord: 'blue', signal: 'green' };
  let h = statRow('Total Events', (d.total_events || 0).toLocaleString(), 'cyan');

  h += '<div class="modal-section"><div class="modal-section-title">All Time</div>';
  for (const [ch, count] of Object.entries(d.channels || {})) {
    h += statRow(ch, count.toLocaleString(), colors[ch] || '');
  }
  h += '</div>';

  if (d.today) {
    h += '<div class="modal-section"><div class="modal-section-title">Today</div>';
    for (const [ch, count] of Object.entries(d.today)) {
      h += statRow(ch, count.toLocaleString(), colors[ch] || '');
    }
    h += '</div>';
  }
  return h;
};

DETAIL_RENDERERS.mlx_status = function(d) {
  let h = statRow('Status', d.status || '?', d.status === 'up' ? 'green' : 'red') +
    statRow('Loaded Model', d.model || '?', 'cyan') +
    statRow('Model Count', d.model_count || 0);

  if (d.models?.length) {
    h += '<div class="modal-section"><div class="modal-section-title">Available Models</div><table class="modal-table"><thead><tr><th>Model ID</th><th>Owner</th></tr></thead><tbody>';
    for (const m of d.models) h += `<tr><td>${escapeHtml(m.id)}</td><td>${escapeHtml(m.owned_by)}</td></tr>`;
    h += '</tbody></table></div>';
  }
  if (d.error) h += `<div class="error-text">${escapeHtml(d.error)}</div>`;
  return h;
};

DETAIL_RENDERERS.cron_health = function(d) {
  const tasks = d.tasks || [];
  if (tasks.length === 0) return '<p class="dim">No task data</p>';
  let okCount = 0, failCount = 0, neverCount = 0;
  for (const t of tasks) {
    if (t.status === 'ok') okCount++;
    else if (t.status === 'failing') failCount++;
    else if (t.status === 'never') neverCount++;
  }
  let h = statRow('OK', okCount, 'green') +
    statRow('Failing', failCount, failCount > 0 ? 'red' : 'green') +
    statRow('Never Run', neverCount, neverCount > 0 ? 'yellow' : '') +
    statRow('Total', tasks.length, 'cyan');

  h += '<div class="modal-section"><div class="modal-section-title">All Jobs</div><table class="modal-table"><thead><tr><th>Job</th><th>Schedule</th><th>Runs</th><th>Fails</th><th>Status</th></tr></thead><tbody>';
  for (const t of tasks) {
    const color = t.status === 'ok' ? 'var(--accent-green)' : t.status === 'failing' ? 'var(--accent-red)' : t.status === 'running' ? 'var(--accent-cyan)' : 'var(--text-dim)';
    h += `<tr><td>${escapeHtml(t.name)}</td><td>${escapeHtml(t.schedule)}</td><td>${t.run_count||0}</td><td>${t.consecutive_failures||0}</td><td style="color:${color}">${t.status}</td></tr>`;
  }
  h += '</tbody></table></div>';
  return h;
};

DETAIL_RENDERERS.token_counter = function(d) {
  let h = statRow('Total Tokens', (d.total_tokens || 0).toLocaleString(), 'cyan') +
    statRow('Total Cost', '$' + (d.total_cost || 0).toFixed(4), 'magenta');

  if (d.by_provider && Object.keys(d.by_provider).length > 0) {
    h += '<div class="modal-section"><div class="modal-section-title">By Provider</div><table class="modal-table"><thead><tr><th>Provider</th><th>Input</th><th>Output</th><th>Total</th><th>Cost</th></tr></thead><tbody>';
    for (const [prov, p] of Object.entries(d.by_provider).sort((a,b) => (b[1].input_tokens+b[1].output_tokens) - (a[1].input_tokens+a[1].output_tokens))) {
      const tot = (p.input_tokens || 0) + (p.output_tokens || 0);
      h += `<tr><td>${escapeHtml(prov)}</td><td>${(p.input_tokens||0).toLocaleString()}</td><td>${(p.output_tokens||0).toLocaleString()}</td><td>${tot.toLocaleString()}</td><td>${p.cost > 0 ? '$' + p.cost.toFixed(4) : '$0'}</td></tr>`;
    }
    h += '</tbody></table></div>';
  }
  return h;
};

DETAIL_RENDERERS.cameras = function(d) {
  if (d.status === 'no_file') return '<p class="dim">No camera state file found</p>';
  let h = statRow('Total Cameras', d.total || 0, 'cyan') +
    statRow('Connected', d.connected || 0, 'green') +
    statRow('Disconnected', d.disconnected || 0, d.disconnected > 0 ? 'red' : 'green');

  if (d.cameras?.length) {
    h += '<div class="modal-section"><div class="modal-section-title">Camera Details</div><table class="modal-table"><thead><tr><th>Name</th><th>Type</th><th>IP</th><th>Status</th></tr></thead><tbody>';
    for (const c of d.cameras) {
      const color = c.connected ? 'var(--accent-green)' : 'var(--accent-red)';
      h += `<tr><td>${escapeHtml(c.name)}</td><td>${escapeHtml(c.type || '?')}</td><td style="font-size:10px">${escapeHtml(c.ip || '?')}</td><td style="color:${color}">${c.connected ? 'Online' : 'Offline'}</td></tr>`;
    }
    h += '</tbody></table></div>';
  }
  if (d.error) h += `<div class="error-text">${escapeHtml(d.error)}</div>`;
  return h;
};

DETAIL_RENDERERS.homekit = function(d) {
  if (d.status === 'unavailable') return '<p class="dim">Not configured or unavailable</p>';
  let h = statRow('Scenes', d.scene_count || 0, 'cyan') +
    statRow('Accessories', d.accessory_count || 0, 'green');

  if (d.scenes?.length) {
    h += '<div class="modal-section"><div class="modal-section-title">Scenes</div><table class="modal-table"><thead><tr><th>Scene</th><th>ID</th></tr></thead><tbody>';
    for (const s of d.scenes) h += `<tr><td>${escapeHtml(s.name)}</td><td style="font-size:10px">${escapeHtml(s.id)}</td></tr>`;
    h += '</tbody></table></div>';
  }

  if (d.raw_status && Object.keys(d.raw_status).length > 0) {
    h += '<div class="modal-section"><div class="modal-section-title">Status</div>';
    for (const [k, v] of Object.entries(d.raw_status)) {
      h += statRow(k, typeof v === 'object' ? JSON.stringify(v) : String(v));
    }
    h += '</div>';
  }
  if (d.error) h += `<div class="error-text">${escapeHtml(d.error)}</div>`;
  return h;
};

DETAIL_RENDERERS.searxng = DETAIL_RENDERERS.searxng_stats;
DETAIL_RENDERERS.gateway = DETAIL_RENDERERS.gateway_queries;

function formatBytesDetail(bytes) {
  if (!bytes) return '?';
  if (bytes < 1024*1024) return (bytes/1024).toFixed(1) + ' KB';
  if (bytes < 1024*1024*1024) return (bytes/(1024*1024)).toFixed(1) + ' MB';
  return (bytes/(1024*1024*1024)).toFixed(2) + ' GB';
}

// --- Modal Chart Loading ---
function loadModalCharts(container) {
  if (!container) return;
  const canvases = container.querySelectorAll('.modal-trend-chart');
  for (const canvas of canvases) {
    const metric = canvas.dataset.metric;
    if (!metric) continue;
    fetchChartData(canvas, metric, '6h');
  }
  // Wire up time-range buttons
  const btnGroups = container.querySelectorAll('.time-range-btns');
  for (const group of btnGroups) {
    const metric = group.dataset.metric;
    const buttons = group.querySelectorAll('.time-range-btn');
    const canvas = container.querySelector(`.modal-trend-chart[data-metric="${metric}"]`);
    buttons.forEach(btn => {
      btn.addEventListener('click', () => {
        buttons.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        if (canvas) fetchChartData(canvas, metric, btn.dataset.range);
      });
    });
  }
}

function fetchChartData(canvas, metric, range) {
  fetch(`/api/history/${metric}?range=${range}`)
    .then(r => r.json())
    .then(data => {
      if (!data || !data.points || data.points.length === 0) return;
      const series = [{
        label: metric,
        color: metric === 'costs' ? 'rgba(255,0,128,0.7)' : 'rgba(0,255,200,0.7)',
        data: data.points.map(p => ({ ts: p.ts || p.timestamp, value: p.value })),
      }];
      const unit = metric === 'costs' ? '$' : metric === 'memories' ? '' : '';
      drawLineChart(canvas, series, { unit, minY: 0 });
    })
    .catch(() => {});
}

// --- Shortcut Help Modal ---
function showShortcutHelp() {
  let h = '<table class="modal-table"><thead><tr><th>Key</th><th>Action</th></tr></thead><tbody>';
  h += '<tr><td>R</td><td>Reconnect WebSocket</td></tr>';
  h += '<tr><td>1-9</td><td>Jump to card by position</td></tr>';
  h += '<tr><td>/</td><td>Search tasks</td></tr>';
  h += '<tr><td>?</td><td>Show this help</td></tr>';
  h += '<tr><td>Esc</td><td>Close modal</td></tr>';
  h += '</tbody></table>';
  openModal('Keyboard Shortcuts', h);
}

// --- Search Modal ---
function showSearchModal() {
  let h = '<input class="search-input" type="text" placeholder="Search scheduler tasks..." autofocus>';
  h += '<ul class="search-results"></ul>';
  openModal('Search', h);
  const input = document.querySelector('.search-input');
  const results = document.querySelector('.search-results');
  if (input) {
    input.focus();
    input.addEventListener('input', () => {
      const q = input.value.toLowerCase().trim();
      if (!q) { results.innerHTML = ''; return; }
      const tasks = window.novaState?.scheduler?.tasks;
      if (!tasks) { results.innerHTML = '<li class="search-result-item dim">No task data</li>'; return; }
      const matches = Object.keys(tasks).filter(name => name.toLowerCase().includes(q));
      if (matches.length === 0) { results.innerHTML = '<li class="search-result-item dim">No matches</li>'; return; }
      results.innerHTML = matches.map(name =>
        `<li class="search-result-item" data-task="${escapeHtml(name)}">${escapeHtml(name)} <span class="dim" style="font-size:10px">${escapeHtml(tasks[name].schedule || '')}</span></li>`
      ).join('');
      results.querySelectorAll('.search-result-item[data-task]').forEach(el => {
        el.addEventListener('click', () => {
          closeModal();
          const section = document.getElementById('card-task-table');
          if (section) section.scrollIntoView({ behavior: 'smooth', block: 'center' });
        });
      });
    });
  }
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

// --- Theme Toggle ---
const savedTheme = localStorage.getItem('nova-theme') || 'dark';
document.documentElement.dataset.theme = savedTheme;
document.getElementById('theme-toggle')?.addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('nova-theme', next);
  document.getElementById('theme-toggle').textContent = next === 'dark' ? '☽' : '☀';
});

// --- Keyboard Shortcuts ---
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  const modal = document.getElementById('modal-overlay');
  if (e.key === 'Escape' && modal.classList.contains('active')) { closeModal(); return; }
  if (e.key === '?' && !modal.classList.contains('active')) { showShortcutHelp(); return; }
  if (e.key === 'r' || e.key === 'R') { if (ws) { ws.close(); connect(); } return; }
  if (e.key >= '1' && e.key <= '9') {
    const cards = document.querySelectorAll('#cards-section > .card');
    const idx = parseInt(e.key) - 1;
    if (cards[idx]) cards[idx].scrollIntoView({ behavior: 'smooth', block: 'center' });
    return;
  }
  if (e.key === '/' && !modal.classList.contains('active')) { e.preventDefault(); showSearchModal(); return; }
});

connect();
