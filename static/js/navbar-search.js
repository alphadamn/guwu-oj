// Extracted from templates/base.html inline <script> (2026-09-13).
(function () {
    var form = document.querySelector('form.navbar-search-form');
    if (!form) return;
    var input = form.querySelector('input[name="q"]');
    function start() {
        if (input && input.value && input.value.trim()) {
            form.classList.add('is-searching');
        }
    }
    function stop() { form.classList.remove('is-searching'); }
    form.addEventListener('submit', start);
    window.addEventListener('pageshow', stop);
    // Stop the spinner after 20s as a safety net.
    setTimeout(function () { if (form.classList.contains('is-searching')) stop(); }, 20000);
})();
