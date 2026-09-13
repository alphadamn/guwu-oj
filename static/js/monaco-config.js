// Extracted from templates/base.html inline <script> (2026-09-13).
// Expose the admin-configured Monaco CDN URL to frontend JS.
// The server value is rendered into <meta name="oj-monaco-base"> in base.html;
// falls back to jsdelivr if the meta is missing or empty.
(function () {
    var meta = document.querySelector('meta[name="oj-monaco-base"]');
    var fromServer = meta ? (meta.getAttribute('content') || '').trim() : '';
    window.OJ_MONACO_BASE = fromServer && fromServer.length > 10
        ? fromServer
        : 'https://cdn.jsdelivr.net/npm/monaco-editor@0.52.2/min/vs';
})();
