// Extracted from templates/base.html inline <script> (2026-09-13).
// Lazy-load GitHub buttons (and its api.github.com XHR) until after
// first paint to keep the main thread free for LCP/FCP. The buttons
// live in the footer, so deferring them is invisible to the user.
(function () {
    function loadButtons() {
        var s = document.createElement('script');
        s.src = '/static/js/buttons.js';
        s.async = true;
        document.body.appendChild(s);
    }
    if (document.readyState === 'complete') {
        (window.requestIdleCallback || function (cb) { return setTimeout(cb, 200); })(loadButtons);
    } else {
        window.addEventListener('load', function () {
            (window.requestIdleCallback || function (cb) { return setTimeout(cb, 200); })(loadButtons);
        });
    }
})();
