// Shared graphical-captcha widget (extracted from the per-page inline
// scripts of users/login.html, users/register.html,
// users/password_reset_request.html, users/password_reset_confirm.html and
// admin/login.html, 2026-09-13).
//
// Usage: render the captcha <img id="captcha-img" data-captcha-url="...">
// plus the hidden <input id="id_captcha_id"> in the template, then include
// this file. It auto-initializes on DOMContentLoaded, refreshes on image
// click / .captcha-refresh buttons, and preloads the first challenge.
(function () {
    function init() {
        var img = document.getElementById('captcha-img');
        var idEl = document.getElementById('id_captcha_id');
        if (!img || !idEl) return;
        var url = img.getAttribute('data-captcha-url');
        if (!url) return;
        if (img.dataset.ojCaptchaBound) return;
        img.dataset.ojCaptchaBound = '1';

        function refresh() {
            img.alt = '正在加载图形验证码';
            fetch(url, { method: 'GET', credentials: 'same-origin', cache: 'no-store' })
                .then(function (resp) {
                    var newId = (resp.headers.get('X-Captcha-Id') || '').trim();
                    if (!newId) throw new Error('captcha unavailable');
                    idEl.value = newId;
                    return resp.blob();
                })
                .then(function (blob) {
                    img.src = URL.createObjectURL(blob);
                    img.alt = '图形验证码';
                })
                .catch(function () {
                    idEl.value = '';
                    img.removeAttribute('src');
                    img.alt = '图形验证码加载失败，请点击刷新后重试';
                });
        }

        img.addEventListener('click', refresh);
        document.querySelectorAll('.captcha-refresh').forEach(function (el) {
            el.addEventListener('click', refresh);
        });
        refresh();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
