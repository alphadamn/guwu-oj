// Extracted from templates/users/two_factor_setup.html inline <script> (2026-09-13).
// The otpauth:// URL is passed via data-otpauth-url on #qr-target.
(function () {
    var target = document.getElementById('qr-target');
    if (!target) return;
    var url = target.getAttribute('data-otpauth-url') || '';
    if (!url || typeof QRCode === 'undefined') return;
    new QRCode(target, {
        text: url,
        width: 220,
        height: 220,
        correctLevel: QRCode.CorrectLevel.M,
    });

    var btn = document.getElementById('copy-secret-btn');
    if (btn) {
        btn.addEventListener('click', function () {
            var input = document.getElementById('secret-text');
            if (!input) return;
            try {
                input.select();
                input.setSelectionRange(0, 99999);
                document.execCommand('copy');
                btn.textContent = '已复制';
                setTimeout(function () { btn.textContent = '复制'; }, 1500);
            } catch (e) {}
        });
    }
})();
