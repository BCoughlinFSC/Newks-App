/**
 * Shared Navigation Sidebar — Newk's Reporting Platform
 *
 * Include on any page:
 *   <div id="shared-sidebar"></div>
 *   <script src="/shared-nav.js"></script>
 *
 * The page must use a grid layout:
 *   .container { display: grid; grid-template-columns: 280px 1fr; }
 */

(function () {
  'use strict';

  // ─── Nav structure ──────────────────────────────
  const REPORT_LINKS = [
    { key: 'sales_summary', label: 'Sales Summary',    href: '/legacy.html?report=sales_summary' },
    { key: 'metrics',       label: 'Net Sales',         href: '/legacy.html?report=metrics' },
    { key: 'menu',          label: 'Menu Performance',  href: '/legacy.html?report=menu' },
    { key: 'guest_count',   label: 'Guest Count',       href: '/legacy.html?report=guest_count' },
    { key: 'royalty',       label: 'Royalty Report',     href: '/legacy.html?report=royalty' },
    { key: 'tip_pool',      label: 'Payroll Report',      href: '/legacy.html?report=tip_pool' },
    { key: 'pick3',         label: 'Pick 3',             href: '/pick3.html' },
    { key: 'pick2',         label: 'Pick 2',             href: '/pick2.html' },
    { key: 'full-pairings', label: 'Full Pairings',      href: '/full-pairings.html' },
    { key: 'wpy',           label: 'Weekly Sales (WPY)',  href: '/wpy.html' },
    { key: 'daily-flash',   label: 'Daily Sales Flash',   href: '/daily-flash.html' },
    { key: 'menu-mix',      label: 'Menu Mix Report',     href: '/menu-mix-report.html' },
    { key: 'catering',      label: 'Catering Reports',    href: '/catering.html' },
    { key: 'loyalty',       label: 'Loyalty Reports',     href: '/loyalty.html' },
    { key: 'food-cost',     label: 'Food Cost (COGS)',    href: '/food-cost.html' },
  ];

  const ADMIN_LINKS = [
    { key: 'mapping',          label: 'Mapping Table',               href: '/mapping.html' },
    { key: 'msi-discrepancy',  label: 'MSI Discrepancy',             href: '/msi-discrepancy.html' },
    { key: 'toast-pricing',    label: 'Toast Pricing Export',         href: '#', action: 'toastPricing' },
    { key: 'olo',              label: 'Digital Orders by Platform',   href: '/olo.html' },
  ];

  // ─── Detect active page ─────────────────────────
  function getActivePage() {
    const path = window.location.pathname.replace(/\/$/, '');
    const params = new URLSearchParams(window.location.search);

    if (path === '' || path === '/' || path.endsWith('/index.html')) {
      return 'home';
    }
    if (path.endsWith('/legacy.html')) {
      return params.get('report') || 'sales_summary';
    }
    if (path.endsWith('/pick3.html'))                return 'pick3';
    if (path.endsWith('/pick2.html'))                return 'pick2';
    if (path.endsWith('/full-pairings.html'))        return 'full-pairings';
    if (path.endsWith('/wpy.html'))                  return 'wpy';
    if (path.endsWith('/daily-flash.html'))         return 'daily-flash';
    if (path.endsWith('/menu-mix-report.html'))     return 'menu-mix';
    if (path.endsWith('/olo.html'))                   return 'olo';
    if (path.endsWith('/catering.html'))              return 'catering';
    if (path.endsWith('/loyalty.html'))               return 'loyalty';
    if (path.endsWith('/food-cost.html'))             return 'food-cost';
    if (path.endsWith('/mapping.html'))               return 'mapping';
    if (path.endsWith('/msi-discrepancy.html'))       return 'msi-discrepancy';
    if (path.endsWith('/toast-reporting-guide.html')) return 'guide';
    if (path.endsWith('/admin.html'))                 return 'admin';
    return '';
  }

  // ─── Auth helpers ───────────────────────────────
  function getUser() {
    try { return JSON.parse(localStorage.getItem('user') || 'null'); } catch { return null; }
  }

  function getAuthToken() {
    return localStorage.getItem('auth_token') || '';
  }

  function userCanAccess(pageKey) {
    const user = getUser();
    if (!user) return false;
    if (user.role === 'admin') return true;
    const allowed = user.allowedPages || [];
    if (allowed.length === 0) return true;
    return allowed.includes(pageKey);
  }

  // ─── Escape helpers ─────────────────────────────
  function esc(s) {
    return String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  // ─── Build sidebar HTML ─────────────────────────
  function buildSidebar() {
    const user = getUser();
    const isAdmin = user && user.role === 'admin';
    const activePage = getActivePage();

    // Reports section
    let reportsHtml = REPORT_LINKS
      .filter(link => userCanAccess(link.key))
      .map(link => {
        const active = link.key === activePage ? ' active' : '';
        return `<a href="${link.href}" class="snav-link${active}" data-key="${link.key}">${esc(link.label)}</a>`;
      }).join('');

    // Guide link
    const guideActive = activePage === 'guide' ? ' active' : '';
    const guideHtml = `<a href="/toast-reporting-guide.html" class="snav-link${guideActive}" data-key="guide">Toast Reporting Guide</a>`;

    // Admin section
    let adminHtml = '';
    if (isAdmin) {
      const adminLinks = ADMIN_LINKS.map(link => {
        const active = link.key === activePage ? ' active' : '';
        const actionAttr = link.action ? ` data-action="${link.action}"` : '';
        return `<a href="${link.href}" class="snav-link${active}" data-key="${link.key}"${actionAttr}>${esc(link.label)}</a>`;
      }).join('');

      const adminPanelActive = activePage === 'admin' ? ' active' : '';

      adminHtml = `
        <div class="snav-section-label" style="margin-top: 0.5rem;">Admin & Data</div>
        ${adminLinks}
        <a href="/admin.html" class="snav-link${adminPanelActive}" data-key="admin">Admin Panel</a>
      `;
    }

    // Saved reports section
    const savedReportsHtml = `
      <div class="snav-card">
        <h2 style="margin:0 0 0.75rem;font-size:1rem;font-weight:700;color:#1E3A5F;">Saved Reports</h2>
        <button class="snav-btn snav-btn-primary snav-btn-full" id="snavNewReportBtn">+ New Report</button>
        <div style="margin-top:0.75rem;">
          <div class="snav-saved-list" id="snavSavedReportsList">
            <div class="snav-empty">Loading...</div>
          </div>
        </div>
      </div>
    `;

    // User profile section
    let userHtml = '';
    if (user) {
      const initials = user.name.split(' ').map(n => n[0]).join('').toUpperCase().slice(0, 2);
      userHtml = `
        <div class="snav-footer">
          <div class="snav-user-info">
            <div class="snav-avatar">${esc(initials)}</div>
            <div>
              <div class="snav-user-name">${esc(user.name)}</div>
              <div class="snav-user-role">${esc(user.role || 'user')}</div>
            </div>
          </div>
          <div class="snav-user-actions">
            <button class="snav-btn snav-btn-secondary snav-btn-full" id="snavLogoutBtn">Logout</button>
          </div>
        </div>
      `;
    }

    return `
      <div class="snav-card" style="flex:none;">
        <h3 style="margin:0 0 0.75rem;color:#C41E3A;font-size:0.95rem;font-weight:700;">Navigation</h3>
        <div class="snav-section-label">Reports</div>
        ${reportsHtml}
        <div style="margin-top:0.5rem;">
          ${guideHtml}
        </div>
        ${adminHtml}
      </div>
      ${savedReportsHtml}
      ${userHtml}
    `;
  }

  // ─── Load saved reports ─────────────────────────
  async function loadSavedReports() {
    const list = document.getElementById('snavSavedReportsList');
    const token = getAuthToken();
    if (!list) return;

    if (!token) {
      list.innerHTML = '<div class="snav-empty">Login to view saved reports</div>';
      return;
    }

    try {
      const resp = await fetch('/api/saved-reports', {
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` }
      });
      if (!resp.ok) throw new Error('Failed to load');
      const reports = await resp.json();

      if (reports.length === 0) {
        list.innerHTML = '<div class="snav-empty">No saved reports yet</div>';
        return;
      }

      list.innerHTML = reports.map(r => `
        <div class="snav-report-item" data-id="${esc(r.id)}">
          <div class="snav-report-name">${esc(r.name)}</div>
          <div class="snav-report-date">Created ${new Date(r.createdAt).toLocaleDateString()}</div>
        </div>
      `).join('');

      list.querySelectorAll('.snav-report-item').forEach(item => {
        item.addEventListener('click', () => {
          const reportId = item.dataset.id;
          // If we're on legacy.html and the function exists, load inline
          if (typeof window.loadReport === 'function') {
            window.loadReport(reportId);
          } else {
            window.location.href = `/legacy.html?loadReport=${reportId}`;
          }
        });
      });
    } catch (err) {
      list.innerHTML = '<div class="snav-empty" style="color:#ff8080;">Error loading reports</div>';
    }
  }

  // ─── Wire up event handlers ─────────────────────
  function wireEvents() {
    // New Report button
    const newBtn = document.getElementById('snavNewReportBtn');
    if (newBtn) {
      newBtn.addEventListener('click', () => {
        if (typeof window.newReport === 'function') {
          window.newReport();
        } else {
          window.location.href = '/legacy.html';
        }
      });
    }

    // Logout button
    const logoutBtn = document.getElementById('snavLogoutBtn');
    if (logoutBtn) {
      logoutBtn.addEventListener('click', () => {
        localStorage.removeItem('auth_token');
        localStorage.removeItem('user');
        window.location.href = '/legacy.html';
      });
    }

    // Toast Pricing Export action
    document.querySelectorAll('[data-action="toastPricing"]').forEach(link => {
      link.addEventListener('click', (e) => {
        e.preventDefault();
        showPricingExportModal();
      });
    });
  }

  // ─── Toast Pricing Export Modal ─────────────────
  function showPricingExportModal() {
    // Remove existing modal if any
    const existing = document.getElementById('pricingExportModal');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.id = 'pricingExportModal';
    overlay.className = 'modal-overlay';
    overlay.innerHTML = `
      <div style="background:white;border-radius:12px;padding:2rem;max-width:420px;width:90%;box-shadow:0 10px 40px rgba(0,0,0,0.3);">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem;padding-bottom:0.75rem;border-bottom:2px solid #E5E7EB;">
          <h2 style="margin:0;color:#C41E3A;font-size:1.25rem;">Toast Pricing Export</h2>
          <button id="pricingModalClose" style="background:none;border:none;font-size:1.5rem;cursor:pointer;color:#6B7280;padding:0;width:32px;height:32px;display:flex;align-items:center;justify-content:center;border-radius:4px;">&times;</button>
        </div>
        <p style="color:#374151;margin:0 0 1.5rem;font-size:0.95rem;">Download the current Toast pricing file as a CSV?</p>
        <div style="display:flex;gap:0.75rem;justify-content:flex-end;">
          <button id="pricingModalCancel" style="padding:0.6rem 1.25rem;border-radius:6px;border:1px solid #D1D5DB;background:white;color:#374151;cursor:pointer;font-size:0.9rem;font-family:inherit;">Cancel</button>
          <button id="pricingModalDownload" style="padding:0.6rem 1.25rem;border-radius:6px;border:none;background:#C41E3A;color:white;cursor:pointer;font-size:0.9rem;font-family:inherit;font-weight:600;">Download</button>
        </div>
      </div>
    `;

    // Inject overlay styles if not already on page
    if (!document.querySelector('.modal-overlay')) {
      const s = document.createElement('style');
      s.textContent = `.modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);display:flex;justify-content:center;align-items:center;z-index:10000;}`;
      document.head.appendChild(s);
    }

    document.body.appendChild(overlay);

    const close = () => overlay.remove();
    overlay.querySelector('#pricingModalClose').addEventListener('click', close);
    overlay.querySelector('#pricingModalCancel').addEventListener('click', close);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });

    overlay.querySelector('#pricingModalDownload').addEventListener('click', async () => {
      const btn = overlay.querySelector('#pricingModalDownload');
      btn.disabled = true; btn.textContent = 'Downloading...';
      try {
        const url = new URL('/api/toast-pricing-export', window.location.origin);
        url.searchParams.set('group_by_tier', 'true');
        const headers = {};
        const token = getAuthToken();
        if (token) headers['Authorization'] = `Bearer ${token}`;
        const resp = await fetch(url.toString(), { headers });
        if (!resp.ok) {
          let errorText = await resp.text();
          try { const j = JSON.parse(errorText); errorText = j.detail || j.message || errorText; } catch(e) {}
          throw new Error(errorText || `HTTP ${resp.status}`);
        }
        const blob = await resp.blob();
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `toast_pricing_export_${new Date().toISOString().split('T')[0]}.csv`;
        a.style.visibility = 'hidden';
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        setTimeout(() => URL.revokeObjectURL(a.href), 100);
        close();
      } catch (err) {
        btn.disabled = false; btn.textContent = 'Download';
        alert(`Toast pricing export failed: ${err.message}`);
      }
    });
  }

  // ─── Inject CSS ─────────────────────────────────
  function injectStyles() {
    if (document.getElementById('shared-nav-styles')) return;
    const style = document.createElement('style');
    style.id = 'shared-nav-styles';
    style.textContent = `
      /* ─── Shared Nav Sidebar ────────────────────── */
      #shared-sidebar {
        display: flex;
        flex-direction: column;
        gap: 1rem;
      }

      .snav-card {
        background: #ffffff;
        border-radius: 12px;
        padding: 1.25rem;
        border: 1px solid #e8e0d5;
        box-shadow: 0 2px 8px rgba(0,0,0,0.08);
        display: flex;
        flex-direction: column;
      }

      .snav-section-label {
        font-size: 0.7rem;
        font-weight: 600;
        color: #666;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        padding: 0.25rem 0.5rem;
        margin-bottom: 0.25rem;
      }

      .snav-link {
        display: block;
        padding: 0.5rem 0.75rem;
        color: #2c2c2c;
        text-decoration: none;
        border-radius: 6px;
        margin-bottom: 0.25rem;
        font-size: 0.9rem;
        transition: all 0.2s;
      }
      .snav-link:hover {
        background: #F0F9FF;
        color: #C41E3A;
      }
      .snav-link.active {
        background: #C41E3A;
        color: #fff;
      }

      /* Saved reports */
      .snav-saved-list {
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
        max-height: 300px;
        overflow-y: auto;
        padding-right: 2px;
      }
      .snav-saved-list::-webkit-scrollbar { width: 6px; }
      .snav-saved-list::-webkit-scrollbar-track { background: #F8FAFC; border-radius: 4px; }
      .snav-saved-list::-webkit-scrollbar-thumb { background: #d4c9b8; border-radius: 4px; }

      .snav-report-item {
        background: #F8FAFC;
        border: 1px solid #e8e0d5;
        border-radius: 8px;
        padding: 0.75rem;
        cursor: pointer;
        transition: all 0.2s;
      }
      .snav-report-item:hover {
        background: #F0F9FF;
        border-color: #C41E3A;
      }
      .snav-report-name {
        font-weight: 600;
        color: #2c2c2c;
        font-size: 0.9rem;
        margin-bottom: 0.2rem;
      }
      .snav-report-date {
        font-size: 0.75rem;
        color: #666;
      }
      .snav-empty {
        text-align: center;
        padding: 1rem;
        color: #9ca3af;
        font-size: 0.85rem;
      }

      /* User profile */
      .snav-footer {
        margin-top: 0.5rem;
        padding-top: 1rem;
        border-top: 1px solid #e8e0d5;
      }
      .snav-user-info {
        display: flex;
        align-items: center;
        gap: 0.75rem;
        padding: 0.75rem;
        background: #F0F9FF;
        border-radius: 8px;
        margin-bottom: 0.75rem;
      }
      .snav-avatar {
        width: 38px; height: 38px;
        border-radius: 50%;
        background: #C41E3A;
        color: white;
        display: flex; align-items: center; justify-content: center;
        font-weight: 700; font-size: 0.85rem; flex-shrink: 0;
      }
      .snav-user-name {
        font-weight: 600; font-size: 0.9rem; color: #2c2c2c;
      }
      .snav-user-role {
        font-size: 0.75rem; color: #666; text-transform: capitalize;
      }
      .snav-user-actions {
        display: flex; flex-direction: column; gap: 0.5rem;
      }

      /* Buttons */
      .snav-btn {
        padding: 0.55rem 1.1rem;
        border-radius: 6px;
        border: none;
        font-weight: 600;
        cursor: pointer;
        transition: all 0.2s;
        font-size: 0.875rem;
        font-family: inherit;
      }
      .snav-btn:disabled { opacity: 0.55; cursor: not-allowed; }
      .snav-btn-primary { background: #C41E3A; color: #fff; }
      .snav-btn-primary:hover:not(:disabled) { background: #A01A2E; }
      .snav-btn-secondary { background: #6B7280; color: #fff; }
      .snav-btn-secondary:hover:not(:disabled) { background: #4B5563; }
      .snav-btn-full { width: 100%; }
    `;
    document.head.appendChild(style);
  }

  // ─── Initialize ─────────────────────────────────
  function init() {
    const container = document.getElementById('shared-sidebar');
    if (!container) return;

    injectStyles();
    container.innerHTML = buildSidebar();
    wireEvents();
    loadSavedReports();
  }

  // Run on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Expose rebuild for pages that update auth state
  window.rebuildSharedNav = function () {
    init();
  };

})();
