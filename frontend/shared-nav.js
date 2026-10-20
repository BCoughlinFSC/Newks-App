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
  // Reports are organized into categories. Reorder items inside each
  // group (or reorder the groups themselves) by editing this array.
  // Reports still owned by this app. Sales, menu and labor reporting moved to
  // the Snowflake / Power BI dashboards at fscreporting.com (2026-08) — those
  // entries were removed rather than hidden, because they are not coming back.
  // What's left is the app's remaining charter: vendor reporting (Monkey Media
  // catering, loyalty) and the Toast-API reports nobody else runs.
  const REPORT_GROUPS = [
    {
      label: 'Catering & Loyalty',
      links: [
        { key: 'catering', label: 'Catering Reports', href: '/catering.html' },
        { key: 'loyalty',  label: 'Loyalty Reports',  href: '/loyalty.html' },
      ],
    },
    {
      label: 'Operations',
      links: [
        { key: 'tax-exempt-sc',  label: 'Tax-Exempt Svc Chg', href: '/tax-exempt-service-charges.html' },
        { key: 'deferred-items', label: 'Deferred Items',     href: '/deferred-items.html' },
        { key: 'spirit-night',   label: 'Spirit Night',       href: '/spirit-night.html' },
      ],
    },
  ];

  const ADMIN_LINKS = [
    { key: 'mapping',          label: 'Mapping Table',               href: '/mapping.html' },
    { key: 'msi-discrepancy',  label: 'MSI Discrepancy',             href: '/msi-discrepancy.html' },
    { key: 'toast-pricing',    label: 'Toast Pricing Export',         href: '#', action: 'toastPricing' },
    { key: 'olo',              label: 'Digital Orders by Platform',   href: '/olo.html' },
    { key: 'contacts',         label: 'Contacts (forms.nfc.local)',   href: '/contacts.html' },
    { key: 'integrations',     label: 'Integrations / Re-run',        href: '/integrations.html' },
    { key: 'subscriptions',    label: 'Report Subscriptions',         href: '/subscriptions.html' },
    { key: 'onboarding',       label: 'NRO Onboarding',               href: '/onboarding.html' },
    { key: 'toast-tools',      label: 'ToastWeb Bulk Edit Tools',     href: '/toast-tools.html' },
    { key: 'mealcard',         label: 'Meal Cards',                   href: '/mealcard.html' },
  ];

  // ─── Which links the sidebar actually shows ─────
  // Narrowed 2026-10-20 to the handful of things people still come here for.
  // This HIDES, it does not remove: every other page is still deployed and
  // still works from a bookmark or a direct URL, and nothing server-side
  // changed. Reports whose home really has moved were deleted from the lists
  // above instead, so a hidden link here means "not front and centre", not
  // "gone".
  //
  // To put the full sidebar back, set this to null. To show one more thing,
  // add its key from REPORT_GROUPS or ADMIN_LINKS above.
  const VISIBLE_KEYS = [
    'spirit-night',    // Spirit Night
    'subscriptions',   // Report Subscriptions
    'onboarding',      // NRO Onboarding
    'mealcard',        // Meal Cards
    'contacts',        // Contacts (forms.nfc.local)
    'admin',           // Admin Panel
  ];

  function isVisible(key) {
    return !VISIBLE_KEYS || VISIBLE_KEYS.includes(key);
  }

  // ─── Detect active page ─────────────────────────
  function getActivePage() {
    const path = window.location.pathname.replace(/\/$/, '');

    if (path === '' || path === '/' || path.endsWith('/index.html')) {
      return 'home';
    }
    if (path.endsWith('/olo.html'))                   return 'olo';
    if (path.endsWith('/catering.html'))              return 'catering';
    if (path.endsWith('/loyalty.html'))               return 'loyalty';
    if (path.endsWith('/spirit-night.html'))          return 'spirit-night';
    if (path.endsWith('/tax-exempt-service-charges.html')) return 'tax-exempt-sc';
    if (path.endsWith('/deferred-items.html'))        return 'deferred-items';
    if (path.endsWith('/mapping.html'))               return 'mapping';
    if (path.endsWith('/msi-discrepancy.html'))       return 'msi-discrepancy';
    if (path.endsWith('/toast-reporting-guide.html')) return 'guide';
    if (path.endsWith('/admin.html'))                 return 'admin';
    if (path.endsWith('/projects.html'))              return 'projects';
    if (path.endsWith('/onboarding.html'))            return 'onboarding';
    if (path.endsWith('/subscriptions.html'))         return 'subscriptions';
    if (path.endsWith('/toast-tools.html'))           return 'toast-tools';
    if (path.endsWith('/mealcard.html'))              return 'mealcard';
    if (path.startsWith('/contacts'))                 return 'contacts';
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
    // Empty allowedPages = no access (admins still see everything).
    // This avoids accidentally granting full access to invited users
    // when an admin forgets to check any boxes.
    const allowed = user.allowedPages || [];
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

    // Reports section — render each group with its own section label.
    // Skip groups whose visible links are all filtered out by permissions.
    let reportsHtml = REPORT_GROUPS.map(group => {
      const visibleLinks = group.links.filter(
        link => userCanAccess(link.key) && isVisible(link.key));
      if (visibleLinks.length === 0) return '';

      const linksHtml = visibleLinks.map(link => {
        const active = link.key === activePage ? ' active' : '';
        return `<a href="${link.href}" class="snav-link${active}" data-key="${link.key}">${esc(link.label)}</a>`;
      }).join('');

      return `
        <div class="snav-group-label">${esc(group.label)}</div>
        ${linksHtml}
      `;
    }).join('');

    // Guide link — hidden pending SSRS rebuild (toast-reporting-guide.html not present)
    const guideHtml = '';

    // Projects board — shared team ticket board. Visible to every logged-in
    // user (not permission-gated), like the Guide link.
    const projectsActive = activePage === 'projects' ? ' active' : '';
    const projectsHtml = (user && isVisible('projects'))
      ? `<div class="snav-section-label" style="margin-top:0.5rem;">Team</div>
         <a href="/projects.html" class="snav-link${projectsActive}" data-key="projects">Projects</a>`
      : '';

    // Admin section
    let adminHtml = '';
    if (isAdmin) {
      const adminLinks = ADMIN_LINKS.filter(link => isVisible(link.key)).map(link => {
        const active = link.key === activePage ? ' active' : '';
        const actionAttr = link.action ? ` data-action="${link.action}"` : '';
        return `<a href="${link.href}" class="snav-link${active}" data-key="${link.key}"${actionAttr}>${esc(link.label)}</a>`;
      }).join('');

      const adminPanelActive = activePage === 'admin' ? ' active' : '';
      const adminPanelHtml = isVisible('admin')
        ? `<a href="/admin.html" class="snav-link${adminPanelActive}" data-key="admin">Admin Panel</a>`
        : '';

      // Only draw the heading if something sits under it.
      if (adminLinks || adminPanelHtml) {
        adminHtml = `
          <div class="snav-section-label" style="margin-top: 0.5rem;">Admin & Data</div>
          ${adminLinks}
          ${adminPanelHtml}
        `;
      }
    }

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
        ${reportsHtml ? `<div class="snav-section-label">Reports</div>${reportsHtml}` : ''}
        <div style="margin-top:0.5rem;">
          ${guideHtml}
        </div>
        ${projectsHtml}
        ${adminHtml}
      </div>
      ${userHtml}
    `;
  }

  // ─── Wire up event handlers ─────────────────────
  function wireEvents() {
    // Logout button — clear auth state and bounce to the canonical
    // home/login page so the user always sees the same login modal.
    const logoutBtn = document.getElementById('snavLogoutBtn');
    if (logoutBtn) {
      logoutBtn.addEventListener('click', () => {
        localStorage.removeItem('auth_token');
        localStorage.removeItem('user');
        window.location.href = '/index.html';
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

      /* Per-category header within the Reports section */
      .snav-group-label {
        font-size: 0.72rem;
        font-weight: 700;
        color: #C41E3A;
        letter-spacing: 0.3px;
        padding: 0.5rem 0.5rem 0.25rem;
        margin-top: 0.5rem;
        border-top: 1px solid #f0e6d8;
      }
      .snav-group-label:first-of-type {
        margin-top: 0;
        border-top: none;
        padding-top: 0.25rem;
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
