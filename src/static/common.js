/*
 * Shared chrome and helpers for every page.
 *
 * The Tailwind palette override, the background, the navigation and the
 * command palette live here so a page only has to load this script. It must be
 * loaded *after* the Tailwind Play CDN script (which reads the config when it
 * scans) and *before* the page's own scripts.
 */

// Remap Tailwind's slate palette (default: cool blue-gray) onto a neutral
// zinc-like scale so every slate-* utility -- dropdowns, modals, table
// headers, badges -- matches the app's near-black surfaces instead of
// clashing with a blue-tinted navy. Blue is likewise pulled to the accent.
window.tailwind = window.tailwind || {};
window.tailwind.config = {
    theme: {
        extend: {
            colors: {
                slate: {
                    50: '#fafafa',
                    100: '#f4f4f5',
                    200: '#e4e4e7',
                    300: '#d4d4d8',
                    400: '#a1a1aa',
                    500: '#71717a',
                    600: '#52525b',
                    700: '#3f3f46',
                    800: '#27272a',
                    900: '#18181b',
                    950: '#0c0c0e'
                },
                blue: {
                    400: '#5eb0ff',
                    500: '#3d9cff',
                    600: '#0a84ff',
                    700: '#0071e3',
                    800: '#0058b0'
                }
            }
        }
    }
};

const App = (() => {
    /* Pages grouped by the job they do. The top bar shows the sections; the
       current section's pages sit in a second row, so every page is still one
       click away from its neighbours. */
    const NAV_GROUPS = [
        {
            label: 'Overview',
            items: [
                { href: '/', label: 'Dashboard', hint: 'Income, spending and what needs attention' },
                { href: '/insights', label: 'Insights', hint: 'Trends, recurring payments, uncategorized spend' },
                { href: '/budgets', label: 'Budgets', hint: 'Monthly limits per category or tag' }
            ]
        },
        {
            label: 'Transactions',
            items: [
                { href: '/data', label: 'Data Grid', hint: 'Browse, tag, remark and review transactions' },
                { href: '/self-transfers', label: 'Self Transfers', hint: 'Match money moved between your accounts' }
            ]
        },
        {
            label: 'Organize',
            items: [
                { href: '/rules', label: 'Rules', hint: 'Regex rules that categorize transactions' },
                { href: '/tags', label: 'Tags', hint: 'Group tagging and the tag library' },
                { href: '/category-tags', label: 'Category Tags', hint: 'Default tags per category' }
            ]
        },
        {
            label: 'Verify',
            items: [
                { href: '/reconcile', label: 'Reconcile', hint: 'Statement coverage, overlaps and gaps' },
                { href: '/audit', label: 'Audit', hint: 'Balance continuity, duplicates, review progress' }
            ]
        }
    ];

    // Flat list, kept for anything that still wants every page.
    const NAV_LINKS = NAV_GROUPS.flatMap(g => g.items);

    const currency = new Intl.NumberFormat('en-IN', {
        style: 'currency',
        currency: 'INR',
        maximumFractionDigits: 0
    });

    const decimal = new Intl.NumberFormat('en-IN', {
        style: 'decimal',
        minimumFractionDigits: 2
    });

    /* --- Formatting ------------------------------------------------- */

    const money = v => currency.format(v || 0);

    /* Axis-friendly rupees: 1.2L, 3.4Cr, 45k. */
    function compactMoney(v) {
        const a = Math.abs(v);
        if (a >= 1e7) return `₹${(v / 1e7).toFixed(1)}Cr`;
        if (a >= 1e5) return `₹${(v / 1e5).toFixed(1)}L`;
        if (a >= 1e3) return `₹${Math.round(v / 1e3)}k`;
        return `₹${v}`;
    }

    /* "2024-07-05" -> "5 Jul 2024" */
    function fmtDate(iso) {
        if (!iso) return '–';
        return new Date(iso + 'T00:00:00').toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
    }

    /* "2024-07" -> "Jul 24" */
    function monthLabel(month) {
        const [y, m] = month.split('-');
        return new Date(+y, +m - 1, 1).toLocaleString('en-IN', { month: 'short', year: '2-digit' });
    }

    /* Text -> safe HTML, for both element content and attribute values.
       Quotes are escaped too: the old textContent/innerHTML trick left them
       alone, so an escaped value inside value="..." could still break out. */
    const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

    function escapeHtml(text) {
        return text == null ? '' : String(text).replace(/[&<>"']/g, ch => HTML_ESCAPES[ch]);
    }

    /* A value as a JS literal inside an inline handler attribute, e.g.
       onclick="removeTag(${App.jsArg(tag)})". JSON makes it a valid JS string
       whatever it contains; escaping makes it safe inside the attribute. */
    function jsArg(value) {
        return escapeHtml(JSON.stringify(value == null ? '' : value));
    }

    /* Fetch JSON, turning a non-2xx into a rejection so callers can catch it
       rather than silently rendering an error body as data. */
    async function api(url, options) {
        const response = await fetch(url, options);
        if (!response.ok) {
            const detail = await response.text().catch(() => '');
            throw new Error(detail || `${response.status} ${response.statusText}`);
        }
        return response.status === 204 ? null : response.json();
    }

    function postJson(url, body) {
        return api(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
    }

    function isActive(href) {
        const path = window.location.pathname;
        return href === '/' ? path === '/' : path.startsWith(href);
    }

    function currentGroup() {
        return NAV_GROUPS.find(g => g.items.some(i => isActive(i.href))) || null;
    }

    const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
    const shortcutLabel = isMac ? '⌘K' : 'Ctrl K';

    const ICON_SEARCH = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>';
    const ICON_UPLOAD = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 16V4"/><path d="m6 10 6-6 6 6"/><path d="M4 20h16"/></svg>';
    const ICON_MARK = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 10h18"/><path d="M5 10v8"/><path d="M10 10v8"/><path d="M14 10v8"/><path d="M19 10v8"/><path d="M3 20h18"/><path d="m12 3 9 5H3z"/></svg>';

    function chrome() {
        const group = currentGroup();
        const sections = NAV_GROUPS.map(g => {
            const active = g === group;
            return `<a href="${g.items[0].href}" class="nav-pill${active ? ' active' : ''}"${active ? ' aria-current="true"' : ''}>${g.label}</a>`;
        }).join('');

        const subnav = group ? `
            <div class="subnav">
                <div class="subnav-links" role="navigation" aria-label="${group.label}">
                    ${group.items.map(i => `<a href="${i.href}" class="subnav-link${isActive(i.href) ? ' active' : ''}"${isActive(i.href) ? ' aria-current="page"' : ''}>${i.label}</a>`).join('')}
                </div>
            </div>` : '';

        return `
    <div class="bg-ambient" aria-hidden="true"></div>

    <nav class="topbar">
        <div class="topbar-inner">
            <a href="/" class="topbar-brand"><span class="brand-mark">${ICON_MARK}</span> Statements</a>
            <div class="topbar-links">${sections}</div>
            <div class="topbar-actions">
                <button type="button" class="cmdk-trigger" onclick="App.openPalette()" aria-label="Search pages and actions">
                    ${ICON_SEARCH}<span class="cmdk-label">Search</span><kbd>${shortcutLabel}</kbd>
                </button>
                <a href="/upload" class="btn-primary" aria-label="Upload statements">${ICON_UPLOAD}<span class="btn-label">Upload</span></a>
            </div>
        </div>
        ${subnav}
    </nav>`;
    }

    /* A small "Section" label above each page title, so the page says where
       it sits without the user having to read the nav. */
    function addEyebrow() {
        const hero = document.querySelector('.page-hero');
        const group = currentGroup();
        if (!hero || !group || hero.querySelector('.page-eyebrow')) return;
        const title = hero.querySelector('h1');
        if (title) title.insertAdjacentHTML('beforebegin', `<div class="page-eyebrow">${group.label}</div>`);
    }

    /* --- Command palette ------------------------------------------------- */

    let palette = null;

    function paletteCommands(query) {
        const commands = [];
        NAV_GROUPS.forEach(g => g.items.forEach(i => commands.push({
            group: 'Go to', label: i.label, hint: g.label, detail: i.hint, run: () => { location.href = i.href; }
        })));
        commands.push(
            { group: 'Actions', label: 'Upload statements', hint: 'Upload', run: () => { location.href = '/upload'; } },
            { group: 'Actions', label: 'Review unreviewed transactions', hint: 'Data Grid', run: () => { location.href = '/data?review=unreviewed&view=grid'; } },
            { group: 'Actions', label: 'Find self transfers', hint: 'Self Transfers', run: () => { location.href = '/self-transfers'; } },
            { group: 'Actions', label: 'Add a budget', hint: 'Budgets', run: () => { location.href = '/budgets'; } },
            { group: 'Actions', label: 'Export enriched data as CSV', hint: 'Download', run: () => { location.href = '/data.csv'; } }
        );

        const q = query.trim().toLowerCase();
        if (!q) return commands;
        const matches = commands.filter(c =>
            `${c.label} ${c.hint} ${c.detail || ''}`.toLowerCase().includes(q));
        // Free text always offers a transaction search as the last resort.
        matches.push({
            group: 'Search', label: `Search transactions for “${query.trim()}”`, hint: 'Data Grid',
            run: () => { location.href = `/data?search=${encodeURIComponent(query.trim())}`; }
        });
        return matches;
    }

    function renderPalette() {
        const { list, input } = palette;
        palette.items = paletteCommands(input.value);
        if (palette.index >= palette.items.length) palette.index = 0;

        if (!palette.items.length) {
            list.innerHTML = '<div class="cmdk-empty">No matches</div>';
            return;
        }
        let lastGroup = null;
        list.innerHTML = palette.items.map((c, i) => {
            const header = c.group !== lastGroup ? `<div class="cmdk-group">${escapeHtml(c.group)}</div>` : '';
            lastGroup = c.group;
            return `${header}<div class="cmdk-item${i === palette.index ? ' is-active' : ''}" data-index="${i}" role="option" aria-selected="${i === palette.index}">
                <span>${escapeHtml(c.label)}</span><span class="cmdk-hint">${escapeHtml(c.hint || '')}</span></div>`;
        }).join('');
        const active = list.querySelector('.is-active');
        if (active) active.scrollIntoView({ block: 'nearest' });
    }

    function openPalette() {
        if (palette) return;
        const backdrop = document.createElement('div');
        backdrop.className = 'cmdk-backdrop';
        backdrop.innerHTML = `
            <div class="cmdk-panel" role="dialog" aria-modal="true" aria-label="Command palette">
                <input class="cmdk-input" type="text" placeholder="Go to a page, run an action, or search transactions…" aria-label="Command">
                <div class="cmdk-list" role="listbox"></div>
                <div class="cmdk-footer"><span><kbd>↑</kbd> <kbd>↓</kbd> to move</span><span><kbd>Enter</kbd> to open</span><span><kbd>Esc</kbd> to close</span></div>
            </div>`;
        document.body.appendChild(backdrop);

        palette = {
            backdrop,
            input: backdrop.querySelector('.cmdk-input'),
            list: backdrop.querySelector('.cmdk-list'),
            index: 0,
            items: [],
            previousFocus: document.activeElement
        };

        palette.input.addEventListener('input', () => { palette.index = 0; renderPalette(); });
        palette.input.addEventListener('keydown', event => {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
                event.preventDefault();
                const step = event.key === 'ArrowDown' ? 1 : -1;
                palette.index = (palette.index + step + palette.items.length) % Math.max(palette.items.length, 1);
                renderPalette();
            } else if (event.key === 'Enter') {
                event.preventDefault();
                const command = palette.items[palette.index];
                if (command) { closePalette(); command.run(); }
            } else if (event.key === 'Escape') {
                closePalette();
            }
        });
        palette.list.addEventListener('mousemove', event => {
            const item = event.target.closest('.cmdk-item');
            if (item && +item.dataset.index !== palette.index) {
                palette.index = +item.dataset.index;
                renderPalette();
            }
        });
        palette.list.addEventListener('click', event => {
            const item = event.target.closest('.cmdk-item');
            if (!item) return;
            const command = palette.items[+item.dataset.index];
            closePalette();
            command.run();
        });
        backdrop.addEventListener('mousedown', event => {
            if (event.target === backdrop) closePalette();
        });

        renderPalette();
        palette.input.focus();
    }

    function closePalette() {
        if (!palette) return;
        palette.backdrop.remove();
        if (palette.previousFocus && palette.previousFocus.focus) palette.previousFocus.focus();
        palette = null;
    }

    document.addEventListener('keydown', event => {
        if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
            event.preventDefault();
            palette ? closePalette() : openPalette();
        }
    });

    function mount() {
        if (!document.querySelector('.topbar')) {
            document.body.insertAdjacentHTML('afterbegin', chrome());
        }
        addEyebrow();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', mount);
    } else {
        mount();
    }

    return {
        NAV_GROUPS, NAV_LINKS, currency, decimal, money, compactMoney, fmtDate, monthLabel,
        escapeHtml, jsArg, api, postJson, openPalette, closePalette
    };
})();
