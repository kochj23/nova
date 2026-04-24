function drawLineChart(canvas, series, options = {}) {
  const rect = canvas.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const W = options.width || rect.width;
  const H = options.height || 140;
  canvas.width = W * dpr;
  canvas.height = H * dpr;
  canvas.style.width = W + 'px';
  canvas.style.height = H + 'px';
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const pad = { top: 10, right: 12, bottom: 24, left: 48 };
  const cw = W - pad.left - pad.right;
  const ch = H - pad.top - pad.bottom;

  const isLight = document.documentElement.dataset.theme === 'light';
  const gridColor = isLight ? 'rgba(0,0,0,0.06)' : 'rgba(255,255,255,0.04)';
  const textColor = isLight ? 'rgba(0,0,0,0.4)' : 'rgba(255,255,255,0.3)';

  let allValues = [];
  for (const s of series) {
    for (const p of s.data) allValues.push(p.value);
  }
  if (allValues.length === 0) return;

  const minVal = options.minY ?? Math.min(...allValues);
  const maxVal = options.maxY ?? Math.max(...allValues);
  const range = maxVal - minVal || 1;

  let allTs = [];
  for (const s of series) {
    for (const p of s.data) allTs.push(p.ts);
  }
  const minTs = Math.min(...allTs);
  const maxTs = Math.max(...allTs);
  const tsRange = maxTs - minTs || 1;

  function x(ts) { return pad.left + ((ts - minTs) / tsRange) * cw; }
  function y(val) { return pad.top + ch - ((val - minVal) / range) * ch; }

  ctx.clearRect(0, 0, W, H);

  // Grid lines
  ctx.strokeStyle = gridColor;
  ctx.lineWidth = 0.5;
  for (let i = 0; i <= 4; i++) {
    const yy = pad.top + (ch / 4) * i;
    ctx.beginPath(); ctx.moveTo(pad.left, yy); ctx.lineTo(W - pad.right, yy); ctx.stroke();
  }

  // Y-axis labels
  ctx.fillStyle = textColor;
  ctx.font = '9px monospace';
  ctx.textAlign = 'right';
  for (let i = 0; i <= 4; i++) {
    const val = maxVal - (range / 4) * i;
    const yy = pad.top + (ch / 4) * i;
    ctx.fillText(formatChartValue(val, options.unit), pad.left - 4, yy + 3);
  }

  // X-axis labels
  ctx.textAlign = 'center';
  const labelCount = Math.min(6, Math.floor(cw / 80));
  for (let i = 0; i <= labelCount; i++) {
    const ts = minTs + (tsRange / labelCount) * i;
    const xx = x(ts);
    ctx.fillText(formatChartTime(ts, tsRange), xx, H - 4);
  }

  // Draw series
  for (const s of series) {
    if (s.data.length < 2) continue;
    const sorted = [...s.data].sort((a, b) => a.ts - b.ts);

    if (options.fill !== false) {
      ctx.beginPath();
      ctx.moveTo(x(sorted[0].ts), y(0));
      for (const p of sorted) ctx.lineTo(x(p.ts), y(p.value));
      ctx.lineTo(x(sorted[sorted.length - 1].ts), y(0));
      ctx.closePath();
      ctx.fillStyle = (s.color || 'rgba(0,255,200,1)').replace(/[\d.]+\)$/, '0.08)');
      ctx.fill();
    }

    ctx.beginPath();
    for (let i = 0; i < sorted.length; i++) {
      const px = x(sorted[i].ts);
      const py = y(sorted[i].value);
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.strokeStyle = s.color || 'rgba(0,255,200,0.7)';
    ctx.lineWidth = 1.5;
    ctx.stroke();
  }

  // Legend
  if (series.length > 1) {
    ctx.font = '9px monospace';
    ctx.textAlign = 'left';
    let lx = pad.left + 4;
    for (const s of series) {
      ctx.fillStyle = s.color || 'rgba(0,255,200,0.7)';
      ctx.fillRect(lx, pad.top + 2, 10, 3);
      lx += 14;
      ctx.fillText(s.label || '', lx, pad.top + 7);
      lx += ctx.measureText(s.label || '').width + 12;
    }
  }
}

function formatChartValue(val, unit) {
  if (unit === '%') return val.toFixed(0) + '%';
  if (unit === 'ms') return val.toFixed(0) + 'ms';
  if (unit === '$') return '$' + val.toFixed(3);
  if (unit === 'GB') return val.toFixed(1) + 'GB';
  if (val >= 1e6) return (val / 1e6).toFixed(1) + 'M';
  if (val >= 1e3) return (val / 1e3).toFixed(1) + 'K';
  return val.toFixed(0);
}

function formatChartTime(ts, totalRange) {
  const d = new Date(ts * 1000);
  if (totalRange > 86400 * 2) {
    return (d.getMonth() + 1) + '/' + d.getDate();
  }
  return d.getHours().toString().padStart(2, '0') + ':' + d.getMinutes().toString().padStart(2, '0');
}

function renderTimeRangeSelector(containerId, metric, onData) {
  const ranges = ['1h', '6h', '24h', '7d'];
  let html = '<div class="time-range-selector">';
  for (const r of ranges) {
    html += `<button class="time-range-btn" data-range="${r}">${r}</button>`;
  }
  html += '</div><canvas class="trend-chart"></canvas>';

  const container = document.getElementById(containerId) || document.createElement('div');
  container.innerHTML = html;

  const canvas = container.querySelector('.trend-chart');
  const buttons = container.querySelectorAll('.time-range-btn');

  function load(range) {
    buttons.forEach(b => b.classList.toggle('active', b.dataset.range === range));
    fetch(`/api/history/${metric}?range=${range}`)
      .then(r => r.json())
      .then(data => onData(canvas, data, range))
      .catch(() => {});
  }

  buttons.forEach(b => b.addEventListener('click', () => load(b.dataset.range)));
  load('6h');
  return container;
}
