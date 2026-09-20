/**
 * Live judge status on the submission detail page.
 *
 * Primary channel: WebSocket (/ws/submissions/<id>/status/). The server
 * pushes a snapshot immediately on connect and again whenever the judge
 * worker writes a test point / the final verdict.
 *
 * Safety net: if the socket cannot be established (some CDNs/proxies block
 * WebSocket), is rejected (4401/4403), or drops repeatedly, the page
 * transparently falls back to the old 800ms HTTP long-poll endpoint. Both
 * channels carry identical JSON payloads.
 */
(function () {
    const root = document.getElementById('submission-detail-root');
    if (!root || root.dataset.poll !== 'true') {
        return;
    }

    const statusUrl = root.dataset.statusUrl;
    const wsPath = root.dataset.wsUrl;
    const statusEl = document.getElementById('submission-status-text');
    const summaryEl = document.getElementById('submission-status-summary');
    const testContainer = document.getElementById('submission-test-results');
    const progressEl = document.getElementById('submission-judge-progress');

    const POLL_INTERVAL_MS = 800;
    const HEARTBEAT_MS = 25000;
    const MAX_WS_RECONNECT = 3;

    const STATUS_CLASS = {
        Accepted: 'status-Accepted',
        'Wrong Answer': 'status-Wrong',
        'Time Limit Exceeded': 'status-Time',
        'Memory Limit Exceeded': 'status-Runtime',
        'Runtime Error': 'status-Runtime',
        Pending: 'status-Pending',
    };

    const CASE_BADGE = {
        Accepted: { cls: 'case-Accepted', label: 'AC' },
        'Wrong Answer': { cls: 'case-Wrong', label: 'WA' },
        'Time Limit Exceeded': { cls: 'case-Time', label: 'TLE' },
        'Memory Limit Exceeded': { cls: 'case-Memory', label: 'MLE' },
        'Runtime Error': { cls: 'case-Runtime', label: 'RE' },
    };

    function statusCssClass(status) {
        if (STATUS_CLASS[status]) {
            return STATUS_CLASS[status];
        }
        return 'status-Error';
    }

    function applyStatusClass(el, status) {
        el.className = statusCssClass(status);
    }

    function renderTestResults(results) {
        if (!testContainer || !results.length) {
            return;
        }
        progressEl?.classList.add('d-none');
        testContainer.classList.remove('d-none');

        // Build nodes instead of interpolating server data into innerHTML,
        // so a malicious payload (status/runtime/case_index) can never be
        // interpreted as HTML (js/xss).
        const pointsEl = testContainer.querySelector('.case-points');
        pointsEl.replaceChildren();

        results.forEach((r) => {
            const badge = CASE_BADGE[r.status] || {
                cls: 'case-Skipped',
                label: `#${r.case_index}`,
            };
            const row = document.createElement('div');
            row.className = 'case-point';

            const badgeEl = document.createElement('span');
            badgeEl.className = `badge case-badge ${badge.cls}`;
            badgeEl.title = r.status;
            badgeEl.textContent = badge.label;

            const labelEl = document.createElement('span');
            labelEl.className = 'case-label';
            labelEl.textContent = `#${r.case_index}`;

            row.appendChild(badgeEl);
            row.appendChild(labelEl);

            if (r.runtime && r.runtime !== 'None') {
                const runtimeEl = document.createElement('span');
                runtimeEl.className = 'case-runtime';
                runtimeEl.textContent = `${r.runtime} ms`;
                row.appendChild(runtimeEl);
            }
            pointsEl.appendChild(row);
        });
    }

    function updateSummary(data) {
        if (!summaryEl) {
            return;
        }
        // All dynamic values go through text nodes; no innerHTML with data.
        summaryEl.replaceChildren();

        if (data.test_results.length || data.total_cases) {
            const p = document.createElement('p');
            p.className = 'mb-1';
            p.appendChild(document.createTextNode('测试点: '));
            const strong = document.createElement('strong');
            strong.textContent = `${data.passed_count}/${data.total_cases}`;
            p.appendChild(strong);
            p.appendChild(document.createTextNode(' 通过'));
            summaryEl.appendChild(p);
        }
        if (data.runtime != null && data.runtime !== 'None') {
            const p = document.createElement('p');
            p.className = 'mb-0';
            p.textContent = `最大运行时间: ${data.runtime} ms`;
            summaryEl.appendChild(p);
        }
        if (data.memory != null) {
            const p = document.createElement('p');
            p.className = 'mb-0';
            p.textContent = `内存使用: ${data.memory} KB`;
            summaryEl.appendChild(p);
        }
    }

    let finished = false;

    function applyPayload(data) {
        if (statusEl) {
            // textContent / createTextNode keep data.status as plain text.
            statusEl.replaceChildren();
            if (data.done) {
                statusEl.textContent = data.status;
            } else {
                const spinner = document.createElement('span');
                spinner.className = 'spinner-border spinner-border-sm me-2';
                spinner.setAttribute('role', 'status');
                spinner.setAttribute('aria-hidden', 'true');
                statusEl.appendChild(spinner);
                statusEl.appendChild(document.createTextNode(data.status));
            }
            applyStatusClass(statusEl, data.status);
        }
        updateSummary(data);
        renderTestResults(data.test_results);

        if (data.done) {
            finished = true;
            if (progressEl) {
                progressEl.remove();
            }
        }
    }

    // ── HTTP polling fallback ────────────────────────────────────────────
    let pollTimer = null;

    function startPollingFallback() {
        stopWs();
        if (pollTimer !== null || finished) {
            return;
        }
        poll();
        pollTimer = setInterval(poll, POLL_INTERVAL_MS);
    }

    function stopPolling() {
        if (pollTimer !== null) {
            clearInterval(pollTimer);
            pollTimer = null;
        }
    }

    async function poll() {
        try {
            const response = await fetch(statusUrl, {
                headers: { Accept: 'application/json' },
                credentials: 'same-origin',
            });
            if (!response.ok) {
                return;
            }
            const data = await response.json();
            applyPayload(data);
            if (data.done) {
                stopPolling();
            }
        } catch {
            /* ignore transient network errors; keep polling */
        }
    }

    // ── WebSocket ────────────────────────────────────────────────────────
    let ws = null;
    let heartbeatTimer = null;
    let reconnectAttempts = 0;
    let reconnectTimer = null;
    let fallbackArmed = false;

    function buildWsUrl() {
        const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        return `${scheme}//${window.location.host}${wsPath}`;
    }

    function clearHeartbeat() {
        if (heartbeatTimer !== null) {
            clearInterval(heartbeatTimer);
            heartbeatTimer = null;
        }
    }

    function clearReconnect() {
        if (reconnectTimer !== null) {
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
        }
    }

    function stopWs() {
        clearHeartbeat();
        clearReconnect();
        if (ws) {
            // Avoid triggering onclose reconnect logic during teardown.
            ws.onclose = null;
            ws.onerror = null;
            ws.onmessage = null;
            try {
                ws.close();
            } catch {
                /* noop */
            }
            ws = null;
        }
    }

    function connectWs() {
        if (finished || fallbackArmed) {
            return;
        }
        if (!wsPath || !('WebSocket' in window)) {
            startPollingFallback();
            return;
        }

        let socket;
        try {
            socket = new WebSocket(buildWsUrl());
        } catch {
            startPollingFallback();
            return;
        }
        ws = socket;

        socket.onopen = function () {
            reconnectAttempts = 0;
            clearHeartbeat();
            heartbeatTimer = setInterval(function () {
                if (socket.readyState === WebSocket.OPEN) {
                    socket.send(JSON.stringify({ type: 'ping' }));
                }
            }, HEARTBEAT_MS);
        };

        socket.onmessage = function (event) {
            let data;
            try {
                data = JSON.parse(event.data);
            } catch {
                return;
            }
            if (data.type === 'pong' || data.type === 'ping') {
                return;
            }
            applyPayload(data);
            if (data.done) {
                stopWs();
            }
        };

        socket.onerror = function () {
            // onclose always follows; handle the decision there.
        };

        socket.onclose = function (event) {
            clearHeartbeat();
            if (finished) {
                return;
            }
            // Auth/policy rejects never recover by retrying.
            if (event.code === 4401 || event.code === 4403 || event.code === 4404) {
                fallbackArmed = true;
                startPollingFallback();
                return;
            }
            if (reconnectAttempts >= MAX_WS_RECONNECT) {
                fallbackArmed = true;
                startPollingFallback();
                return;
            }
            reconnectAttempts += 1;
            const delayMs = 1000 * reconnectAttempts;
            clearReconnect();
            reconnectTimer = setTimeout(connectWs, delayMs);
        };
    }

    connectWs();
})();
