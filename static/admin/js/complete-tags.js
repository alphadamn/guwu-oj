// Extracted from templates/admin/problems/complete_tags.html inline <script> (2026-09-14).
// The start/step endpoints are passed via data attributes on #tag-complete-form:
//   data-start-url  - POST to begin a batch
//   data-step-url   - POST for each step (now returns a batch of results)
//   data-step-batch - how many problems each step processes (default 10)
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
    const stepBatch = parseInt(form.getAttribute('data-step-batch') || '10', 10);
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

    // 判断错误是否属于「停下来别继续」的类型
    function isFatalError(msg) {
        const s = String(msg || '');
        return s.indexOf('API') !== -1
            || s.indexOf('提示词') !== -1
            || s.indexOf('Key') !== -1;
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
        progress.textContent =
            '将完善 ' + total + ' 道题（中文词表 ' + started.vocab_size + ' 个，每批并发 '
            + stepBatch + ' 道）';

        let fatalStop = false;

        while (!stopping) {
            const stepBody = new FormData();
            stepBody.append('csrfmiddlewaretoken', csrf);
            const step = await post(stepUrl, stepBody);

            // 顶层错误（非单题）：会话过期、参数非法等，直接停
            if (step.error && !step.results) {
                addLog(step.error, false);
                if (isFatalError(step.error)) {
                    addLog('已停止（请检查 API Key / 提示词 / 余额）。', false);
                }
                break;
            }

            // 新版 step 返回 results 数组；旧版返回单条，做一次兼容
            const items = Array.isArray(step.results) ? step.results : [];
            if (!items.length && step.problem_id) {
                items.push(step);
            }

            for (const r of items) {
                if (!r.problem_id) continue;
                finished += 1;
                const label = 'P' + r.problem_id + ' ' + (r.title || '');
                if (r.ok) {
                    okCount += 1;
                    const added = (r.added || []).join(', ');
                    addLog(label + ' → ' + added + '　' + (r.after || ''), true);
                } else {
                    addLog(label + '：' + (r.error || '失败'), false);
                    if (isFatalError(r.error)) {
                        fatalStop = true;
                    }
                }
            }

            const remaining = (typeof step.remaining === 'number')
                ? step.remaining
                : Math.max(0, total - finished);

            progress.textContent =
                '进度 ' + finished + ' / ' + total + '，成功 ' + okCount
                + '，剩余 ' + remaining;

            if (fatalStop) {
                addLog('已停止（请检查 API Key / 提示词 / 余额）。', false);
                break;
            }

            if (step.done) break;

            // 防御：如果一步既没有结果也没推进 remaining，避免死循环
            if (!items.length && remaining <= 0) break;
            if (!items.length && remaining === (total - finished)) {
                // 没有任何进展，再试一次；若连续两次无进展可在此加计数器
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
        progress.textContent = '即将在当前批次（≤' + stepBatch + ' 道）结束后停止…';
    });
})();