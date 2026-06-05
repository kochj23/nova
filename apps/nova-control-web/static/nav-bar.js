// Nova Dashboard Navigation Bar — auto-injected into all pages
(function() {
  const NAV_LINKS = [
    {href: '/', label: 'HUD'},
    {href: '/gauges', label: 'Gauges'},
    {href: '/bb', label: 'Big Brother'},
    {href: '/mrtg', label: 'MRTG'},
    {href: '/birdseye', label: 'Birdseye'},
    {href: '/journal', label: 'Journal'},
    {href: '/analytics', label: 'Analytics'},
  ];

  const nav = document.createElement('nav');
  nav.id = 'nova-nav';
  nav.innerHTML = NAV_LINKS.map(l => {
    const active = window.location.pathname === l.href || 
                   (l.href !== '/' && window.location.pathname.startsWith(l.href));
    return `<a href="${l.href}" class="${active ? 'active' : ''}">${l.label}</a>`;
  }).join('');

  const style = document.createElement('style');
  style.textContent = `
    #nova-nav {
      position: fixed; top: 0; left: 0; right: 0; z-index: 9999;
      display: flex; align-items: center; gap: 2px;
      background: #0d1117; border-bottom: 1px solid #21262d;
      padding: 6px 16px; font-family: -apple-system, system-ui, monospace;
      font-size: 12px; overflow-x: auto;
    }
    #nova-nav a {
      color: #8b949e; text-decoration: none; padding: 4px 10px;
      border-radius: 4px; white-space: nowrap; transition: all 0.15s;
    }
    #nova-nav a:hover { color: #e6edf3; background: #21262d; }
    #nova-nav a.active { color: #58a6ff; background: #1c2128; font-weight: 600; }
    body { padding-top: 36px !important; }
  `;

  document.head.appendChild(style);
  document.body.prepend(nav);
})();
