/**
 * Poll submission judge status after the detail page has rendered.
 */
(function () {
    const root = document.getElementById('submission-detail-root');
    if (!root || root.dataset.poll !== 'true') {
        return;
    }

    const statusUrl = root.dataset.statusUrl;
    const statusEl = document.getElementById('submission-status-text');
    const summaryEl = document.getElementById('submission-status-summary');
    const testContainer = document.getElementById('submission-test-results');
    const progressEl = document.getElementById('submission-judge-progress');

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

        const html = results.map((r) => {
            const badge = CASE_BADGE[r.status] || {
                cls: 'case-Skipped',
                label: `#${r.case_index}`,
            };
            const runtimeHtml = r.runtime
                ? `<span class="case-runtime">${r.runtime} ms</span>`
                : '';
            return `
                <div class="case-point">
                    <span class="badge case-badge ${badge.cls}" title="${r.status}">${badge.label}</span>
                    <span class="case-label">#${r.case_index}</span>
                    ${runtimeHtml}
                </div>`;
        }).join('');

        testContainer.querySelector('.case-points').innerHTML = html;
    }

    function updateSummary(data) {
        if (!summaryEl) {
            return;
        }
        let html = '';
        if (data.test_results.length || data.total_cases) {
            html += `<p class="mb-1">测试点: <strong>${data.passed_count}/${data.total_cases}</strong> 通过</p>`;
        }
        if (data.runtime != null) {
            html += `<p class="mb-0">最大运行时间: ${data.runtime} ms</p>`;
        }
        if (data.memory != null) {
            html += `<p class="mb-0">内存使用: ${data.memory} KB</p>`;
        }
        summaryEl.innerHTML = html;
    }

    function applyPayload(data) {
        if (statusEl) {
            statusEl.innerHTML = data.done ? data.status : (
                '<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>'
                + data.status
            );
            applyStatusClass(statusEl, data.status);
        }
        updateSummary(data);
        renderTestResults(data.test_results);
    }

    let timer = null;

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
            if (data.done && timer) {
                clearInterval(timer);
                timer = null;
                progressEl?.remove();
            }
        } catch {
            /* ignore transient network errors */
        }
    }

    poll();
    timer = setInterval(poll, 800);
})();
