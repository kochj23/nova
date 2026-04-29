const canvas = document.getElementById('node-graph');
const ctx = canvas.getContext('2d');
let W = 0, H = 0;
const dpr = window.devicePixelRatio || 1;

const NODE_DEFS = {
    slack:          { label: 'Slack',         group: 'channel',  gx: 0.10, gy: 0.15, icon: '#' },
    discord:        { label: 'Discord',       group: 'channel',  gx: 0.10, gy: 0.30, icon: '🎮' },
    signal:         { label: 'Signal',        group: 'channel',  gx: 0.10, gy: 0.45, icon: '🔒' },
    imessage:       { label: 'iMessage',      group: 'channel',  gx: 0.10, gy: 0.60, icon: '💬' },
    email:          { label: 'Email',         group: 'channel',  gx: 0.10, gy: 0.75, icon: '✉' },

    gateway:        { label: 'GATEWAY',       group: 'gateway',  gx: 0.50, gy: 0.42, icon: '⬢' },

    ollama:         { label: 'Ollama',        group: 'backend',  gx: 0.90, gy: 0.10, sublabel: 'coder · vision · dreams', icon: '🐇' },
    openrouter:     { label: 'OpenRouter',    group: 'backend',  gx: 0.90, gy: 0.26, sublabel: 'chat · conversation', icon: '⑂' },
    mlx_chat:       { label: 'MLX Chat',      group: 'backend',  gx: 0.90, gy: 0.42, sublabel: 'general · fast', icon: '⬡' },
    tinychat:       { label: 'TinyChat',      group: 'backend',  gx: 0.90, gy: 0.58, sublabel: 'web UI · RAG', icon: '💬' },
    openwebui:      { label: 'OpenWebUI',     group: 'backend',  gx: 0.90, gy: 0.74, sublabel: 'models · playground', icon: '🌐' },

    searxng:        { label: 'SearXNG',       group: 'support',  gx: 0.50, gy: 0.72, sublabel: 'web search', icon: '🔍' },

    redis:          { label: 'Redis',         group: 'support',  gx: 0.17, gy: 0.90, icon: '◆' },
    postgresql:     { label: 'PostgreSQL',    group: 'support',  gx: 0.33, gy: 0.90, icon: '🗃' },
    unifi:          { label: 'UniFi',         group: 'support',  gx: 0.50, gy: 0.90, icon: '📡' },
    memory_server:  { label: 'Memory',        group: 'support',  gx: 0.66, gy: 0.90, icon: '🧠' },
    scheduler:      { label: 'Scheduler',     group: 'support',  gx: 0.83, gy: 0.90, icon: '⏰' },
};

const EDGE_DEFS = [
  { from: 'slack',     to: 'gateway', dir: 'in' },
  { from: 'discord',   to: 'gateway', dir: 'in' },
  { from: 'signal',    to: 'gateway', dir: 'in' },
  { from: 'imessage',  to: 'gateway', dir: 'in' },
  { from: 'email',     to: 'gateway', dir: 'in' },
  { from: 'gateway',   to: 'ollama',       dir: 'out' },
  { from: 'gateway',   to: 'openrouter',   dir: 'out' },
  { from: 'searxng',        to: 'gateway', dir: 'support' },
  { from: 'gateway',   to: 'mlx_chat',     dir: 'out' },
  { from: 'gateway',   to: 'tinychat',     dir: 'out' },
  { from: 'gateway',   to: 'openwebui',    dir: 'out' },
  { from: 'redis',          to: 'gateway', dir: 'support' },
  { from: 'postgresql',     to: 'gateway', dir: 'support' },
  { from: 'unifi',          to: 'gateway', dir: 'support' },
  { from: 'memory_server',  to: 'gateway', dir: 'support' },
  { from: 'scheduler',      to: 'gateway', dir: 'support' },
];

const nodes = {};
const edges = [];
let particles = [];

function initNodes() {
  for (const [id, def] of Object.entries(NODE_DEFS)) {
    nodes[id] = {
      id,
      label: def.label,
      group: def.group,
      gx: def.gx,
      gy: def.gy,
      x: 0,
      y: 0,
      radius: 18, // will be recalculated in layoutNodes
      status: 'unknown',
      pulsePhase: Math.random() * Math.PI * 2,
    };
  }
  for (const def of EDGE_DEFS) {
    edges.push({ from: def.from, to: def.to, dir: def.dir, cx: 0, cy: 0 });
  }
}

function layoutNodes() {
  const unit = Math.min(W, H);
  for (const node of Object.values(nodes)) {
    node.x = node.gx * W;
    node.y = node.gy * H;
    // Responsive sizing: gateway = 6% of unit, others = 3.5% of unit
    node.radius = node.group === 'gateway' ? Math.max(32, unit * 0.06) : Math.max(18, unit * 0.035);
  }
  for (const edge of edges) {
    const a = nodes[edge.from];
    const b = nodes[edge.to];
    const mx = (a.x + b.x) / 2;
    const my = (a.y + b.y) / 2;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const len = Math.sqrt(dx * dx + dy * dy) || 1;
    const nx = -dy / len;
    const ny = dx / len;
    const curvature = edge.dir === 'support' ? 0.08 : 0.12;
    edge.cx = mx + nx * len * curvature;
    edge.cy = my + ny * len * curvature;
  }
}

function resizeCanvas() {
  const rect = canvas.parentElement.getBoundingClientRect();
  W = rect.width;
  H = rect.height;
  canvas.width = W * dpr;
  canvas.height = H * dpr;
  canvas.style.width = W + 'px';
  canvas.style.height = H + 'px';
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  layoutNodes();
  updateHitboxes();
}

function statusColor(status) {
  switch (status) {
    case 'running': case 'up': case 'ok': case 'live': case 'healthy':
      return [0, 255, 102];
    case 'degraded': case 'slow': case 'warning':
      return [255, 204, 0];
    case 'down': case 'error': case 'stopped':
      return [255, 51, 68];
    default:
      return [68, 85, 102];
  }
}

function rgba(c, a) {
  return `rgba(${c[0]},${c[1]},${c[2]},${a})`;
}

function particleColor(edge) {
  if (edge.dir === 'in') return [0, 255, 200];
  if (edge.dir === 'out') return [0, 255, 102];
  return [68, 136, 255];
}

function bezierPoint(ax, ay, cx, cy, bx, by, t) {
  const u = 1 - t;
  return {
    x: u * u * ax + 2 * u * t * cx + t * t * bx,
    y: u * u * ay + 2 * u * t * cy + t * t * by,
  };
}

function drawGrid() {
  const isLight = document.documentElement.dataset.theme === 'light';
  ctx.strokeStyle = isLight ? 'rgba(0, 100, 80, 0.06)' : 'rgba(0, 255, 200, 0.025)';
  ctx.lineWidth = 0.5;
  const spacing = 50;
  for (let x = spacing; x < W; x += spacing) {
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, H);
    ctx.stroke();
  }
  for (let y = spacing; y < H; y += spacing) {
    ctx.beginPath();
    ctx.moveTo(0, y);
    ctx.lineTo(W, y);
    ctx.stroke();
  }
}

function drawEdge(edge) {
  const a = nodes[edge.from];
  const b = nodes[edge.to];

  const isDown = a.status === 'down' || a.status === 'error' ||
                 b.status === 'down' || b.status === 'error';

  const rate = getEdgeFlowRate(edge);
  const intensity = Math.min(1, rate / 0.04);
  const baseAlpha = 0.08 + intensity * 0.2;

  ctx.beginPath();
  ctx.moveTo(a.x, a.y);
  ctx.quadraticCurveTo(edge.cx, edge.cy, b.x, b.y);

  if (isDown) {
    ctx.strokeStyle = `rgba(255, 51, 68, 0.12)`;
  } else if (edge.dir === 'support') {
    ctx.strokeStyle = `rgba(68, 136, 255, ${baseAlpha.toFixed(2)})`;
  } else {
    ctx.strokeStyle = `rgba(0, 255, 200, ${baseAlpha.toFixed(2)})`;
  }
  ctx.lineWidth = edge.dir === 'support' ? 1 + intensity : 1.5 + intensity;
  ctx.stroke();
}

function getNodeActivity(nodeId) {
  const flow = window.novaState?.traffic_flow;
  if (!flow) return 0;
  return flow[nodeId] || flow[nodeId.replace('_', '')] || 0;
}

function activityFillColor(activity, isDown, status) {
  if (isDown) return [255, 51, 68];
  if (status === 'unknown') return [0, 200, 130]; // still visible cyan-green for unknown
  if (activity < 0.01) return [0, 255, 102];       // green
  if (activity < 0.3) {
    const t = activity / 0.3;
    return [Math.round(255 * t), 255, Math.round(102 * (1 - t))];
  }
  if (activity < 0.7) {
    const t = (activity - 0.3) / 0.4;
    return [255, Math.round(255 - t * 100), 0];
  }
  const t = (activity - 0.7) / 0.3;
  return [255, Math.round(155 - t * 80), 0];
}

function drawNode(node, ts) {
  const isDown = node.status === 'down' || node.status === 'error' || node.status === 'stopped';
  const activity = getNodeActivity(node.id);
  const fillCol = isDown ? [255, 51, 68] : [0, 255, 102]; // Force green/red for visibility
  const pulse = Math.sin(ts / 1000 + node.pulsePhase) * 0.3 + 0.7;
  const r = node.radius;
  const fillOpacity = 0.3;

  // Outer glow halo
  const grad = ctx.createRadialGradient(node.x, node.y, r * 0.5, node.x, node.y, r * 2.2);
  grad.addColorStop(0, rgba(fillCol, 0.5 * pulse));
  grad.addColorStop(0.6, rgba(fillCol, 0.15 * pulse));
  grad.addColorStop(1, rgba(fillCol, 0));
  ctx.fillStyle = grad;
  ctx.beginPath();
  ctx.arc(node.x, node.y, r * 2.2, 0, Math.PI * 2);
  ctx.fill();

  // Solid filled interior
  ctx.beginPath();
  ctx.arc(node.x, node.y, r, 0, Math.PI * 2);
  ctx.fillStyle = rgba(fillCol, 0.3);
  ctx.fill();

  // Bright border ring
  ctx.beginPath();
  ctx.arc(node.x, node.y, r, 0, Math.PI * 2);
  ctx.strokeStyle = rgba(fillCol, 0.9);
  ctx.lineWidth = node.group === 'gateway' ? 3.5 : 2.5;
  ctx.stroke();

  // Inner detail ring
  ctx.beginPath();
  ctx.arc(node.x, node.y, r * 0.65, 0, Math.PI * 2);
  ctx.strokeStyle = rgba(fillCol, 0.3);
  ctx.lineWidth = 1;
  ctx.stroke();

  // Icon inside the node circle
  const def = NODE_DEFS[node.id];
  if (def && def.icon) {
    ctx.fillStyle = node.group === 'gateway' ? rgba([0, 255, 200], 0.85 * pulse) : rgba(fillCol, 0.85 * pulse);
    ctx.font = Math.max(14, r * 0.65) + 'px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(def.icon, node.x, node.y);
    ctx.textBaseline = 'alphabetic';
  } else if (node.group === 'gateway') {
    ctx.beginPath();
    ctx.arc(node.x, node.y, r * 0.4, 0, Math.PI * 2);
    ctx.fillStyle = rgba(col, 0.35 * pulse);
    ctx.fill();
  }

  // Label below node
  ctx.fillStyle = node.group === 'gateway' ? rgba([0, 255, 200], 0.9) : 'rgba(192, 192, 208, 0.85)';
  const labelSize = node.group === 'gateway' ? Math.max(12, r * 0.4) : Math.max(9, r * 0.5);
  ctx.font = (node.group === 'gateway' ? 'bold ' : '') + labelSize + 'px monospace';
  ctx.textAlign = 'center';
  ctx.fillText(node.label, node.x, node.y + r + Math.max(14, r * 0.6));

  // Sublabel (intent/role) below label for backend nodes
  if (def && def.sublabel) {
    ctx.fillStyle = 'rgba(128, 180, 160, 0.55)';
    ctx.font = Math.max(7, r * 0.35) + 'px monospace';
    ctx.fillText(def.sublabel, node.x, node.y + r + Math.max(14, r * 0.6) + Math.max(10, r * 0.45));
  }
}

class Particle {
  constructor(edge) {
    this.edge = edge;
    this.t = 0;
    const rate = getEdgeFlowRate(edge);
    const intensity = Math.min(1, rate / 0.06);
    this.speed = 0.003 + Math.random() * 0.006 + intensity * 0.006;
    this.size = 1.2 + Math.random() * 1.5 + intensity * 1.5;
    this.col = particleColor(edge);
    this.alpha = 0.4 + Math.random() * 0.3 + intensity * 0.3;
  }

  update() {
    this.t += this.speed;
    return this.t < 1.0;
  }

  draw() {
    const a = nodes[this.edge.from];
    const b = nodes[this.edge.to];
    const p = bezierPoint(a.x, a.y, this.edge.cx, this.edge.cy, b.x, b.y, this.t);
    const fade = 1 - Math.pow((this.t - 0.5) * 2, 2);

    ctx.beginPath();
    ctx.arc(p.x, p.y, this.size, 0, Math.PI * 2);
    ctx.fillStyle = rgba(this.col, this.alpha * Math.max(0.1, fade));
    ctx.fill();

    if (this.size > 2) {
      ctx.beginPath();
      ctx.arc(p.x, p.y, this.size * 2, 0, Math.PI * 2);
      ctx.fillStyle = rgba(this.col, 0.08 * fade);
      ctx.fill();
    }
  }
}

const MAX_PARTICLES = 500;
const AMBIENT_RATE = 0.002;

function getEdgeFlowRate(edge) {
  const flow = window.novaState?.traffic_flow;
  if (!flow) return AMBIENT_RATE;

  const nonGateway = edge.from === 'gateway' ? edge.to : edge.from;
  const rate = flow[nonGateway] || 0;

  if (rate <= 0) return AMBIENT_RATE;
  return AMBIENT_RATE + rate * 0.06;
}

function spawnParticles() {
  if (particles.length >= MAX_PARTICLES) return;

  for (const edge of edges) {
    const a = nodes[edge.from];
    const b = nodes[edge.to];

    if (a.status === 'down' && b.status === 'down') continue;

    const isDown = a.status === 'down' || a.status === 'error' ||
                   b.status === 'down' || b.status === 'error';

    const rate = isDown ? AMBIENT_RATE * 0.3 : getEdgeFlowRate(edge);

    if (Math.random() < rate) {
      particles.push(new Particle(edge));
    }
  }
}

function updateNodeStatuses() {
  const s = window.novaState;
  if (!s) return;

  if (s.gateway) {
    nodes.gateway.status = s.gateway.ok ? 'up' : 'down';
    const chStatus = s.gateway.ws_reachable ? 'up' : 'down';
    for (const ch of ['slack', 'discord', 'signal', 'imessage', 'email']) {
      nodes[ch].status = chStatus;
    }
  }

  if (s.services) {
    for (const [key, val] of Object.entries(s.services)) {
      if (nodes[key]) nodes[key].status = val.status;
    }
  }

  nodes.openrouter.status = 'up';

  if (s.redis) nodes.redis.status = s.redis.status === 'ok' ? 'up' : 'down';
  if (s.scheduler) nodes.scheduler.status = s.scheduler.status === 'ok' ? 'up' : 'down';
  if (s.services?.memory_server) nodes.memory_server.status = s.services.memory_server.status;

  nodes.postgresql.status = 'up';

  if (s.unifi) nodes.unifi.status = s.unifi.status === 'ok' ? 'up' : s.unifi.status === 'no_key' ? 'unknown' : 'down';
}

function animate(ts) {
  ctx.clearRect(0, 0, W, H);
  drawGrid();
  updateNodeStatuses();

  for (const edge of edges) drawEdge(edge);

  spawnParticles();
  particles = particles.filter(p => {
    const alive = p.update();
    if (alive) p.draw();
    return alive;
  });

  for (const node of Object.values(nodes)) drawNode(node, ts);

  requestAnimationFrame(animate);
}

const hitboxContainer = document.getElementById('node-hitboxes');

function updateHitboxes() {
  const existing = hitboxContainer.children;
  const nodeList = Object.values(nodes);

  if (existing.length !== nodeList.length) {
    hitboxContainer.innerHTML = '';
    for (const node of nodeList) {
      const div = document.createElement('div');
      div.className = 'node-hitbox';
      div.dataset.nodeId = node.id;
      div.dataset.nodeLabel = node.label;
      div.title = node.label;
      div.addEventListener('click', () => {
        if (typeof window.openNodeDetail === 'function') {
          window.openNodeDetail(node.id, node.label);
        }
      });
      hitboxContainer.appendChild(div);
    }
  }

  let i = 0;
  for (const node of nodeList) {
    const div = existing[i];
    if (div) {
      const size = (node.radius + 14) * 2;
      div.style.left = (node.x - size / 2) + 'px';
      div.style.top = (node.y - size / 2) + 'px';
      div.style.width = size + 'px';
      div.style.height = size + 'px';
    }
    i++;
  }
}

initNodes();
resizeCanvas();
window.addEventListener('resize', resizeCanvas);
requestAnimationFrame(animate);
