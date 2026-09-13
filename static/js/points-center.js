// Extracted from templates/users/points_center.html inline <script> (2026-09-13).
(function () {
    var input = document.getElementById('referral-link');
    var button = document.getElementById('copy-referral-link');
    if (!input || !button) return;
    function fallbackCopy() {
        input.focus();
        input.select();
        var copied = document.execCommand('copy');
        if (copied) {
            button.innerHTML = '<i class="bi bi-check2 me-1"></i>已复制';
            setTimeout(function () { button.innerHTML = '<i class="bi bi-copy me-1"></i>复制'; }, 1800);
        }
    }

    button.addEventListener('click', function () {
        if (!navigator.clipboard || !navigator.clipboard.writeText) {
            fallbackCopy();
            return;
        }
        navigator.clipboard.writeText(input.value).then(function () {
            button.innerHTML = '<i class="bi bi-check2 me-1"></i>已复制';
            setTimeout(function () { button.innerHTML = '<i class="bi bi-copy me-1"></i>复制'; }, 1800);
        }).catch(fallbackCopy);
    });
})();
