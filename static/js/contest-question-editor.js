// Extracted from templates/contests/question.html inline <script> (2026-09-13).
// The contest problem id is passed via data-problem-id on #submit-form.
document.addEventListener('DOMContentLoaded', function () {
    var form = document.getElementById('submit-form');
    if (!form) return;
    var statusEl = document.getElementById('code-editor-status');
    function setStatus(text, cls) {
        if (!statusEl) return;
        statusEl.textContent = text;
        statusEl.className = 'badge ' + (cls || 'text-muted');
    }
    initOJCodeEditor({
        containerId: 'monaco-editor',
        textareaId: 'code',
        languageSelectId: 'language',
        problemId: Number(form.getAttribute('data-problem-id')),
        initialLanguage: document.getElementById('language').value,
        onProgress: setStatus,
    }).then(function () {
        setStatus('已就绪', 'text-success');
    }).catch(function () {
        var ta = document.getElementById('code');
        ta.classList.remove('d-none');
        ta.classList.add('form-control', 'code-editor');
        ta.rows = 20;
        document.getElementById('monaco-editor').style.display = 'none';
        setStatus('编辑器加载失败', 'text-danger');
    });
});
