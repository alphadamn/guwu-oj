// Extracted from search/templates/search/search.html inline <script> (2026-09-13).
// Dynamic URLs are passed via data attributes on #search-source-form:
//   data-api-url  - search results API endpoint
//   data-page-url - the search page URL (for history.replaceState)
(function () {
    function init() {
        var overlay = document.getElementById('search-loading-overlay');
        var resultsEl = document.getElementById('search-results');
        var errorEl = document.getElementById('search-error');
        var sourceForm = document.getElementById('search-source-form');
        if (!sourceForm) return;
        var apiUrl = sourceForm.getAttribute('data-api-url');
        var pageUrl = sourceForm.getAttribute('data-page-url');
        var queryInput = sourceForm.querySelector('input[name="q"]');

        // Prefer the URL query string – this is the source of truth after a
        // menubar submission. Fall back to the hidden form field for safety.
        var urlQuery = (new URLSearchParams(window.location.search)).get('q') || '';
        var query = (queryInput && queryInput.value && queryInput.value.trim())
            ? queryInput.value
            : urlQuery;
        if (queryInput) queryInput.value = query;

        function showLoading() {
            if (overlay) overlay.classList.add('is-visible');
            if (errorEl) errorEl.style.display = 'none';
        }
        function hideLoading() {
            if (overlay) overlay.classList.remove('is-visible');
        }
        function showError(message) {
            if (!errorEl) return;
            errorEl.textContent = message || '搜索失败，请稍后再试。';
            errorEl.style.display = 'block';
        }

        function activeSources() {
            if (!sourceForm) return [];
            return Array.from(
                sourceForm.querySelectorAll('input[name="src"]:checked')
            ).map(function (el) { return el.value; });
        }

        function buildUrl(q, sources) {
            var params = new URLSearchParams();
            if (q) params.set('q', q);
            sources.forEach(function (s) { params.append('src', s); });
            return apiUrl + '?' + params.toString();
        }

        function fetchResults() {
            if (!query) {
                if (resultsEl) resultsEl.innerHTML = '';
                hideLoading();
                return;
            }
            showLoading();
            fetch(buildUrl(query, activeSources()), {
                headers: { 'X-Requested-With': 'XMLHttpRequest' },
                cache: 'no-store',
            })
                .then(function (resp) {
                    if (!resp.ok) throw new Error('HTTP ' + resp.status);
                    return resp.json();
                })
                .then(function (data) {
                    if (resultsEl) resultsEl.innerHTML = data.html || '';
                    if (!data.html) showError('没有收到搜索结果，请稍后再试。');
                })
                .catch(function (err) {
                    showError(err.message || '搜索失败，请稍后再试。');
                })
                .finally(hideLoading);
        }

        // Load results via AJAX on first render so the loading overlay shows on /search.
        fetchResults();

        // Re-fetch when the user toggles sources, without a full navigation.
        sourceForm.addEventListener('submit', function (e) {
            e.preventDefault();
            fetchResults();
            var params = new URLSearchParams();
            if (query) params.set('q', query);
            activeSources().forEach(function (s) { params.append('src', s); });
            if (pageUrl) history.replaceState(null, '', pageUrl + '?' + params.toString());
        });

        // Only hide the overlay on BFCache restore – NOT on initial page load,
        // which would prematurely hide it while the AJAX fetch is still pending.
        window.addEventListener('pageshow', function (e) {
            if (e.persisted) hideLoading();
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
