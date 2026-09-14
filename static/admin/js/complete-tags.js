// Extracted from templates/admin/problems/complete_tags.html inline <script> (2026-09-13).
// The start/step endpoints are passed via data attributes on #tag-complete-form:
//   data-start-url - POST to begin a batch
//   data-step-url  - POST for each step
(function () {
    const form = document.getElementById('tag-complete-form');
    const startBtn = document.getElementById('start-btn');
    const stopBtn = document.getElementById('stop-btn');
    const progress = document.getElementById('progress');
    const log = document.getElementById('log');
    const csrf = form.querySelector('[name=csrfmiddlewaretoken]').value;
    const defaultSystem = JSON.parse(document.getElementById('default-system-prompt').textContent);
    const defaultUser = JSON.parse(document.getElementById('default-user-prompt').textContent);
    const startUrl = form.getAttribute('data-start-url');
    const stepUrl = form.getAttribute('data-step-url');
    let stopping = false;

    document.getElementById('reset-system').addEventListener('click', function () {
        document.getElementById('system_prompt').value = defaultSystem;
    });
    document.getElementById('reset-user').addEventListener('click', function () {
        document.getElementById('user_prompt').value = defaultUser;
    });

    function addLog(text, ok) {
        const li = document.createElement('li');
        li.textContent = text;
        if (ok === true) li.style.color = '#0a0';
        if (ok === false) li.style.color = '#c00';
        log.appendChild(li);
    }

    async function post(url, body) {
        const res = await fetch(url, {
            method: 'POST',
            headers: {
                'X-CSRFToken': csrf,
                'X-Requested-With': 'XMLHttpRequest',
            },
            body: body,
            credentials: 'same-origin',
        });
        let data = {};
        try { data = await res.json(); } catch (e) { data = {}; }
        if (!res.ok && !data.error) {
            data.error = '请求失败（HTTP ' + res.status + '）';
        }
        return data;
    }

    form.addEventListener('submit', async function (ev) {
        ev.preventDefault();
        stopping = false;
        log.innerHTML = '';
        startBtn.disabled = true;
        stopBtn.disabled = false;
        stopBtn.style.display = 'inline-block';
        progress.textContent = '正在挑选题目…';

        const body = new FormData();
        body.append('csrfmiddlewaretoken', csrf);
        body.append('api_key', document.getElementById('api_key').value);
        body.append('count', document.getElementById('count').value);
        body.append('system_prompt', document.getElementById('system_prompt').value);
        body.append('user_prompt', document.getElementById('user_prompt').value);

        const started = await post(startUrl, body);
        if (!started.ok) {
            progress.textContent = '';
            addLog(started.error || '启动失败', false);
            startBtn.disabled = false;
            stopBtn.style.display = 'none';
            return;
        }

        const total = started.total || 0;
        let finished = 0;
        let okCount = 0;
        progress.textContent = '将完善 ' + total + ' 道题（中文词表 ' + started.vocab_size + ' 个）';

        while (!stopping) {
            const stepBody = new FormData();
            stepBody.append('csrfmiddlewaretoken', csrf);
            const step = await post(stepUrl, stepBody);
            if (step.done && !step.problem_id && step.remaining === 0 && finished >= total) {
                break;
            }
            if (step.problem_id) {
                finished += 1;
                const label = 'P' + step.problem_id + ' ' + (step.title || '');
                if (step.ok) {
                    okCount += 1;
                    addLog(label + ' → ' + (step.added || []).join(', ') + '　' + (step.after || ''), true);
                } else {
                    addLog(label + '：' + (step.error || '失败'), false);
            if (step.error && (String(step.error).indexOf('API') !== -1
                    || String(step.error).indexOf('提示词') !== -1
                    || String(step.error).indexOf('Key') !== -1)) {
                        addLog('已停止（请检查 API Key / 提示词 / 余额）。', false);
                        break;
                    }
                }
                progress.textContent = '进度 ' + finished + ' / ' + total + '，成功 ' + okCount;
            }
            if (step.done) break;
            if (step.error && !step.problem_id) {
                addLog(step.error, false);
                break;
            }
        }

        if (stopping) {
            progress.textContent = '已停止。成功 ' + okCount + ' / ' + finished;
        } else {
            progress.textContent = '完成。成功 ' + okCount + ' / ' + (finished || total);
        }
        startBtn.disabled = false;
        stopBtn.style.display = 'none';
    });

    stopBtn.addEventListener('click', function () {
        stopping = true;
        stopBtn.disabled = true;
        progress.textContent = '即将在当前这题结束后停止…';
    });
})();
