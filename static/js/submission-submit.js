// Extracted from templates/submissions/submit.html inline <script> (2026-09-13).
// Dynamic values are passed via data attributes on #submit-form:
//   data-problem-id - the problem being submitted to
//   data-captcha-url - URL of the graphical captcha image endpoint
document.addEventListener('DOMContentLoaded', function () {
    var form = document.getElementById('submit-form');
    var statusEl = document.getElementById('code-editor-status');
    function setStatus(text, cls) {
        if (!statusEl) return;
        statusEl.textContent = text;
        statusEl.className = 'badge ' + (cls || 'text-muted');
    }
    setStatus('正在加载代码编辑器 (' + (window.OJ_MONACO_BASE || '') + ')', 'text-muted');
    initOJCodeEditor({
        containerId: 'monaco-editor',
        textareaId: 'code',
        languageSelectId: 'language',
        problemId: form ? Number(form.getAttribute('data-problem-id')) : null,
        initialLanguage: document.getElementById('language').value,
        onProgress: setStatus,
    }).then(function () {
        setStatus('已就绪', 'text-success');
    }).catch(function (err) {
        console.error(err);
        var ta = document.getElementById('code');
        ta.classList.remove('d-none');
        ta.classList.add('form-control', 'code-editor');
        ta.style.minHeight = '400px';
        ta.rows = 20;
        document.getElementById('monaco-editor').style.display = 'none';
        alert('代码编辑器加载失败，已回退为普通文本框。请检查网络后刷新页面。');
        setStatus('编辑器加载失败', 'text-danger');
    });

    // Captcha widget: refresh on demand, and keep the hidden captcha_id
    // field in sync with the image response header. Also auto-focus the
    // answer input so the flow is faster.
    var img = document.getElementById('captcha-img');
    var idEl = document.getElementById('captcha-id');
    var answerEl = document.getElementById('captcha-answer');
    var captchaUrl = form ? form.getAttribute('data-captcha-url') : '';
    // Allow only a same-origin absolute path (starts with a single '/').
    // Explicit charAt/indexOf guards (a regex test was not modeled as a
    // sanitizer) reject protocol-relative ("//host"), backslash smuggling
    // ("\/host"), any scheme ("javascript:...") and CR/LF header/script
    // injection before the value can reach fetch() or img.src below.
    if (captchaUrl) {
        if (captchaUrl.charAt(0) !== '/'
            || captchaUrl.charAt(1) === '/'
            || captchaUrl.charAt(1) === '\\'
            || captchaUrl.indexOf(':') !== -1
            || captchaUrl.indexOf('\\') !== -1
            || captchaUrl.indexOf('\n') !== -1
            || captchaUrl.indexOf('\r') !== -1) {
            captchaUrl = '';
        }
    }
    if (img && idEl && captchaUrl) {
        function refreshCaptcha() {
            img.classList.add('refreshing');
            var src = captchaUrl + '?_=' + Date.now();
            fetch(src, { method: 'GET', credentials: 'same-origin', cache: 'no-store' })
                .then(function (resp) {
                    var newId = (resp.headers.get('X-Captcha-Id') || '').trim();
                    if (newId) idEl.value = newId;
                    return resp.blob();
                })
                .then(function (blob) {
                    var reader = new FileReader();
                    reader.onload = function () {
                        img.src = reader.result;
                        img.classList.remove('refreshing');
                    };
                    reader.readAsDataURL(blob);
                })
                .catch(function () {
                    img.src = src;
                    img.classList.remove('refreshing');
                });
        }
        img.addEventListener('click', refreshCaptcha);
        var btn = document.getElementById('captcha-refresh');
        if (btn) btn.addEventListener('click', refreshCaptcha);
        // Auto-load an initial challenge on pageload; focus the answer input.
        refreshCaptcha();
        if (answerEl) {
            setTimeout(function () { try { answerEl.focus({ preventScroll: false }); } catch (e) {} }, 250);
        }
    }
});
