/* =====================================================================
   NOVA CONTROL — Radial Orbital HUD Visualization
   Sci-fi command center / radar display for TV output.
   Pure Canvas2D, no dependencies. 60fps via requestAnimationFrame.
   ===================================================================== */

(function () {
  'use strict';

  // ---- Color palette (RGB arrays) ----
  const C = {
    cyan:    [0, 255, 200],
    green:   [0, 255, 102],
    amber:   [255, 204, 0],
    red:     [255, 51, 68],
    blue:    [0, 136, 255],
    magenta: [255, 0, 170],
    dim:     [40, 55, 80],
    white:   [200, 210, 230],
  };

  function rgba(c, a) { return 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + a + ')'; }
  function lerpColor(a, b, t) {
    return [
      a[0] + (b[0] - a[0]) * t,
      a[1] + (b[1] - a[1]) * t,
      a[2] + (b[2] - a[2]) * t,
    ];
  }

  // ---- Constants ----
  const TWO_PI = Math.PI * 2;
  const DEG = Math.PI / 180;
  const RADAR_SWEEP_PERIOD = 5000;   // ms per full rotation
  const RING_COUNT = 6;              // concentric rings on gateway (more = denser)
  const TICK_COUNT = 120;            // compass tick marks (every 3 degrees)
  const ARC_SEGMENT_COUNT = 5;       // rotating arc segments per ring
  const MAX_PARTICLES = 1200;
  const MAX_DUST = 100;
  const AMBIENT_FLOW = 0.006;

  // ---- Orbital node definitions ----
  // angle in degrees (0 = right, 90 = bottom), orbit: 'inner' | 'outer'
  const ORBITAL_DEFS = {
    // Channels — outer orbit
    slack:    { label: 'SLACK',    angle: 200, orbit: 'outer', icon: 'S',  group: 'channel', intents: 'chat · commands' },
    discord:  { label: 'DISCORD',  angle: 220, orbit: 'outer', icon: 'D',  group: 'channel', intents: 'chat · notify' },
    signal:   { label: 'SIGNAL',   angle: 240, orbit: 'outer', icon: 'G',  group: 'channel', intents: 'private msgs' },
    imessage: { label: 'iMESSAGE', angle: 260, orbit: 'outer', icon: 'i',  group: 'channel', intents: 'relay · watch' },
    email:    { label: 'EMAIL',    angle: 280, orbit: 'outer', icon: 'E',  group: 'channel', intents: 'herd · reply' },
    // Backends — outer orbit (right side)
    ollama:     { label: 'OLLAMA',     angle: 20,  orbit: 'outer', icon: 'O', group: 'backend', intents: 'coder · vision · dreams · garden · journal' },
    openrouter: { label: 'OPENROUTER', angle: 45,  orbit: 'outer', icon: 'R', group: 'backend', intents: 'conversation · slack · discord · signal · herd' },
    mlx_chat:   { label: 'MLX',        angle: 70,  orbit: 'outer', icon: 'M', group: 'backend', intents: 'memory · email · health · reasoner · rag · quick' },
    // Support — inner orbit
    redis:         { label: 'REDIS',     angle: 120, orbit: 'inner', icon: 'R', group: 'support', intents: 'cache · queue' },
    postgresql:    { label: 'POSTGRES',  angle: 140, orbit: 'inner', icon: 'P', group: 'support', intents: '1.38M vectors · pgvector' },
    memory_server: { label: 'MEMORY',    angle: 160, orbit: 'inner', icon: 'M', group: 'support', intents: 'recall · remember · search' },
    scheduler:     { label: 'SCHEDULER', angle: 310, orbit: 'inner', icon: 'C', group: 'support', intents: '39 tasks · cron · interval' },
    searxng:       { label: 'SEARXNG',   angle: 340, orbit: 'inner', icon: 'X', group: 'support', intents: 'web search · private' },
  };

  // ---- State ----
  let state = null;
  let ws = null;
  let wsRetryDelay = 1000;

  // ---- Canvas setup ----
  const canvas = document.getElementById('hud-canvas');
  const ctx = canvas.getContext('2d');
  const hmCanvas = document.getElementById('hud-heatmap');
  const hmCtx = hmCanvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  let W = 0, H = 0;
  let CX = 0, CY = 0;     // center of screen
  let UNIT = 0;            // min(W, H)
  let gatewayR = 0;        // gateway circle radius (visual, ~15% of UNIT)
  let innerOrbitR = 0;     // 30% of UNIT
  let outerOrbitR = 0;     // 42% of UNIT

  // ---- Runtime node objects ----
  const nodes = {};
  let particles = [];
  let dustParticles = [];

  // Gateway pulse state (brightens when traffic flows)
  let gatewayPulse = 0;     // 0..1
  let gatewayReqSec = 0;
  let gatewayConnections = 0;

  // ---- Ticker ----
  const tickerEvents = [];
  const MAX_TICKER = 40;
  const _tickerSeen = new Set();

  // ---- Heatmap tasks for bottom-left mini grid ----
  let heatmapTasks = [];

  // ---- Initialize nodes ----
  function initNodes() {
    for (const [id, def] of Object.entries(ORBITAL_DEFS)) {
      nodes[id] = {
        id: id,
        label: def.label,
        group: def.group,
        icon: def.icon,
        angleDeg: def.angle,
        angleRad: def.angle * DEG,
        orbit: def.orbit,
        x: 0, y: 0,
        radius: 0,       // computed in layout
        status: 'unknown',
        activity: 0,
        pulsePhase: Math.random() * TWO_PI,
        gaugeValue: 0,    // mini ring gauge 0..1
      };
    }
  }

  // ---- Layout ----
  function layout() {
    UNIT = Math.min(W, H);
    CX = W / 2;
    CY = H * 0.47;  // slightly above center for better balance with bottom bar
    gatewayR = UNIT * 0.28;          // MUCH larger (was 0.15) — fills ~60% of screen
    innerOrbitR = UNIT * 0.34;       // Tight inner orbit (was 0.30) — just outside the gateway rings
    outerOrbitR = UNIT * 0.44;       // Tight outer orbit (was 0.42) — close to inner

    for (const node of Object.values(nodes)) {
      const orbitR = node.orbit === 'inner' ? innerOrbitR : outerOrbitR;
      node.x = CX + Math.cos(node.angleRad) * orbitR;
      node.y = CY + Math.sin(node.angleRad) * orbitR;
      node.radius = node.orbit === 'inner' ? Math.max(28, UNIT * 0.042) : Math.max(32, UNIT * 0.048);
    }
  }

  function resize() {
    const main = document.getElementById('hud-main');
    const rect = main.getBoundingClientRect();
    W = rect.width;
    H = rect.height;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    hmCanvas.width = W * dpr;
    hmCanvas.height = H * dpr;
    hmCanvas.style.width = W + 'px';
    hmCanvas.style.height = H + 'px';
    hmCtx.setTransform(dpr, 0, 0, dpr, 0, 0);

    layout();
    initDust();
  }

  // ---- Status helpers ----
  function statusColor(status) {
    switch (status) {
      case 'running': case 'up': case 'ok': case 'live': case 'healthy':
        return C.cyan;
      case 'degraded': case 'slow': case 'warning':
        return C.amber;
      case 'down': case 'error': case 'stopped':
        return C.red;
      default:
        return C.dim;
    }
  }

  function nodeLineColor(node) {
    var s = node.status;
    if (s === 'up' || s === 'ok' || s === 'live' || s === 'healthy' || s === 'running') return C.cyan;
    if (s === 'degraded' || s === 'slow' || s === 'warning') return C.amber;
    if (s === 'down' || s === 'error' || s === 'stopped') return C.red;
    return C.dim;
  }

  // ---- Flow rate for a given node ----
  function getFlowRate(nodeId) {
    if (!state || !state.traffic_flow) return AMBIENT_FLOW;
    var rate = state.traffic_flow[nodeId] || 0;
    return rate <= 0 ? AMBIENT_FLOW : AMBIENT_FLOW + rate * 0.06;
  }

  // ================================================================
  //  DRAWING: Background effects
  // ================================================================

  function drawRadialGrid(ts) {
    ctx.save();

    var maxR = Math.max(W, H) * 0.7;

    // Dense concentric background circles (more rings, tighter spacing)
    var ringSpacing = UNIT * 0.04;
    for (var r = ringSpacing; r < maxR; r += ringSpacing) {
      var distRatio = r / maxR;
      ctx.beginPath();
      ctx.arc(CX, CY, r, 0, TWO_PI);
      ctx.strokeStyle = rgba(C.cyan, 0.02 - distRatio * 0.015);
      ctx.lineWidth = (r === innerOrbitR || r === outerOrbitR) ? 1.0 : 0.4;
      ctx.stroke();
    }

    // Orbit path rings (highlighted) — dashed
    ctx.setLineDash([4, 8]);
    ctx.beginPath();
    ctx.arc(CX, CY, innerOrbitR, 0, TWO_PI);
    ctx.strokeStyle = rgba(C.cyan, 0.08);
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(CX, CY, outerOrbitR, 0, TWO_PI);
    ctx.strokeStyle = rgba(C.cyan, 0.06);
    ctx.stroke();
    ctx.setLineDash([]);

    // Dense radial lines (72 lines, like a clock face)
    var lineCount = 72;
    for (var i = 0; i < lineCount; i++) {
      var angle = (TWO_PI / lineCount) * i;
      var isMajor = (i % 6 === 0);
      ctx.beginPath();
      ctx.moveTo(CX + Math.cos(angle) * gatewayR * 0.5, CY + Math.sin(angle) * gatewayR * 0.5);
      ctx.lineTo(CX + Math.cos(angle) * maxR, CY + Math.sin(angle) * maxR);
      ctx.strokeStyle = rgba(C.cyan, isMajor ? 0.03 : 0.01);
      ctx.lineWidth = isMajor ? 0.8 : 0.4;
      ctx.stroke();
    }

    // Cross-hairs at center (very subtle)
    ctx.strokeStyle = rgba(C.cyan, 0.04);
    ctx.lineWidth = 0.6;
    ctx.beginPath();
    ctx.moveTo(CX - maxR, CY);
    ctx.lineTo(CX + maxR, CY);
    ctx.stroke();
    ctx.beginPath();
    ctx.moveTo(CX, CY - maxR);
    ctx.lineTo(CX, CY + maxR);
    ctx.stroke();

    ctx.restore();
  }

  // ================================================================
  //  DRAWING: Central gateway (the big radar circle)
  // ================================================================

  function drawGateway(ts) {
    ctx.save();

    var gwStatus = state && state.gateway ? (state.gateway.ok ? 'up' : 'down') : 'unknown';
    var baseCol = statusColor(gwStatus);
    var pulse = 0.6 + 0.4 * Math.sin(ts / 1200);
    var trafficPulse = 1 + gatewayPulse * 0.6; // brightens with traffic

    // --- Concentric rings ---
    for (var ring = 0; ring < RING_COUNT; ring++) {
      var ringR = gatewayR * (0.35 + (ring + 1) * 0.12);
      var ringAlpha = (0.04 + ring * 0.02) * trafficPulse;

      // Static ring
      ctx.beginPath();
      ctx.arc(CX, CY, ringR, 0, TWO_PI);
      ctx.strokeStyle = rgba(baseCol, ringAlpha * pulse);
      ctx.lineWidth = ring === RING_COUNT - 1 ? 2 : 1;
      ctx.stroke();

      // Rotating arc segments on each ring
      var segSpeed = (ring % 2 === 0 ? 1 : -1) * (0.0003 + ring * 0.0001);
      for (var s = 0; s < ARC_SEGMENT_COUNT; s++) {
        var segAngle = (TWO_PI / ARC_SEGMENT_COUNT) * s + ts * segSpeed;
        var segLen = 0.3 + ring * 0.05; // radians of arc
        ctx.beginPath();
        ctx.arc(CX, CY, ringR, segAngle, segAngle + segLen);
        ctx.strokeStyle = rgba(baseCol, (0.08 + ring * 0.03) * trafficPulse * pulse);
        ctx.lineWidth = 2;
        ctx.stroke();
      }
    }

    // --- Tick marks around circumference (outer ring) ---
    var outerRingR = gatewayR * (0.35 + RING_COUNT * 0.12);
    ctx.strokeStyle = rgba(baseCol, 0.12 * trafficPulse);
    ctx.lineWidth = 1;
    for (var t = 0; t < TICK_COUNT; t++) {
      var tickAngle = (TWO_PI / TICK_COUNT) * t;
      var isMajor = (t % 6 === 0); // every 30 degrees is major
      var tickInner = outerRingR + 2;
      var tickOuter = outerRingR + (isMajor ? 10 : 5);

      ctx.beginPath();
      ctx.moveTo(CX + Math.cos(tickAngle) * tickInner, CY + Math.sin(tickAngle) * tickInner);
      ctx.lineTo(CX + Math.cos(tickAngle) * tickOuter, CY + Math.sin(tickAngle) * tickOuter);
      if (isMajor) {
        ctx.strokeStyle = rgba(baseCol, 0.2 * trafficPulse);
        ctx.lineWidth = 1.5;
      } else {
        ctx.strokeStyle = rgba(baseCol, 0.08 * trafficPulse);
        ctx.lineWidth = 0.8;
      }
      ctx.stroke();
    }

    // --- Radar sweep line ---
    var sweepAngle = ((ts % RADAR_SWEEP_PERIOD) / RADAR_SWEEP_PERIOD) * TWO_PI - Math.PI / 2;
    var sweepR = outerRingR + 12;

    // Sweep trail (fading arc behind the line)
    var trailLen = 0.6; // radians
    var trailGrad = ctx.createConicGradient(sweepAngle - trailLen, CX, CY);
    trailGrad.addColorStop(0, rgba(baseCol, 0));
    trailGrad.addColorStop(0.8, rgba(baseCol, 0.04 * trafficPulse));
    trailGrad.addColorStop(1, rgba(baseCol, 0.12 * trafficPulse));
    ctx.beginPath();
    ctx.moveTo(CX, CY);
    ctx.arc(CX, CY, sweepR, sweepAngle - trailLen, sweepAngle);
    ctx.closePath();
    ctx.fillStyle = trailGrad;
    ctx.fill();

    // Sweep line itself
    ctx.beginPath();
    ctx.moveTo(CX, CY);
    ctx.lineTo(CX + Math.cos(sweepAngle) * sweepR, CY + Math.sin(sweepAngle) * sweepR);
    ctx.strokeStyle = rgba(baseCol, 0.35 * trafficPulse);
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Bright dot at the tip
    var tipX = CX + Math.cos(sweepAngle) * sweepR;
    var tipY = CY + Math.sin(sweepAngle) * sweepR;
    ctx.beginPath();
    ctx.arc(tipX, tipY, 3, 0, TWO_PI);
    ctx.fillStyle = rgba(baseCol, 0.6);
    ctx.fill();

    // --- Inner filled circle (core) ---
    var coreR = gatewayR * 0.25;
    var coreGrad = ctx.createRadialGradient(CX, CY, 0, CX, CY, coreR);
    coreGrad.addColorStop(0, rgba(baseCol, 0.12 * trafficPulse));
    coreGrad.addColorStop(1, rgba(baseCol, 0.02));
    ctx.beginPath();
    ctx.arc(CX, CY, coreR, 0, TWO_PI);
    ctx.fillStyle = coreGrad;
    ctx.fill();

    // Core ring
    ctx.beginPath();
    ctx.arc(CX, CY, coreR, 0, TWO_PI);
    ctx.strokeStyle = rgba(baseCol, 0.2 * pulse * trafficPulse);
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // --- OpenClaw icon + "GATEWAY" label ---
    ctx.font = Math.max(24, UNIT * 0.04) + 'px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = rgba(baseCol, 0.8 * trafficPulse);
    ctx.fillText('🐙', CX, CY - UNIT * 0.025);
    ctx.font = 'bold ' + Math.max(12, UNIT * 0.018) + 'px "Orbitron", "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(baseCol, 0.6 * trafficPulse);
    ctx.fillText('GATEWAY', CX, CY + UNIT * 0.02);

    // --- Data readouts along rings ---
    ctx.font = Math.max(9, UNIT * 0.011) + 'px "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(baseCol, 0.4 * trafficPulse);

    // Reqs/sec readout at top
    var readoutR = gatewayR * 0.80;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillText(gatewayReqSec.toFixed(1) + ' req/s', CX, CY + readoutR * 0.3);

    // Active connections at bottom
    ctx.textBaseline = 'bottom';
    ctx.fillText(gatewayConnections + ' active', CX, CY - readoutR * 0.15);

    // --- Outer glow halo ---
    var haloR = outerRingR + 30;
    var haloGrad = ctx.createRadialGradient(CX, CY, outerRingR * 0.8, CX, CY, haloR);
    haloGrad.addColorStop(0, rgba(baseCol, 0.02 * trafficPulse));
    haloGrad.addColorStop(1, rgba(baseCol, 0));
    ctx.beginPath();
    ctx.arc(CX, CY, haloR, 0, TWO_PI);
    ctx.fillStyle = haloGrad;
    ctx.fill();

    ctx.restore();
  }

  // ================================================================
  //  DRAWING: Orbit rings (dashed circles for inner and outer)
  // ================================================================

  function drawOrbitRings(ts) {
    ctx.save();

    // Inner orbit ring
    ctx.setLineDash([4, 12]);
    ctx.strokeStyle = rgba(C.cyan, 0.04);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(CX, CY, innerOrbitR, 0, TWO_PI);
    ctx.stroke();

    // Outer orbit ring
    ctx.strokeStyle = rgba(C.cyan, 0.03);
    ctx.beginPath();
    ctx.arc(CX, CY, outerOrbitR, 0, TWO_PI);
    ctx.stroke();

    ctx.setLineDash([]);
    ctx.restore();
  }

  // ================================================================
  //  DRAWING: Connection lines (center to orbital nodes)
  // ================================================================

  function drawConnectionLine(node, ts) {
    ctx.save();

    var col = nodeLineColor(node);
    var flow = getFlowRate(node.id);
    var intensity = Math.min(1, flow / 0.04);

    // Line from center to node
    var baseAlpha = 0.03 + intensity * 0.12;
    var grad = ctx.createLinearGradient(CX, CY, node.x, node.y);
    grad.addColorStop(0, rgba(col, baseAlpha * 0.3));
    grad.addColorStop(0.5, rgba(col, baseAlpha));
    grad.addColorStop(1, rgba(col, baseAlpha * 0.7));

    ctx.beginPath();
    ctx.moveTo(CX, CY);
    ctx.lineTo(node.x, node.y);
    ctx.strokeStyle = grad;
    ctx.lineWidth = 1 + intensity * 1.5;
    ctx.stroke();

    // Glow pass
    if (intensity > 0.1) {
      ctx.beginPath();
      ctx.moveTo(CX, CY);
      ctx.lineTo(node.x, node.y);
      ctx.strokeStyle = rgba(col, 0.02 + intensity * 0.04);
      ctx.lineWidth = 4 + intensity * 6;
      ctx.stroke();
    }

    ctx.restore();
  }

  // ================================================================
  //  DRAWING: Orbital nodes
  // ================================================================

  function drawOrbitalNode(node, ts) {
    ctx.save();

    var col = statusColor(node.status);
    var activity = node.activity || 0;
    var pulseSpeed = 800 + (1 - activity) * 1200;
    var pulse = Math.sin((ts / pulseSpeed) + node.pulsePhase) * 0.3 + 0.7;
    var glowIntensity = 0.5 + activity * 0.5;
    var r = node.radius;

    // Outer glow halo
    var haloR = r * (2.0 + activity * 2.0);
    var haloGrad = ctx.createRadialGradient(node.x, node.y, r * 0.3, node.x, node.y, haloR);
    haloGrad.addColorStop(0, rgba(col, 0.15 * pulse * glowIntensity));
    haloGrad.addColorStop(0.5, rgba(col, 0.04 * pulse * glowIntensity));
    haloGrad.addColorStop(1, rgba(col, 0));
    ctx.beginPath();
    ctx.arc(node.x, node.y, haloR, 0, TWO_PI);
    ctx.fillStyle = haloGrad;
    ctx.fill();

    // Filled circle
    ctx.beginPath();
    ctx.arc(node.x, node.y, r, 0, TWO_PI);
    var fillGrad = ctx.createRadialGradient(node.x, node.y, 0, node.x, node.y, r);
    fillGrad.addColorStop(0, rgba(col, 0.18 * pulse * glowIntensity));
    fillGrad.addColorStop(1, rgba(col, 0.04 * pulse));
    ctx.fillStyle = fillGrad;
    ctx.fill();

    // Border ring (outer)
    ctx.beginPath();
    ctx.arc(node.x, node.y, r, 0, TWO_PI);
    ctx.strokeStyle = rgba(col, 0.6 * pulse * glowIntensity);
    ctx.lineWidth = 2;
    ctx.stroke();

    // Second border ring (inner detail)
    ctx.beginPath();
    ctx.arc(node.x, node.y, r * 0.7, 0, TWO_PI);
    ctx.strokeStyle = rgba(col, 0.2 * pulse);
    ctx.lineWidth = 0.8;
    ctx.stroke();

    // Rotating arc on the outer ring (activity indicator)
    if (activity > 0.01) {
      var arcStart = (ts / 2000 + node.pulsePhase) % TWO_PI;
      var arcLen = 0.4 + activity * 1.2;
      ctx.beginPath();
      ctx.arc(node.x, node.y, r * 1.15, arcStart, arcStart + arcLen);
      ctx.strokeStyle = rgba(col, 0.7 * activity);
      ctx.lineWidth = 2.5;
      ctx.stroke();
    }

    // Inner dot
    ctx.beginPath();
    ctx.arc(node.x, node.y, r * 0.2, 0, TWO_PI);
    ctx.fillStyle = rgba(col, 0.8 * pulse);
    ctx.fill();

    // Mini ring gauge (health/load)
    drawMiniGauge(node, ts, col, pulse, glowIntensity);

    // Icon letter inside
    ctx.font = 'bold ' + Math.max(9, r * 0.7) + 'px "Orbitron", "Share Tech Mono", monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillStyle = rgba(col, 0.6 * pulse * glowIntensity);
    ctx.fillText(node.icon, node.x, node.y);

    // Label below
    ctx.font = Math.max(9, UNIT * 0.012) + 'px "Share Tech Mono", monospace';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillStyle = rgba(col, 0.75);
    ctx.fillText(node.label, node.x, node.y + r + 6);

    // Intent/role sublabel (smaller, dimmer)
    if (node.intents) {
      ctx.font = Math.max(7, UNIT * 0.008) + 'px "Share Tech Mono", monospace';
      ctx.fillStyle = rgba(col, 0.35);
      ctx.fillText(node.intents, node.x, node.y + r + 18);
    }

    ctx.restore();
  }

  function drawMiniGauge(node, ts, col, pulse, glow) {
    var r = node.radius + 4;
    var gaugeVal = node.gaugeValue || 0;
    var lineW = 2;
    var startAngle = -Math.PI / 2;
    var endAngle = startAngle + TWO_PI * gaugeVal;

    // Background ring
    ctx.beginPath();
    ctx.arc(node.x, node.y, r, 0, TWO_PI);
    ctx.strokeStyle = rgba(col, 0.05);
    ctx.lineWidth = lineW;
    ctx.stroke();

    // Filled arc
    if (gaugeVal > 0.005) {
      var arcCol = col;
      if (gaugeVal > 0.85) arcCol = C.red;
      else if (gaugeVal > 0.65) arcCol = C.amber;

      ctx.beginPath();
      ctx.arc(node.x, node.y, r, startAngle, endAngle);
      ctx.strokeStyle = rgba(arcCol, 0.55 * pulse * glow);
      ctx.lineWidth = lineW;
      ctx.lineCap = 'round';
      ctx.stroke();
    }
  }

  // ================================================================
  //  PARTICLES: flowing along connection lines
  // ================================================================

  function Particle(nodeId) {
    this.nodeId = nodeId;
    this.t = 0;
    var flow = getFlowRate(nodeId);
    var intensity = Math.min(1, flow / 0.05);
    this.speed = 0.003 + Math.random() * 0.005 + intensity * 0.008;
    this.size = 1.2 + Math.random() * 1.5 + intensity * 1.5;
    this.col = statusColor(nodes[nodeId].status);
    this.alpha = 0.4 + Math.random() * 0.3 + intensity * 0.3;
    this.trail = [];
  }

  Particle.prototype.update = function () {
    var node = nodes[this.nodeId];
    if (!node) return false;

    // Linear interpolation along the line from center to node
    var x = CX + (node.x - CX) * this.t;
    var y = CY + (node.y - CY) * this.t;

    if (this.trail.length > 5) this.trail.shift();
    this.trail.push({ x: x, y: y });

    this.t += this.speed;
    return this.t < 1.0;
  };

  Particle.prototype.draw = function () {
    var node = nodes[this.nodeId];
    if (!node) return;

    var x = CX + (node.x - CX) * this.t;
    var y = CY + (node.y - CY) * this.t;
    var fade = 1 - Math.pow((this.t - 0.5) * 2, 2);

    // Trail
    for (var i = 0; i < this.trail.length; i++) {
      var tp = this.trail[i];
      var ta = (i / this.trail.length) * 0.25 * fade;
      var ts = this.size * (0.3 + 0.5 * (i / this.trail.length));
      ctx.beginPath();
      ctx.arc(tp.x, tp.y, ts, 0, TWO_PI);
      ctx.fillStyle = rgba(this.col, ta * this.alpha);
      ctx.fill();
    }

    // Main particle
    ctx.beginPath();
    ctx.arc(x, y, this.size, 0, TWO_PI);
    ctx.fillStyle = rgba(this.col, this.alpha * Math.max(0.15, fade));
    ctx.fill();

    // Glow
    if (this.size > 1.5) {
      ctx.beginPath();
      ctx.arc(x, y, this.size * 2.5, 0, TWO_PI);
      ctx.fillStyle = rgba(this.col, 0.04 * fade);
      ctx.fill();
    }
  };

  function spawnParticles() {
    if (particles.length >= MAX_PARTICLES) return;
    for (var id in nodes) {
      var node = nodes[id];
      var isDown = node.status === 'down' || node.status === 'error';
      var rate = isDown ? AMBIENT_FLOW * 0.15 : getFlowRate(id);
      if (Math.random() < rate) {
        // Randomly choose direction: center->node or node->center
        var p = new Particle(id);
        if (Math.random() < 0.4) {
          // Reverse direction (inbound)
          p.t = 1.0;
          p.speed = -p.speed;
          p._reverse = true;
        }
        particles.push(p);
      }
    }
  }

  // ================================================================
  //  AMBIENT DUST PARTICLES
  // ================================================================

  function DustParticle() {
    this.x = Math.random() * W;
    this.y = Math.random() * H;
    this.vx = (Math.random() - 0.5) * 0.15;
    this.vy = (Math.random() - 0.5) * 0.1;
    this.size = 0.5 + Math.random() * 1.2;
    this.alpha = 0.02 + Math.random() * 0.04;
    this.col = Math.random() < 0.7 ? C.cyan : C.blue;
  }

  DustParticle.prototype.update = function () {
    this.x += this.vx;
    this.y += this.vy;
    if (this.x < 0) this.x = W;
    if (this.x > W) this.x = 0;
    if (this.y < 0) this.y = H;
    if (this.y > H) this.y = 0;
  };

  DustParticle.prototype.draw = function () {
    ctx.beginPath();
    ctx.arc(this.x, this.y, this.size, 0, TWO_PI);
    ctx.fillStyle = rgba(this.col, this.alpha);
    ctx.fill();
  };

  function initDust() {
    dustParticles = [];
    for (var i = 0; i < MAX_DUST; i++) {
      dustParticles.push(new DustParticle());
    }
  }

  // ================================================================
  //  CORNER PANELS
  // ================================================================

  function drawCornerPanels(ts) {
    ctx.save();

    var panelPad = Math.max(16, UNIT * 0.025);
    var fontSize = Math.max(10, UNIT * 0.013);
    var bigFontSize = Math.max(18, UNIT * 0.032);
    var pulse = 0.7 + 0.3 * Math.sin(ts / 1500);

    // ---- TOP-LEFT: Memory count with mini arc gauge ----
    var tlX = panelPad;
    var tlY = panelPad;
    var memCount = 0;
    var memMax = 2000000;
    if (state && state.postgresql) {
      memCount = state.postgresql.total_rows || 0;
    }
    var memRatio = Math.min(1, memCount / memMax);

    // Label
    ctx.font = fontSize + 'px "Share Tech Mono", monospace';
    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    ctx.fillStyle = rgba(C.cyan, 0.35);
    ctx.fillText('MEMORIES', tlX, tlY);

    // Big number
    ctx.font = 'bold ' + bigFontSize + 'px "Orbitron", "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(C.magenta, 0.7 * pulse);
    ctx.fillText(formatNumber(memCount), tlX, tlY + fontSize + 4);

    // Mini arc gauge (toward 2M)
    var arcCX = tlX + UNIT * 0.06;
    var arcCY = tlY + fontSize + bigFontSize + UNIT * 0.04;
    var arcR = UNIT * 0.025;
    var arcStart = Math.PI * 0.8;
    var arcEnd = Math.PI * 2.2;
    var arcFill = arcStart + (arcEnd - arcStart) * memRatio;

    // Background arc
    ctx.beginPath();
    ctx.arc(arcCX, arcCY, arcR, arcStart, arcEnd);
    ctx.strokeStyle = rgba(C.magenta, 0.08);
    ctx.lineWidth = 3;
    ctx.lineCap = 'round';
    ctx.stroke();

    // Fill arc
    if (memRatio > 0.001) {
      ctx.beginPath();
      ctx.arc(arcCX, arcCY, arcR, arcStart, arcFill);
      ctx.strokeStyle = rgba(C.magenta, 0.4 * pulse);
      ctx.lineWidth = 3;
      ctx.stroke();
    }

    // Target label
    ctx.font = (fontSize - 2) + 'px "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(C.magenta, 0.25);
    ctx.textAlign = 'center';
    ctx.fillText('/ 2M', arcCX, arcCY + arcR + 6);

    // ---- TODAY stats (below the arc gauge) ----
    var todayY = arcCY + arcR + 20;
    var todayCount = 0;
    var todaySources = [];
    if (state && state.postgresql) {
      todayCount = state.postgresql.today_count || 0;
      todaySources = state.postgresql.today_sources || [];
    }

    ctx.textAlign = 'left';
    ctx.textBaseline = 'top';
    ctx.font = fontSize + 'px "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(C.green, 0.35);
    ctx.fillText('TODAY', tlX, todayY);

    ctx.font = 'bold ' + (bigFontSize * 0.8) + 'px "Orbitron", "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(C.green, 0.65 * pulse);
    ctx.fillText('+' + formatNumber(todayCount), tlX, todayY + fontSize + 2);

    // Top sources today (compact list)
    if (todaySources.length > 0) {
      ctx.font = (fontSize - 2) + 'px "Share Tech Mono", monospace';
      var srcY = todayY + fontSize + bigFontSize * 0.8 + 6;
      for (var si = 0; si < Math.min(4, todaySources.length); si++) {
        var src = todaySources[si];
        ctx.fillStyle = rgba(C.green, 0.25);
        ctx.fillText(src.source.substring(0, 14) + ' +' + formatNumber(src.count), tlX, srcY);
        srcY += fontSize - 1;
      }
    }

    // ---- LEFT COLUMN: Subsystem status below Today ----
    var subY = srcY || (todayY + fontSize + bigFontSize * 0.8 + 10);
    subY += 8;

    // Weather
    if (state && state.weather && state.weather.status === 'ok') {
      ctx.font = fontSize + 'px "Share Tech Mono", monospace';
      ctx.fillStyle = rgba(C.blue, 0.35);
      ctx.textAlign = 'left';
      ctx.textBaseline = 'top';
      ctx.fillText('WEATHER', tlX, subY);
      var wTemp = state.weather.temperature || '?';
      var wCond = (state.weather.conditions || '').substring(0, 16);
      ctx.font = (fontSize - 1) + 'px "Share Tech Mono", monospace';
      ctx.fillStyle = rgba(C.blue, 0.5 * pulse);
      ctx.fillText(wTemp + '  ' + wCond, tlX, subY + fontSize + 1);
      var moonPhase = state.weather.moon_phase || '';
      if (moonPhase) {
        ctx.fillStyle = rgba(C.white, 0.3);
        ctx.fillText(moonPhase.substring(0, 30), tlX, subY + fontSize * 2 + 2);
      }
      subY += fontSize * 3 + 8;
    }

    // App watchdog summary
    if (state && state.app_watchdog && state.app_watchdog.apps) {
      var apps = state.app_watchdog.apps;
      var aUp = apps.filter(function(a) { return a.status === 'up'; }).length;
      var aDown = apps.length - aUp;
      ctx.font = fontSize + 'px "Share Tech Mono", monospace';
      ctx.fillStyle = rgba(aDown > 0 ? C.amber : C.cyan, 0.35);
      ctx.textAlign = 'left';
      ctx.fillText('APPS', tlX, subY);
      ctx.font = (fontSize - 1) + 'px "Share Tech Mono", monospace';
      ctx.fillStyle = rgba(aDown > 0 ? C.amber : C.cyan, 0.45 * pulse);
      ctx.fillText(aUp + ' up / ' + aDown + ' down', tlX, subY + fontSize + 1);
      subY += fontSize * 2 + 8;
    }

    // Dead man's switch
    if (state && state.scheduler && state.scheduler.tasks) {
      var dms = state.scheduler.tasks.dead_mans_switch;
      if (dms) {
        var dmsAge = (Date.now() / 1000) - (dms.last_run || 0);
        var dmsOk = dmsAge < 129600 && (dms.consecutive_failures || 0) === 0;
        ctx.font = fontSize + 'px "Share Tech Mono", monospace';
        ctx.fillStyle = rgba(dmsOk ? C.green : C.red, 0.35);
        ctx.fillText('DEADMAN', tlX, subY);
        ctx.font = (fontSize - 1) + 'px "Share Tech Mono", monospace';
        ctx.fillStyle = rgba(dmsOk ? C.green : C.red, 0.5 * pulse);
        var dmsText = dmsOk ? 'OK (' + formatUptime(dmsAge) + ' ago)' : 'ALERT';
        ctx.fillText(dmsText, tlX, subY + fontSize + 1);
        subY += fontSize * 2 + 8;
      }
    }

    // ---- TOP-RIGHT: Clock + uptime ----
    var trX = W - panelPad;
    var trY = panelPad;

    ctx.textAlign = 'right';
    ctx.textBaseline = 'top';
    ctx.font = fontSize + 'px "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(C.cyan, 0.35);
    ctx.fillText('SYSTEM TIME', trX, trY);

    var now = new Date();
    var timeStr = now.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
    ctx.font = 'bold ' + bigFontSize + 'px "Orbitron", "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(C.cyan, 0.6 * pulse);
    ctx.fillText(timeStr, trX, trY + fontSize + 4);

    // Uptime
    var uptimeSec = 0;
    if (state && state.system) {
      uptimeSec = state.system.uptime_seconds || 0;
    }
    ctx.font = fontSize + 'px "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(C.cyan, 0.3);
    ctx.fillText('UPTIME: ' + formatUptime(uptimeSec), trX, trY + fontSize + bigFontSize + 12);

    // ---- BOTTOM-LEFT: Scheduler health + mini task grid ----
    var blX = panelPad;
    var blY = H - panelPad;
    var schedHealthy = 0, schedTotal = 0, schedRunning = 0;
    if (state && state.scheduler) {
      if (state.scheduler.info) {
        schedTotal = state.scheduler.info.tasks_total || 0;
        schedRunning = state.scheduler.info.tasks_running || 0;
        var failCount = (state.scheduler.failed_tasks || []).length;
        schedHealthy = schedTotal - failCount;
      } else if (state.scheduler.tasks) {
        var taskNames = Object.keys(state.scheduler.tasks);
        schedTotal = taskNames.length;
        schedHealthy = taskNames.filter(function (n) {
          var t = state.scheduler.tasks[n];
          return t.consecutive_failures === 0 && t.enabled !== false;
        }).length;
        schedRunning = taskNames.filter(function (n) {
          return state.scheduler.tasks[n].running;
        }).length;
      }
    }

    ctx.textAlign = 'left';
    ctx.textBaseline = 'bottom';
    ctx.font = fontSize + 'px "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(C.amber, 0.35);
    ctx.fillText('SCHEDULER', blX, blY - bigFontSize - 8);

    var schedColor = schedHealthy >= schedTotal ? C.green : (schedHealthy >= schedTotal * 0.8 ? C.amber : C.red);
    ctx.font = 'bold ' + bigFontSize + 'px "Orbitron", "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(schedColor, 0.65 * pulse);
    ctx.fillText(schedHealthy + '/' + schedTotal, blX, blY - 4);

    // Running indicator
    if (schedRunning > 0) {
      ctx.font = (fontSize - 1) + 'px "Share Tech Mono", monospace';
      ctx.fillStyle = rgba(C.cyan, 0.4 * pulse);
      ctx.fillText(schedRunning + ' running', blX + UNIT * 0.08, blY - 4 - (bigFontSize - fontSize) / 2);
    }

    // ---- BOTTOM-RIGHT: CPU/RAM mini gauges ----
    var brX = W - panelPad;
    var brY = H - panelPad;
    var cpuPct = 0, ramPct = 0;
    if (state && state.system) {
      cpuPct = state.system.cpu_percent || 0;
      if (state.system.memory) {
        ramPct = state.system.memory.percent || 0;
      }
    }

    ctx.textAlign = 'right';
    ctx.textBaseline = 'bottom';

    // CPU
    var cpuCol = cpuPct > 90 ? C.red : (cpuPct > 70 ? C.amber : C.cyan);
    ctx.font = fontSize + 'px "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(cpuCol, 0.35);
    ctx.fillText('CPU', brX - UNIT * 0.06, brY - bigFontSize - 8);

    ctx.font = 'bold ' + bigFontSize + 'px "Orbitron", "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(cpuCol, 0.6 * pulse);
    ctx.fillText(cpuPct.toFixed(0) + '%', brX - UNIT * 0.06, brY - 4);

    // Mini CPU bar
    var barW = UNIT * 0.04;
    var barH = 4;
    var barX = brX - UNIT * 0.06 - barW;
    var barY = brY - bigFontSize - 14;
    ctx.fillStyle = rgba(cpuCol, 0.08);
    ctx.fillRect(barX, barY, barW, barH);
    ctx.fillStyle = rgba(cpuCol, 0.35 * pulse);
    ctx.fillRect(barX, barY, barW * (cpuPct / 100), barH);

    // RAM
    var ramCol = ramPct > 90 ? C.red : (ramPct > 75 ? C.amber : C.green);
    ctx.font = fontSize + 'px "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(ramCol, 0.35);
    ctx.fillText('RAM', brX, brY - bigFontSize - 8);

    ctx.font = 'bold ' + bigFontSize + 'px "Orbitron", "Share Tech Mono", monospace';
    ctx.fillStyle = rgba(ramCol, 0.6 * pulse);
    ctx.fillText(ramPct.toFixed(0) + '%', brX, brY - 4);

    // Mini RAM bar
    var rBarX = brX - barW;
    ctx.fillStyle = rgba(ramCol, 0.08);
    ctx.fillRect(rBarX, barY, barW, barH);
    ctx.fillStyle = rgba(ramCol, 0.35 * pulse);
    ctx.fillRect(rBarX, barY, barW * (ramPct / 100), barH);

    ctx.restore();
  }

  // ================================================================
  //  HEATMAP: Mini task grid (bottom-left corner on heatmap canvas)
  // ================================================================

  function drawHeatmap(ts) {
    hmCtx.clearRect(0, 0, W, H);
    if (!heatmapTasks.length) return;

    var panelPad = Math.max(16, UNIT * 0.025);
    var cols = Math.min(9, heatmapTasks.length);
    var rows = Math.ceil(heatmapTasks.length / cols);
    var cellSize = Math.max(6, UNIT * 0.01);
    var gap = 2;
    var gridW = cols * (cellSize + gap);
    var gridH = rows * (cellSize + gap);
    var startX = panelPad;
    var startY = H - panelPad - UNIT * 0.09 - gridH;

    for (var i = 0; i < heatmapTasks.length; i++) {
      var task = heatmapTasks[i];
      var col = i % cols;
      var row = Math.floor(i / cols);
      var cx = startX + col * (cellSize + gap) + cellSize / 2;
      var cy = startY + row * (cellSize + gap) + cellSize / 2;

      var hColor;
      if (task.health === 'ok') hColor = C.green;
      else if (task.health === 'running') hColor = C.cyan;
      else if (task.health === 'failing') hColor = C.red;
      else if (task.health === 'disabled') hColor = C.dim;
      else hColor = C.amber;

      var age = Date.now() / 1000 - (task.lastRun || 0);
      var recency = Math.max(0.1, 1 - Math.min(1, age / 3600));

      // Tiny glowing square
      hmCtx.fillStyle = rgba(hColor, 0.2 * recency);
      hmCtx.fillRect(cx - cellSize / 2, cy - cellSize / 2, cellSize, cellSize);
      hmCtx.strokeStyle = rgba(hColor, 0.08 * recency);
      hmCtx.lineWidth = 0.5;
      hmCtx.strokeRect(cx - cellSize / 2, cy - cellSize / 2, cellSize, cellSize);
    }
  }

  // ================================================================
  //  STATE UPDATE from WebSocket data
  // ================================================================

  function updateFromState() {
    if (!state) return;

    // Gateway status
    if (state.gateway) {
      var gwOk = state.gateway.ok || state.gateway.status === 'up';
      // Update channel statuses
      var chStatus = state.gateway.ws_reachable ? 'up' : 'down';
      var channels = ['slack', 'discord', 'signal', 'imessage', 'email'];
      for (var ci = 0; ci < channels.length; ci++) {
        if (nodes[channels[ci]]) nodes[channels[ci]].status = chStatus;
      }
    }

    // Service statuses
    if (state.services) {
      for (var key in state.services) {
        if (nodes[key]) nodes[key].status = state.services[key].status;
      }
    }

    // OpenRouter is assumed up
    if (nodes.openrouter) nodes.openrouter.status = 'up';

    // Redis, Scheduler, PostgreSQL from dedicated fields
    if (state.redis && nodes.redis) nodes.redis.status = state.redis.status === 'ok' ? 'up' : 'down';
    if (state.scheduler && nodes.scheduler) nodes.scheduler.status = state.scheduler.status === 'ok' ? 'up' : 'down';
    if (state.postgresql && nodes.postgresql) nodes.postgresql.status = state.postgresql.status === 'ok' ? 'up' : 'down';

    // Activity levels from traffic_flow
    var maxFlow = 0;
    if (state.traffic_flow) {
      for (var tfKey in state.traffic_flow) {
        var val = state.traffic_flow[tfKey];
        if (nodes[tfKey]) nodes[tfKey].activity = Math.min(1, val);
        if (val > maxFlow) maxFlow = val;
      }
    }
    gatewayPulse = Math.min(1, maxFlow * 2);

    // Gateway queries
    if (state.gateway_queries) {
      gatewayReqSec = state.gateway_queries.reqs_per_sec || 0;
    }

    // Count active connections
    gatewayConnections = 0;
    for (var nid in nodes) {
      if (nodes[nid].status === 'up' || nodes[nid].status === 'ok') gatewayConnections++;
    }

    // Node gauge values
    // Ollama: model count / 6
    if (state.ollama && nodes.ollama) {
      nodes.ollama.gaugeValue = Math.min(1, (state.ollama.model_count || 0) / 6);
    }
    // Memory: total rows toward 2M
    if (state.postgresql && nodes.memory_server) {
      nodes.memory_server.gaugeValue = Math.min(1, (state.postgresql.total_rows || 0) / 2000000);
    }
    // Scheduler: health ratio
    if (state.scheduler && state.scheduler.tasks && nodes.scheduler) {
      var tasks = state.scheduler.tasks;
      var names = Object.keys(tasks);
      var total = names.length;
      var healthy = names.filter(function (n) {
        var t = tasks[n];
        return t.consecutive_failures === 0 && t.enabled !== false;
      }).length;
      nodes.scheduler.gaugeValue = total > 0 ? healthy / total : 1;
    }
    // Redis: just show up = 100% for now
    if (nodes.redis) {
      nodes.redis.gaugeValue = nodes.redis.status === 'up' ? 1.0 : 0;
    }
    // PostgreSQL
    if (nodes.postgresql) {
      nodes.postgresql.gaugeValue = nodes.postgresql.status === 'up' ? 1.0 : 0;
    }
    // SearXNG
    if (nodes.searxng) {
      nodes.searxng.gaugeValue = nodes.searxng.status === 'up' ? 1.0 : 0;
    }
    // Backends
    if (nodes.openrouter) {
      nodes.openrouter.gaugeValue = nodes.openrouter.status === 'up' ? 1.0 : 0;
    }
    if (nodes.mlx_chat) {
      nodes.mlx_chat.gaugeValue = nodes.mlx_chat.status === 'up' ? 1.0 : 0;
    }

    // Heatmap tasks
    if (state.scheduler && state.scheduler.tasks) {
      var sTasks = state.scheduler.tasks;
      heatmapTasks = Object.entries(sTasks).map(function (entry) {
        var name = entry[0];
        var t = entry[1];
        var health = 'ok';
        if (!t.enabled) health = 'disabled';
        else if (t.running) health = 'running';
        else if (t.consecutive_failures > 0) health = 'failing';
        else if (t.run_count === 0) health = 'unknown';
        return { name: name, health: health, lastRun: t.last_run || 0 };
      });
    }

    // Update DOM elements
    updateDOMStats();
    updateStatusLEDs();
    updateTicker();
  }

  // ================================================================
  //  DOM UPDATES
  // ================================================================

  function updateDOMStats() {
    var memEl = document.getElementById('stat-memories');
    if (state && state.postgresql && memEl) {
      memEl.textContent = formatNumber(state.postgresql.total_rows || 0);
    }

    var todayEl = document.getElementById('stat-today');
    if (state && state.postgresql && todayEl) {
      var todayCount = state.postgresql.today_count || 0;
      todayEl.textContent = todayCount > 0 ? '+' + formatNumber(todayCount) : '0';
      todayEl.className = 'hud-stat-value' + (todayCount > 1000 ? ' today-active' : '');
    }

    // Apps status (up/total)
    var appsEl = document.getElementById('stat-apps');
    if (state && state.app_watchdog && appsEl) {
      var aw = state.app_watchdog;
      var appsUp = (aw.apps || []).filter(function(a) { return a.status === 'up'; }).length;
      var appsTotal = (aw.apps || []).length;
      appsEl.textContent = appsUp + '/' + appsTotal;
      appsEl.className = 'hud-stat-value' + (appsUp < appsTotal ? ' warning' : '');
    }

    // Channels status
    var chEl = document.getElementById('stat-channels');
    if (state && chEl) {
      var chUp = 0;
      var chTotal = 5;
      if (state.services) {
        var chKeys = ['slack', 'discord', 'signal', 'imessage', 'email'];
        for (var ci = 0; ci < chKeys.length; ci++) {
          var svc = state.services[chKeys[ci]];
          if (svc && (svc.status === 'up' || svc.status === 'ok')) chUp++;
        }
      } else if (state.gateway && state.gateway.ok) {
        chUp = chTotal;
      }
      chEl.textContent = chUp + '/' + chTotal;
      chEl.className = 'hud-stat-value' + (chUp < chTotal ? ' warning' : '');
    }

    // NAS status
    var nasEl = document.getElementById('stat-nas');
    if (state && state.synology && nasEl) {
      var nasStatus = state.synology.status;
      if (nasStatus === 'ok') {
        nasEl.textContent = 'OK';
        nasEl.className = 'hud-stat-value';
      } else if (nasStatus === 'unavailable') {
        nasEl.textContent = 'SLEEP';
        nasEl.className = 'hud-stat-value warning';
      } else {
        nasEl.textContent = 'DOWN';
        nasEl.className = 'hud-stat-value critical';
      }
    }

    var schedEl = document.getElementById('stat-scheduler');
    if (state && state.scheduler && state.scheduler.info && schedEl) {
      var info = state.scheduler.info;
      var running = info.tasks_running || 0;
      var total = info.tasks_enabled || info.tasks_total || 0;
      schedEl.textContent = running + '/' + total;
      if (state.scheduler.failed_tasks && state.scheduler.failed_tasks.length > 0) {
        schedEl.classList.add('warning');
      } else {
        schedEl.classList.remove('warning');
      }
    }

    var uptimeEl = document.getElementById('stat-uptime');
    if (state && state.system && uptimeEl) {
      uptimeEl.textContent = formatUptime(state.system.uptime_seconds || 0);
    }

    var cpuEl = document.getElementById('stat-cpu');
    if (state && state.system && cpuEl) {
      var cpu = state.system.cpu_percent || 0;
      cpuEl.textContent = cpu.toFixed(0) + '%';
      cpuEl.className = 'hud-stat-value' + (cpu > 90 ? ' critical' : cpu > 70 ? ' warning' : '');
    }

    var ramEl = document.getElementById('stat-ram');
    if (state && state.system && state.system.memory && ramEl) {
      var pct = state.system.memory.percent || 0;
      ramEl.textContent = pct.toFixed(0) + '%';
      ramEl.className = 'hud-stat-value' + (pct > 90 ? ' critical' : pct > 75 ? ' warning' : '');
    }

    var modelsEl = document.getElementById('stat-models');
    if (state && state.ollama && modelsEl) {
      modelsEl.textContent = (state.ollama.model_count || 0).toString();
    }
  }

  // ---- Status LEDs ----
  var LED_SERVICES = [
    { key: 'gateway', label: 'GW' },
    { key: 'ollama', label: 'OLL' },
    { key: 'scheduler', label: 'SCH' },
    { key: 'redis', label: 'RED' },
    { key: 'postgresql', label: 'PG' },
    { key: 'memory_server', label: 'MEM' },
    { key: 'searxng', label: 'SRX' },
    { key: 'homebridge', label: 'HB' },
    { key: 'nas', label: 'NAS' },
  ];

  var ledsInitialized = false;

  function initStatusLEDs() {
    var container = document.getElementById('hud-status-leds');
    for (var i = 0; i < LED_SERVICES.length; i++) {
      var svc = LED_SERVICES[i];
      var wrap = document.createElement('div');
      wrap.style.display = 'flex';
      wrap.style.alignItems = 'center';
      wrap.style.gap = '4px';
      var led = document.createElement('div');
      led.className = 'status-led';
      led.id = 'led-' + svc.key;
      var lbl = document.createElement('span');
      lbl.className = 'led-label';
      lbl.textContent = svc.label;
      wrap.appendChild(led);
      wrap.appendChild(lbl);
      container.appendChild(wrap);
    }
    ledsInitialized = true;
  }

  function updateStatusLEDs() {
    if (!ledsInitialized) return;

    // Gateway special handling
    var gwNode = { status: 'unknown' };
    if (state && state.gateway) {
      gwNode.status = state.gateway.ok ? 'up' : 'down';
    }

    for (var i = 0; i < LED_SERVICES.length; i++) {
      var svc = LED_SERVICES[i];
      var led = document.getElementById('led-' + svc.key);
      if (!led) continue;

      var s = 'unknown';
      if (svc.key === 'gateway') {
        s = gwNode.status;
      } else if (svc.key === 'homebridge' && state && state.homebridge) {
        s = state.homebridge.status === 'ok' ? 'up' : state.homebridge.status;
      } else if (svc.key === 'nas' && state && state.synology) {
        s = state.synology.status === 'ok' ? 'up' : (state.synology.status === 'unavailable' ? 'warning' : 'down');
      } else if (nodes[svc.key]) {
        s = nodes[svc.key].status;
      }

      led.className = 'status-led ' + (
        s === 'up' || s === 'ok' ? 'up' :
        s === 'down' || s === 'error' ? 'down' :
        s === 'warning' || s === 'degraded' ? 'warning' : ''
      );
    }
  }

  // ---- Ticker ----
  function updateTicker() {
    if (!state) return;

    if (state.scheduler && state.scheduler.tasks) {
      for (var name in state.scheduler.tasks) {
        var t = state.scheduler.tasks[name];
        if (t.running) addTickerEvent(name + ' running...');
        if (t.last_duration && t.run_count > 0) {
          addTickerEvent(name + ' completed in ' + t.last_duration.toFixed(0) + 's');
        }
      }
    }

    if (state.postgresql) {
      var rows = state.postgresql.total_rows || 0;
      if (rows > 0) addTickerEvent('memory_count: ' + formatNumber(rows) + ' total memories');
    }

    if (state.ollama && state.ollama.models) {
      for (var m = 0; m < state.ollama.models.length; m++) {
        var model = state.ollama.models[m];
        addTickerEvent('ollama: ' + model.name + ' loaded (' + model.vram_gb + ' GB VRAM)');
      }
    }

    if (state.alerts) {
      for (var a = 0; a < state.alerts.length; a++) {
        var alert = state.alerts[a];
        addTickerEvent('[' + alert.severity.toUpperCase() + '] ' + alert.message);
      }
    }

    // New subsystem events
    if (state.app_watchdog && state.app_watchdog.apps) {
      var downApps = state.app_watchdog.apps.filter(function(a) { return a.status !== 'up'; });
      for (var da = 0; da < downApps.length; da++) {
        addTickerEvent('APP DOWN: ' + downApps[da].name + ' (port ' + downApps[da].port + ')');
      }
    }

    if (state.weather && state.weather.temperature) {
      addTickerEvent('weather: ' + state.weather.temperature + ' ' + (state.weather.conditions || ''));
    }

    if (state.synology && state.synology.status === 'ok') {
      addTickerEvent('nas: ' + (state.synology.model || 'Synology') + ' online — RAM ' + (state.synology.ram_pct || 0) + '%');
    }

    if (state.dream && state.dream.status === 'ok') {
      addTickerEvent('dream_pipeline: last run ' + formatUptime((Date.now()/1000) - (state.dream.last_run || 0)) + ' ago');
    }

    renderTicker();
  }

  function addTickerEvent(text) {
    if (_tickerSeen.has(text)) return;
    _tickerSeen.add(text);
  }

  function renderTicker() {
    var el = document.getElementById('hud-ticker');
    if (!el) return;

    var items = Array.from(_tickerSeen);
    _tickerSeen.clear();
    if (items.length === 0) return;

    for (var i = 0; i < items.length; i++) {
      if (tickerEvents.length >= MAX_TICKER) tickerEvents.shift();
      tickerEvents.push(items[i]);
    }

    var now = new Date();
    var timeStr = now.toLocaleTimeString('en-US', { hour12: false });
    var html = '';
    for (var j = 0; j < tickerEvents.length; j++) {
      html += '<span class="ticker-item"><span class="tick-time">' + timeStr + '</span> <span class="tick-text">' + escapeHtml(tickerEvents[j]) + '</span></span>';
    }
    el.innerHTML = html + html;

    var contentWidth = el.scrollWidth / 2;
    var speed = Math.max(30, contentWidth / 80);
    el.style.animationDuration = speed + 's';
  }

  // ---- Clock ----
  function updateClock() {
    var el = document.getElementById('hud-clock');
    if (!el) return;
    var now = new Date();
    el.textContent = now.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  // ---- Utility ----
  function formatNumber(n) {
    if (n >= 1000000) return (n / 1000000).toFixed(2) + 'M';
    if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
    return n.toString();
  }

  function formatUptime(seconds) {
    var d = Math.floor(seconds / 86400);
    var h = Math.floor((seconds % 86400) / 3600);
    var m = Math.floor((seconds % 3600) / 60);
    if (d > 0) return d + 'd ' + h + 'h';
    if (h > 0) return h + 'h ' + m + 'm';
    return m + 'm';
  }

  function escapeHtml(str) {
    return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // ---- WebSocket ----
  function connectWS() {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    var url = proto + '//' + location.host + '/ws';
    ws = new WebSocket(url);

    ws.onopen = function () {
      wsRetryDelay = 1000;
    };

    ws.onmessage = function (evt) {
      try {
        state = JSON.parse(evt.data);
        updateFromState();
      } catch (e) {
        // ignore parse errors
      }
    };

    ws.onclose = function () {
      state = null;
      setTimeout(connectWS, wsRetryDelay);
      wsRetryDelay = Math.min(wsRetryDelay * 1.5, 15000);
    };

    ws.onerror = function () {
      ws.close();
    };
  }

  // ================================================================
  //  MAIN RENDER LOOP
  // ================================================================

  function animate(ts) {
    ctx.clearRect(0, 0, W, H);

    // Layer 0: Background radial grid
    drawRadialGrid(ts);

    // Layer 1: Heatmap (task grid, bottom-left)
    drawHeatmap(ts);

    // Layer 2: Orbit rings (dashed)
    drawOrbitRings(ts);

    // Layer 3: Connection lines (behind everything else)
    for (var id in nodes) {
      drawConnectionLine(nodes[id], ts);
    }

    // Layer 4: Spawn and draw particles
    spawnParticles();
    particles = particles.filter(function (p) {
      // Update position: both forward and reverse use the same update()
      var alive = p.update();
      // For reverse particles, alive means t > 0
      if (p._reverse) alive = p.t > 0;
      if (alive) {
        p.draw();
        return true;
      }
      return false;
    });

    // Layer 5: Dust particles (ambient)
    for (var d = 0; d < dustParticles.length; d++) {
      dustParticles[d].update();
      dustParticles[d].draw();
    }

    // Layer 6: Central gateway (the big circle)
    drawGateway(ts);

    // Layer 7: Orbital nodes
    for (var nid in nodes) {
      drawOrbitalNode(nodes[nid], ts);
    }

    // Layer 8: Corner panels (drawn on top)
    drawCornerPanels(ts);

    requestAnimationFrame(animate);
  }

  // ================================================================
  //  BOOT
  // ================================================================

  function init() {
    initNodes();
    initStatusLEDs();
    resize();
    window.addEventListener('resize', resize);

    updateClock();
    setInterval(updateClock, 1000);

    connectWS();
    requestAnimationFrame(animate);
  }

  init();
})();
