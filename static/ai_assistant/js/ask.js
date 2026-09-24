/* AI 解题页（ask）交互逻辑 —— 独立静态文件，供 nginx / Cloudflare 长期缓存。
 *
 * 协议：服务端以 SSE 推送 start / reasoning / delta / done / error，
 * 思考阶段（deepseek-flash 的 reasoning_content）持续推送 reasoning，
 * 空闲时还有 ": ping" 注释心跳帧。
 */
(function () {
    'use strict';

    // 连续这么久收不到任何字节（含心跳）即判定链路僵死。服务端每 15s 发
    // 一次心跳，正常连接不可能触发；触发后主动断开并提示用户重试。
    var STALL_TIMEOUT_MS = 90000;

    var state = JSON.parse(document.getElementById('ai-page-state').textContent);
    var PROBLEM_ID = state.problem_id;
    var SESSION_ID = state.session_id;
    var GEN_COUNT = state.gen_count;
    var MAX_ROUNDS = state.max_rounds;
    var SATISFIED = state.satisfied;

    var conversation = document.getElementById('ai-conversation');
    var errorBox = document.getElementById('ai-error');
    var loading = document.getElementById('ai-loading');
    var controls = document.getElementById('ai-controls');

    var activeStream = null; // current AbortController, if any

    function csrfToken() {
        var input = document.querySelector('input[name=csrfmiddlewaretoken]');
        return input ? input.value : '';
    }
    function showError(msg) {
        errorBox.textContent = msg;
        errorBox.classList.remove('d-none');
    }
    function clearError() { errorBox.classList.add('d-none'); }
    function setLoading(on) {
        loading.classList.toggle('d-none', !on);
        controls.classList.toggle('d-none', on);
    }
    function typeset(el) {
        try {
            if (window.MathJax && window.MathJax.typesetPromise) {
                window.MathJax.typesetPromise([el]).catch(function () {});
            }
        } catch (e) {}
    }

    // --- incremental rendering -------------------------------------------
    function createStreamingCard(round) {
        var card = document.createElement('div');
        card.className = 'card border-0 shadow-sm rounded-4 mb-3 ai-message';
        card.setAttribute('data-round', round);
        var title = round === 1 ? 'AI 讲解' : '重新生成的讲解';
        card.innerHTML =
            '<div class="card-header bg-white border-0 pb-0"><h6 class="mb-0 text-primary">' +
            '<i class="bi bi-chat-square-text"></i> ' + title + '</h6></div>' +
            '<div class="card-body pt-2">' +
            '  <details class="ai-reasoning d-none" data-phase="thinking">' +
            '    <summary>' +
            '      <span class="ai-reasoning-label">正在思考</span>' +
            '      <span class="ai-reasoning-dots" aria-hidden="true"><i></i><i></i><i></i></span>' +
            '      <i class="bi bi-chevron-down ai-reasoning-chevron" aria-hidden="true"></i>' +
            '    </summary>' +
            '    <div class="ai-reasoning-body"></div>' +
            '  </details>' +
            '  <div class="ai-tool-calls"></div>' +
            '  <div class="ai-answer ai-answer-streaming"></div>' +
            '</div>';
        // 一旦用户手动展开/收起过思考面板，后续自动状态切换都不再覆盖。
        var details = card.querySelector('.ai-reasoning');
        details.addEventListener('toggle', function () { details.dataset.user = '1'; });
        conversation.appendChild(card);
        card.scrollIntoView({behavior: 'smooth', block: 'nearest'});
        return card;
    }

    // --- 思考链：rAF 批量增量渲染（DeepSeek 官网式动态渲染）----------------
    // 旧实现每个 SSE 分片都 appendChild 一个新文本节点并强制同步布局
    // （scrollTop=scrollHeight），长思考（上万字 / 几千分片）会产生上千
    // DOM 节点和近 O(n²) 次重排，导致页面卡死。现在：
    //   1. 分片先进入字符串缓冲，每帧（requestAnimationFrame）合并刷新一次，
    //      多高频分片在一帧内只会产生一次 DOM 写入；
    //   2. 全文只维护一个增长的 Text 节点（appendData），不再每个分片一个节点；
    //   3. 自动滚动每帧至多一次，且只在“吸底”状态下跟随；用户上翻阅读时
    //      不再被强制拉回底部（与 DeepSeek 一致）；
    //   4. 思考尾部有闪烁光标，进入作答/完成阶段自动消失。
    var REASONING_STICK_THRESHOLD = 48; // px：距底部小于该值视为吸底

    function raf(cb) {
        if (window.requestAnimationFrame) { return window.requestAnimationFrame(cb); }
        return setTimeout(cb, 16);
    }
    function rafCancel(handle) {
        if (window.cancelAnimationFrame) { window.cancelAnimationFrame(handle); return; }
        clearTimeout(handle);
    }

    // 首个思考片段到达：展开面板、初始化“单文本节点 + 光标”结构。
    function ensureReasoningUi(card) {
        var details = card.querySelector('.ai-reasoning');
        if (!details) { return null; }
        if (details.classList.contains('d-none')) {
            details.classList.remove('d-none');
            details.dataset.phase = 'thinking';
            card._thinkingStarted = Date.now();
            if (!details.dataset.user) { details.open = true; }
        }
        if (!card._reasoningInited) {
            card._reasoningInited = true;
            card._reasoningPinned = true;
            card._reasoningBuf = '';
            var body = details.querySelector('.ai-reasoning-body');
            // 全部思考文本追加进同一个 Text 节点；光标独立放在其后。
            var tail = document.createTextNode('');
            var cursor = document.createElement('span');
            cursor.className = 'ai-reasoning-cursor';
            cursor.setAttribute('aria-hidden', 'true');
            body.appendChild(tail);
            body.appendChild(cursor);
            card._reasoningTail = tail;
            card._reasoningCursor = cursor;
            // 用户主动上翻即解除吸底跟随，回到底部附近自动恢复。
            body.addEventListener('scroll', function () {
                card._reasoningPinned =
                    body.scrollHeight - body.scrollTop - body.clientHeight <
                    REASONING_STICK_THRESHOLD;
            }, {passive: true});
        }
        return details.querySelector('.ai-reasoning-body');
    }

    // 一帧一次：把缓冲片段合并进唯一文本节点，并按需吸底滚动。
    function flushReasoning(card) {
        card._reasoningRaf = null;
        var body = card.querySelector('.ai-reasoning-body');
        var details = card.querySelector('.ai-reasoning');
        if (!body || !details) { return; }
        var chunk = card._reasoningBuf || '';
        if (chunk) {
            card._reasoningBuf = '';
            if (card._reasoningTail) { card._reasoningTail.appendData(chunk); }
        }
        // 收起状态下子树不参与布局，写入零重排；展开且吸底时一帧至多一次滚动。
        if (details.open && card._reasoningPinned) {
            body.scrollTop = body.scrollHeight;
        }
    }

    function scheduleReasoningFlush(card) {
        if (card._reasoningRaf || card._finalised) { return; }
        card._reasoningRaf = raf(function () { flushReasoning(card); });
    }

    function cancelReasoningFlush(card) {
        if (card._reasoningRaf) {
            rafCancel(card._reasoningRaf);
            card._reasoningRaf = null;
        }
    }

    function removeReasoningCursor(card) {
        if (card._reasoningCursor && card._reasoningCursor.parentNode) {
            card._reasoningCursor.parentNode.removeChild(card._reasoningCursor);
        }
        card._reasoningCursor = null;
    }

    // 思考片段：首个片段展开面板并开始计时，之后只进缓冲，等下一帧合并上屏。
    function appendReasoning(card, text) {
        var body = ensureReasoningUi(card);
        if (!body) { return; }
        card._reasoningBuf += text;
        scheduleReasoningFlush(card);
    }

    // 首条正文片段：思考结束，DeepSeek 风格自动收起（用户手动展开过则保留）。
    function beginAnswer(card) {
        var details = card.querySelector('.ai-reasoning');
        if (!details || details.classList.contains('d-none')) { return; }
        // 先把缓冲里最后的思考片段落盘，避免有文字卡在缓冲区。
        cancelReasoningFlush(card);
        flushReasoning(card);
        removeReasoningCursor(card);
        if (details.dataset.phase === 'thinking') {
            var started = card._thinkingStarted || Date.now();
            var seconds = Math.max(1, Math.round((Date.now() - started) / 1000));
            details.dataset.phase = 'answering';
            var label = details.querySelector('.ai-reasoning-label');
            if (label) { label.textContent = '已深度思考（用时 ' + seconds + ' 秒）'; }
            if (!details.dataset.user) { details.open = false; }
        }
    }

    // --- 判题验证工具（submit_to_judge）状态条 ------------------------------
    var TOOL_VERDICT_LABELS = {
        'Pending': '评测中',
        'Accepted': 'AC',
        'Wrong Answer': '答案错误',
        'Time Limit Exceeded': '超时',
        'Memory Limit Exceeded': '超内存',
        'Runtime Error': '运行错误',
        'Compile Error': '编译错误',
        'System Error': '评测系统错误',
        'Timeout': '判题超时',
        'Invalid': '参数无效',
        'Error': '调用失败'
    };
    function verdictClass(status, judged) {
        if (status === 'Accepted') { return 'is-accepted'; }
        if (status === 'Pending' || (judged === false && !status)) { return 'is-pending'; }
        return 'is-failed';
    }
    function verdictText(ev) {
        var label = ev.status_label || TOOL_VERDICT_LABELS[ev.status] || ev.status || '评测中';
        var parts = [label];
        if (ev.total_cases) { parts.push(ev.passed_cases + '/' + ev.total_cases); }
        if (ev.runtime_ms != null) { parts.push(ev.runtime_ms + 'ms'); }
        return parts.join(' · ');
    }
    function getToolCallsBox(card) {
        if (!card._toolChips) { card._toolChips = {}; }
        return card.querySelector('.ai-tool-calls');
    }
    // 模型发起工具调用：插入“评测中”状态条；本轮此前已流出的正文应作废。
    function onToolCall(card, ev) {
        var box = getToolCallsBox(card);
        var chip = document.createElement('div');
        chip.className = 'ai-tool-chip is-pending';
        chip.setAttribute('data-seq', ev.seq);
        var maxTxt = ev.max ? ev.seq + '/' + ev.max : '#' + ev.seq;
        chip.innerHTML =
            '<i class="bi bi-cpu"></i>' +
            '<span class="ai-tool-title">判题验证 ' + maxTxt +
                (ev.language ? ' · ' + escapeHtml(ev.language) : '') + '</span>' +
            '<span class="ai-tool-status"><span class="ai-spinner"></span> 评测中…</span>';
        box.appendChild(chip);
        card._toolChips[ev.seq] = chip;
        // 工具调用前把已流出的思考片段全部落盘（评测期间模型不在“思考”）。
        if (card._reasoningInited) {
            cancelReasoningFlush(card);
            flushReasoning(card);
            removeReasoningCursor(card);
        }
        // 工具调用轮流出的“半截正文”不是最终讲解，丢弃，等待最终轮重新输出。
        card._answerText = '';
        var body = card.querySelector('.ai-answer');
        if (body) { body.innerHTML = ''; }
        box.scrollIntoView({behavior: 'smooth', block: 'nearest'});
    }
    function onToolResult(card, ev) {
        var chip = card._toolChips && card._toolChips[ev.seq];
        if (!chip) { onToolCall(card, ev); chip = card._toolChips[ev.seq]; }
        var cls = ev.judged === false && ev.status === 'Pending'
            ? 'is-pending'
            : verdictClass(ev.status, ev.judged);
        chip.classList.remove('is-pending', 'is-accepted', 'is-failed');
        chip.classList.add(cls);
        var statusEl = chip.querySelector('.ai-tool-status');
        if (statusEl) { statusEl.textContent = verdictText(ev); }
        if (ev.error) { chip.title = ev.error; }
    }

    // --- 流式 Markdown + LaTeX 实时渲染 -----------------------------------
    // 每个 delta 都把“累计的 Markdown 原文”重新解析为 HTML（节流到 ~90ms），
    // 再由 MathJax 增量排版。关键点：
    //   1. 数学片段（$...$ / $$...$$ / \(...\) / \[...\]，含末尾未闭合的
    //      半截公式）必须在 Markdown 解析前挖空保护，否则其中的 _ * 等字符
    //      会被 marked 改写成 <em>，公式就毁了；
    //   2. 末尾未闭合的代码围栏只在“用于解析的副本”上补全，避免半截 ```
    //      把后面的所有正文吞成代码；
    //   3. MathJax 排版耗时较高，独立调度（250ms），排版进行中只标记 dirty，
    //      完成后按最新内容补排一次。
    var RENDER_MIN_INTERVAL_MS = 90;
    var MATH_RENDER_DELAY_MS = 250;
    var MATH_TOKEN_OPEN = '';
    var MATH_TOKEN_CLOSE = '';
    // 顺序很重要：先 $$ 后 $；每段都允许终止符缺省（停在字符串尾部）。
    var MATH_FRAGMENT_RE =
        /\$\$[\s\S]*?(?:\$\$|$)|\$(?:\\.|[^$\\\n])*(?:\$|$)|\\\[[\s\S]*?(?:\\\]|$)|\\\([\s\S]*?(?:\\\)|$)/g;

    if (window.marked && window.marked.setOptions) {
        try { window.marked.setOptions({gfm: true, breaks: false}); } catch (e) {}
    }

    function escapeHtml(s) {
        return s.replace(/&/g, '&amp;').replace(/</g, '&lt;')
                .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    // 挖空数学片段，返回供 marked 解析的安全文本与原文仓库。
    function protectMath(src) {
        var store = [];
        var safe = src.replace(MATH_FRAGMENT_RE, function (m) {
            var i = store.length;
            store.push(m);
            return MATH_TOKEN_OPEN + i + MATH_TOKEN_CLOSE;
        });
        return {safe: safe, store: store};
    }
    function restoreMath(html, store) {
        return html.replace(/(\d+)/g, function (_, i) {
            return store[+i] != null ? store[+i] : '';
        });
    }

    // 补全末尾未闭合的围栏代码块（仅作用于副本）。
    function withCompletedFences(src) {
        var inFence = null;
        src.split('\n').forEach(function (line) {
            if (inFence) {
                if (new RegExp('^\\s*' + inFence + '{3,}\\s*$').test(line)) { inFence = null; }
            } else {
                var m = /^\s*(`{3,}|~{3,})/.exec(line);
                if (m) { inFence = m[1][0]; }
            }
        });
        return inFence ? src + '\n' + inFence.repeat(3) + '\n' : src;
    }

    function markdownToHtml(raw) {
        var prepared = withCompletedFences(raw || '');
        var guarded = protectMath(prepared);
        var html;
        if (window.marked && window.marked.parse) {
            try {
                html = window.marked.parse(guarded.safe, {async: false});
            } catch (e) {
                html = '<p>' + escapeHtml(guarded.safe) + '</p>';
            }
        } else {
            // marked 尚未加载完成（罕见）：降级为纯文本，下一个片段再渲染。
            html = '<p>' + escapeHtml(guarded.safe) + '</p>';
        }
        return restoreMath(html, guarded.store);
    }

    function makeCursor() {
        var cursor = document.createElement('span');
        cursor.className = 'ai-cursor';
        return cursor;
    }

    function renderAnswerLive(card) {
        if (card._finalised) { return; }
        var body = card.querySelector('.ai-answer');
        body.innerHTML = markdownToHtml(card._answerText || '');
        body.appendChild(makeCursor());
        scheduleMathTypeset(card);
        card.scrollIntoView({block: 'nearest'});
    }

    // 90ms 节流的 Markdown 重渲染（rAF 对齐帧，且保证最后一次必执行）。
    function scheduleAnswerRender(card) {
        if (card._renderQueued || card._finalised) { return; }
        card._renderQueued = true;
        var wait = Math.max(
            0, RENDER_MIN_INTERVAL_MS - (Date.now() - (card._lastRenderAt || 0)),
        );
        setTimeout(function () {
            card._renderQueued = false;
            card._lastRenderAt = Date.now();
            renderAnswerLive(card);
        }, wait);
    }

    // MathJax 排版调度：含数学符号才排版；上一轮没排完就只记 dirty。
    function scheduleMathTypeset(card) {
        if (card._finalised) { return; }
        card._mathDirty = true;
        if (card._mathRunning || card._mathTimer) { return; }
        card._mathTimer = setTimeout(function () {
            card._mathTimer = null;
            runMathTypeset(card);
        }, MATH_RENDER_DELAY_MS);
    }
    function runMathTypeset(card) {
        if (card._finalised || !card.parentNode) { return; }
        if (!window.MathJax || !MathJax.typesetPromise) {
            card._mathDirty = false; // 最终 done 时还会整体排一次
            return;
        }
        var raw = card._answerText || '';
        if (!/[$]|\\\(|\\\[/.test(raw)) { card._mathDirty = false; return; }
        card._mathDirty = false;
        card._mathRunning = true;
        var body = card.querySelector('.ai-answer');
        Promise.resolve()
            .then(function () { return MathJax.typesetPromise([body]); })
            .catch(function () { /* 半截公式抛错可忽略，下一帧会重排 */ })
            .then(function () {
                card._mathRunning = false;
                if (card._mathDirty && !card._finalised) { runMathTypeset(card); }
            });
    }

    // 正文片段：累计原文并调度重渲染，绝不碰思考面板的 open 状态。
    function appendDelta(card, text) {
        card._answerText = (card._answerText || '') + text;
        scheduleAnswerRender(card);
    }

    // 完成：替换答案为服务端渲染的权威 HTML，思考面板状态定格为 done。
    function finaliseCard(card, answerHtml) {
        card._finalised = true; // 终止一切流式重渲染/排版调度
        cancelReasoningFlush(card);
        flushReasoning(card);       // 兜底：确保末尾思考片段不丢
        removeReasoningCursor(card);
        var details = card.querySelector('.ai-reasoning');
        if (details && !details.classList.contains('d-none')) {
            details.dataset.phase = 'done';
        }
        var body = card.querySelector('.ai-answer');
        body.classList.remove('ai-answer-streaming');
        body.innerHTML = answerHtml;
        typeset(card);
        card.scrollIntoView({block: 'nearest'});
    }

    function discardCard(card) {
        if (card) {
            card._finalised = true;
            cancelReasoningFlush(card);
        }
        if (card && card.parentNode) { card.parentNode.removeChild(card); }
    }

    // --- SSE parsing -------------------------------------------------------
    // Reads the response body as text, splits on the SSE frame separator
    // (\n\n), and hands each `data: {...}` payload to onEvent. Any bytes
    // (including ": ping" heartbeats) reset the stall watchdog.
    function readSSE(response, onEvent) {
        var reader = response.body.getReader();
        var decoder = new TextDecoder('utf-8');
        var buffer = '';

        function handleFrame(frame) {
            frame.split('\n').forEach(function (line) {
                line = line.replace(/\r$/, '');
                if (line.indexOf('data:') !== 0) { return; } // 含 ": ping" 心跳
                var payload = line.slice(5).trim();
                if (!payload || payload === '[DONE]') { return; }
                try {
                    onEvent(JSON.parse(payload));
                } catch (e) {
                    // Ignore malformed frames rather than killing the stream.
                }
            });
        }

        function nextRead() {
            // 与 read() 竞速：长时间完全无字节说明连接已被中间设备静默掐断。
            return new Promise(function (resolve, reject) {
                var watchdog = setTimeout(function () {
                    reject(new Error('AI 长时间没有响应，可能是网络中断，请重新生成。'));
                }, STALL_TIMEOUT_MS);
                reader.read().then(
                    function (result) { clearTimeout(watchdog); resolve(result); },
                    function (err) { clearTimeout(watchdog); reject(err); }
                );
            }).then(function (result) {
                if (result.done) {
                    if (buffer.trim()) { handleFrame(buffer); }
                    return;
                }
                buffer += decoder.decode(result.value, {stream: true});
                var idx;
                while ((idx = buffer.indexOf('\n\n')) >= 0) {
                    var frame = buffer.slice(0, idx);
                    buffer = buffer.slice(idx + 2);
                    handleFrame(frame);
                }
                return nextRead();
            });
        }
        return nextRead();
    }

    // --- actions -----------------------------------------------------------
    function renderActions(data) {
        var html = '';
        if (SATISFIED) {
            html = '<div class="alert alert-success mb-3"><i class="bi bi-check-circle"></i> 已采纳本次讲解，祝你早日 AC！</div>' +
                   '<button type="button" id="btn-new" class="btn btn-outline-primary px-4">' +
                   '<i class="bi bi-arrow-repeat"></i> 再次提问（新的一次互动）</button>';
        } else {
            html = '<button type="button" id="btn-satisfied" class="btn btn-success px-4 me-2">' +
                   '<i class="bi bi-check2-circle"></i> 回答满意</button>';
            if (data.can_regenerate) {
                html += '<button type="button" id="btn-regenerate" class="btn btn-outline-primary px-4">' +
                        '<i class="bi bi-arrow-clockwise"></i> 重新生成</button>';
            }
        }
        controls.innerHTML = html;
        bindActions();
    }
    function renderQuotaExceeded() {
        controls.innerHTML =
            '<div class="alert alert-warning mb-3">本周期的 AI 解题次数已用完，升级会员可获得更多次数。</div>' +
            '<a href="/ai/pricing/" class="btn btn-warning px-4"><i class="bi bi-gem"></i> 升级会员</a>';
        controls.classList.remove('d-none');
    }

    // 流式输出期间底部状态条：随思考/作答阶段切换文案。
    function setStreamStatus(phase) {
        var el = document.getElementById('ai-status-text');
        if (!el) { return; }
        if (phase === 'thinking') {
            el.textContent = '正在深度思考，思考过程会实时显示…';
        } else if (phase === 'judging') {
            el.textContent = '正在调用判题系统真实验证思路，请稍候…';
        } else {
            el.textContent = '正在输出讲解…';
        }
    }

    function generate(opts) {
        clearError();
        setLoading(true);

        var fresh = !!(opts && opts.fresh);
        var continueGenId = opts && opts.continue_generation_id;
        var payload = {};
        // A satisfied interaction starts a brand-new session server-side.
        if (SESSION_ID && !fresh && !SATISFIED && !continueGenId) { payload.session_id = SESSION_ID; }
        if (continueGenId) { payload.continue_generation_id = continueGenId; }

        var controller = new AbortController();
        activeStream = controller;
        var card = null;
        var finished = false;
        var gotDelta = false;

        fetch('/ai/problem/' + PROBLEM_ID + '/generate/', {
            method: 'POST',
            credentials: 'same-origin',
            signal: controller.signal,
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken(),
                'Accept': 'text/event-stream, application/json'
            },
            body: JSON.stringify(payload)
        }).then(function (resp) {
            var ctype = resp.headers.get('Content-Type') || '';
            // Pre-flight rejections (rate limit / round limit / quota) come
            // back as plain JSON before the stream opens.
            if (ctype.indexOf('text/event-stream') === -1) {
                return resp.text().then(function (raw) {
                    console.error('[AI] non-stream response', resp.status, ctype, raw.slice(0, 500));
                    setLoading(false);
                    controls.classList.remove('d-none');
                    var data = {};
                    try { data = JSON.parse(raw); } catch (e) {}
                    if (data.code === 'quota_exceeded') { renderQuotaExceeded(); return; }
                    showError(data.message || ('服务返回异常 (' + resp.status + ')'));
                });
            }
            if (!resp.ok) {
                setLoading(false);
                controls.classList.remove('d-none');
                showError('生成失败，请稍后再试。');
                return;
            }

            if (fresh) { conversation.innerHTML = ''; }
            setLoading(false);
            controls.classList.remove('d-none');
            controls.innerHTML =
                '<span class="text-muted"><span class="ai-spinner text-primary"></span>' +
                '<span class="ms-2" id="ai-status-text">AI 正在准备作答…</span></span>';

            return readSSE(resp, function (ev) {
                if (ev.type === 'start') {
                    SESSION_ID = ev.session_id;
                    GEN_COUNT = ev.round;
                    SATISFIED = false;
                    if (ev.continue) {
                        // Continuation: reuse the existing card for this gen.
                        card = document.querySelector(
                            '.ai-message[data-gen-id="' + ev.generation_id + '"]'
                        );
                        if (card) {
                            // Reset streaming state on the existing card.
                            card._finalised = false;
                            card._answerText = card._answerText || '';
                            var ansBody = card.querySelector('.ai-answer');
                            if (ansBody) { ansBody.classList.add('ai-answer-streaming'); }
                            // Remove the interrupted banner + continue button.
                            var banner = card.querySelector('.alert-warning');
                            if (banner) { banner.remove(); }
                        }
                    } else {
                        card = createStreamingCard(ev.round);
                    }
                } else if (ev.type === 'reasoning') {
                    if (!card) { card = createStreamingCard(GEN_COUNT || 1); }
                    appendReasoning(card, ev.text);
                    setStreamStatus('thinking');
                } else if (ev.type === 'delta') {
                    if (!card) { card = createStreamingCard(GEN_COUNT || 1); }
                    if (!gotDelta) {
                        gotDelta = true;
                        beginAnswer(card);
                        setStreamStatus('answering');
                    }
                    appendDelta(card, ev.text);
                } else if (ev.type === 'tool_call') {
                    if (!card) { card = createStreamingCard(GEN_COUNT || 1); }
                    onToolCall(card, ev);
                    setStreamStatus('judging');
                } else if (ev.type === 'tool_result') {
                    if (card) { onToolResult(card, ev); }
                    setStreamStatus('answering');
                } else if (ev.type === 'done') {
                    finished = true;
                    if (card) { finaliseCard(card, ev.answer_html); }
                    renderActions(ev);
                } else if (ev.type === 'error') {
                    finished = true;
                    if (!ev.continue) { discardCard(card); }
                    card = null;
                    showError(ev.message || '生成失败，请稍后再试。');
                    controls.classList.remove('d-none');
                    renderActions({can_regenerate: GEN_COUNT < MAX_ROUNDS});
                }
            });
        }).catch(function (err) {
            if (err && err.name === 'AbortError') { return; }
            // 看门狗超时时底层 fetch 可能还挂着，主动断开。
            try { controller.abort(); } catch (e) {}
            setLoading(false);
            controls.classList.remove('d-none');
            if (!finished && card && !continueGenId) { discardCard(card); }
            console.error('[AI] generate failed:', err);
            showError((err && err.message) ? err.message : String(err));
        }).then(function () {
            if (activeStream === controller) { activeStream = null; }
        });
    }

    function markSatisfied() {
        clearError();
        fetch('/ai/session/' + SESSION_ID + '/satisfied/', {
            method: 'POST', credentials: 'same-origin',
            headers: {'X-CSRFToken': csrfToken(), 'Accept': 'application/json'}
        }).then(function (resp) { return resp.json(); }).then(function () {
            SATISFIED = true;
            renderActions({can_regenerate: false});
        }).catch(function () { showError('操作失败，请刷新后重试。'); });
    }

    function bindActions() {
        var bGen = document.getElementById('btn-generate');
        var bReg = document.getElementById('btn-regenerate');
        var bSat = document.getElementById('btn-satisfied');
        var bNew = document.getElementById('btn-new');
        if (bGen) bGen.addEventListener('click', function () { generate({fresh: false}); });
        if (bReg) bReg.addEventListener('click', function () { generate({fresh: false}); });
        if (bSat) bSat.addEventListener('click', markSatisfied);
        if (bNew) bNew.addEventListener('click', function () {
            SATISFIED = true;
            conversation.innerHTML = '';
            generate({fresh: true});
        });
    }

    // Expose for inline onclick handlers (bypasses any JS caching issues).
    window.__aiContinue = function (genId) {
        generate({continue_generation_id: genId});
    };

    bindActions();

    // Abort an in-flight stream if the user navigates away.
    window.addEventListener('beforeunload', function () {
        if (activeStream) { try { activeStream.abort(); } catch (e) {} }
    });
})();
