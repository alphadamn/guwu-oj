// Extracted from templates/users/register.html inline <script> (2026-09-13).
// The captcha refresh/preload part now lives in captcha-widget.js
// (initialized via data-captcha-url on #captcha-img); only the
// "send verification code" logic remains here.
(function () {
    var btn = document.getElementById('send-code-btn');
    var hint = document.getElementById('send-code-hint');
    var emailInput = document.getElementById('id_email');
    if (!btn || !emailInput) return;

    var originalLabel = btn.innerText;
    var timerId = null;
    var remaining = 0;

    function setCooldown(seconds) {
        remaining = seconds;
        btn.disabled = true;
        btn.classList.remove('btn-outline-primary');
        btn.classList.add('btn-outline-secondary');
        btn.innerText = remaining + 's 后重发';
        clearInterval(timerId);
        timerId = setInterval(function () {
            remaining -= 1;
            if (remaining <= 0) {
                clearInterval(timerId);
                btn.disabled = false;
                btn.classList.remove('btn-outline-secondary');
                btn.classList.add('btn-outline-primary');
                btn.innerText = originalLabel;
            } else {
                btn.innerText = remaining + 's 后重发';
            }
        }, 1000);
    }

    btn.addEventListener('click', function () {
        var email = emailInput.value.trim();
        if (!email) {
            hint.innerText = '请先填写邮箱后再获取验证码。';
            hint.classList.remove('text-muted');
            hint.classList.add('text-danger');
            emailInput.focus();
            return;
        }
        btn.disabled = true;
        btn.innerText = '发送中…';
        var body = new FormData();
        body.append('email', email);
        var csrfToken = document.querySelector('#register-form [name=csrfmiddlewaretoken]');
        if (csrfToken) body.append('csrfmiddlewaretoken', csrfToken.value);

        fetch(btn.dataset.url, { method: 'POST', body: body, credentials: 'same-origin' })
            .then(function (resp) { return resp.json().then(function (data) { return { ok: resp.ok, data: data }; }); })
            .then(function (result) {
                var data = result.data || {};
                hint.innerText = data.message || (result.ok ? '已发送，请查收。' : '发送失败。');
                hint.classList.remove('text-danger', 'text-success');
                hint.classList.add(result.ok ? 'text-success' : 'text-danger');
                if (result.ok) setCooldown(60);
                else { btn.disabled = false; btn.innerText = originalLabel; }
            })
            .catch(function () {
                hint.innerText = '网络错误，请稍后再试。';
                hint.classList.remove('text-muted', 'text-success');
                hint.classList.add('text-danger');
                btn.disabled = false;
                btn.innerText = originalLabel;
            });
    });
})();
