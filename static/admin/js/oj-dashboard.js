(function () {
    'use strict';

    var root = document.getElementById('oj-dashboard-content');
    if (!root || root.dataset.ojDashboardLoaded) return;
    root.dataset.ojDashboardLoaded = 'true';

    var palette = {
        Accepted: '#10b981', 'Wrong Answer': '#f59e0b',
        'Time Limit Exceeded': '#8b5cf6', 'Memory Limit Exceeded': '#ec4899',
        'Runtime Error': '#ef4444', 'Compile Error': '#64748b',
        'System Error': '#475569', Pending: '#3b82f6'
    };

    function escapeHtml(value) {
        return String(value == null ? '' : value).replace(/[&<>'"]/g, function (character) {
            return {'&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'}[character];
        });
    }

    function chartScale(maximum) {
        var raw = maximum / 4;
        var power = Math.pow(10, Math.floor(Math.log(raw || 1) / Math.LN10));
        var step = Math.max(1, Math.ceil(raw / power) * power);
        return {top: step * 4, values: [0, step, step * 2, step * 3, step * 4]};
    }

    function renderChart(id, title, icon, labels, values, color, noData) {
        var titleHtml = '<div><h3 class="oj-dashboard__card-title"><i class="' + icon + '"></i> ' + title + '</h3><p class="oj-dashboard__card-subtitle">近 14 天趋势</p></div>';
        if (noData) {
            return '<section class="oj-dashboard__card"><header class="oj-dashboard__card-header">' + titleHtml + '</header><div class="oj-dashboard__no-data"><div><i class="fas fa-chart-area"></i><b>尚未收集到公开页面访问数据</b><br>部署后，成功访问公开 OJ 页面会自动计入这里。</div></div></section>';
        }
        var scale = chartScale(Math.max.apply(null, values.concat([1])));
        var left = 42, right = 610, top = 28, bottom = 174, count = Math.max(values.length - 1, 1);
        function point(value, index) {
            return [(left + index * (right - left) / count).toFixed(1), (bottom - value / scale.top * (bottom - top)).toFixed(1)];
        }
        var points = values.map(function (value, index) { return point(value, index).join(','); }).join(' ');
        var grid = scale.values.map(function (value) {
            var y = bottom - value / scale.top * (bottom - top);
            return '<line class="oj-dashboard__chart-grid" x1="' + left + '" y1="' + y + '" x2="' + right + '" y2="' + y + '" stroke="#e9eff6"/><text class="oj-dashboard__chart-label" x="34" y="' + (y + 3) + '" text-anchor="end" fill="#99a6b7">' + value + '</text>';
        }).join('');
        var xLabels = labels.map(function (label, index) {
            if (index % 3 && index !== labels.length - 1) return '';
            return '<text class="oj-dashboard__chart-label" x="' + point(0, index)[0] + '" y="204" text-anchor="middle" fill="#99a6b7">' + escapeHtml(label) + '</text>';
        }).join('');
        var latest = values[values.length - 1] || 0;
        var latestPoint = point(latest, values.length - 1);
        var gradientId = 'oj-dashboard-gradient-' + id;
        var hitboxes = values.map(function (value, index) {
            var currentPoint = point(value, index);
            return '<rect class="oj-dashboard__chart-hitbox" x="' + (Number(currentPoint[0]) - 22) + '" y="' + top + '" width="44" height="' + (bottom - top) + '" data-chart-id="' + id + '" data-index="' + index + '" tabindex="0" aria-label="' + escapeHtml(labels[index]) + ': ' + value + '"></rect>';
        }).join('');
        return '<section class="oj-dashboard__card" style="--chart-color:' + color + '"><header class="oj-dashboard__card-header">' + titleHtml + '<div class="oj-dashboard__current">' + latest + '<small>今日</small></div></header><div class="oj-dashboard__chart-wrap"><svg class="oj-dashboard__chart" viewBox="0 0 650 215" role="img" aria-label="' + title + '">' + grid + '<defs><linearGradient id="' + gradientId + '" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="' + color + '" stop-opacity=".34"/><stop offset="100%" stop-color="' + color + '" stop-opacity="0"/></linearGradient></defs><polygon points="' + left + ',' + bottom + ' ' + points + ' ' + right + ',' + bottom + '" fill="url(#' + gradientId + ')"/><polyline points="' + points + '" fill="none" stroke="' + color + '" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/><circle cx="' + latestPoint[0] + '" cy="' + latestPoint[1] + '" r="5" fill="' + color + '" stroke="#ffffff" stroke-width="3"/><text class="oj-dashboard__chart-value" x="' + latestPoint[0] + '" y="' + Math.max(18, Number(latestPoint[1]) - 12) + '" text-anchor="end" fill="#25344a">' + latest + '</text>' + xLabels + hitboxes + '<line class="oj-dashboard__hover-line" x1="0" y1="' + top + '" x2="0" y2="' + bottom + '"/><circle class="oj-dashboard__hover-dot" cx="0" cy="0" r="6"/></svg><div class="oj-dashboard__tooltip" role="status"></div></div></section>';
    }

    function render(data) {
        var summary = data.summary || {};
        var metrics = [
            ['fas fa-users', '注册用户', summary.users], ['fas fa-book', '公开题目', summary.problems],
            ['fas fa-paper-plane', '提交总数', summary.submissions], ['fas fa-check-circle', '通过率', (summary.acceptance_rate || 0) + '%'],
            ['fas fa-database', '数据库存储', summary.database_size], ['fas fa-heartbeat', '服务健康', summary.health]
        ];
        var metricHtml = metrics.map(function (metric) {
            return '<article class="oj-dashboard__metric"><i class="oj-dashboard__metric-icon ' + metric[0] + '"></i><span class="oj-dashboard__metric-label">' + metric[1] + '</span><strong class="oj-dashboard__metric-value">' + escapeHtml(metric[2]) + '</strong></article>';
        }).join('');
        var verdicts = data.verdicts || {};
        var total = Object.keys(verdicts).reduce(function (sum, status) { return sum + verdicts[status]; }, 0);
        var maximum = Math.max.apply(null, Object.keys(verdicts).map(function (status) { return verdicts[status]; }).concat([1]));
        var verdictHtml = Object.keys(verdicts).map(function (status) {
            var count = verdicts[status], percent = total ? (count / total * 100).toFixed(1) : '0.0';
            return '<div class="oj-dashboard__verdict-row" style="--verdict-color:' + (palette[status] || '#64748b') + '"><span class="oj-dashboard__verdict-name">' + escapeHtml(status) + '</span><div class="oj-dashboard__verdict-track"><i class="oj-dashboard__verdict-fill" style="width:' + (count / maximum * 100) + '%"></i></div><span class="oj-dashboard__verdict-value"><b>' + count + '</b> · ' + percent + '%</span></div>';
        }).join('') || '<div class="oj-dashboard__empty">暂无提交数据</div>';
        var topPages = data.top_pages || [], pageMaximum = Math.max.apply(null, topPages.map(function (page) { return page.page_views; }).concat([1]));
        var pageHtml = topPages.map(function (page, index) { return '<li class="oj-dashboard__rank-row"><span class="oj-dashboard__rank-number">' + (index + 1) + '</span><span class="oj-dashboard__rank-name">' + escapeHtml(page.path) + '</span><span class="oj-dashboard__rank-bar"><i style="width:' + (page.page_views / pageMaximum * 100) + '%"></i></span><b>' + page.page_views + '</b></li>'; }).join('') || '<div class="oj-dashboard__empty">尚未积累页面访问排名数据</div>';
        var topProblems = data.top_problems || [], problemMaximum = Math.max.apply(null, topProblems.map(function (problem) { return problem.submissions; }).concat([1]));
        var problemHtml = topProblems.map(function (problem, index) { return '<li class="oj-dashboard__rank-row"><span class="oj-dashboard__rank-number">' + (index + 1) + '</span><a class="oj-dashboard__rank-name" href="/admin/problems/problem/' + problem.id + '/change/">P' + problem.id + ' · ' + escapeHtml(problem.title) + '</a><span class="oj-dashboard__rank-bar oj-dashboard__rank-bar--green"><i style="width:' + (problem.submissions / problemMaximum * 100) + '%"></i></span><b>' + problem.submissions + '</b></li>'; }).join('') || '<div class="oj-dashboard__empty">暂无题目提交数据</div>';
        root.innerHTML = '<div class="oj-dashboard__summary">' + metricHtml + '</div><div class="oj-dashboard__charts">' + renderChart('traffic', '公开页面访问量', 'fas fa-chart-line', data.labels || [], data.traffic || [], '#3b82f6', !data.traffic_has_data) + renderChart('submissions', '提交量', 'fas fa-code', data.labels || [], data.submissions || [], '#10b981', false) + '</div><section class="oj-dashboard__card"><header class="oj-dashboard__verdict-header"><h3 class="oj-dashboard__card-title"><i class="fas fa-chart-pie"></i> 提交结果分布</h3><p class="oj-dashboard__card-subtitle">所有历史提交</p></header>' + verdictHtml + '</section><div class="oj-dashboard__rankings"><section class="oj-dashboard__card"><header class="oj-dashboard__verdict-header"><h3 class="oj-dashboard__card-title"><i class="fas fa-eye"></i> 最常访问页面</h3><p class="oj-dashboard__card-subtitle">近 14 天公开页面访问</p></header><ol class="oj-dashboard__rank-list">' + pageHtml + '</ol></section><section class="oj-dashboard__card"><header class="oj-dashboard__verdict-header"><h3 class="oj-dashboard__card-title"><i class="fas fa-fire"></i> 提交最多的题目</h3><p class="oj-dashboard__card-subtitle">所有历史普通题目提交</p></header><ol class="oj-dashboard__rank-list">' + problemHtml + '</ol></section></div>';
        bindChartTooltips(data);
    }

    function bindChartTooltips(data) {
        Array.prototype.forEach.call(root.querySelectorAll('.oj-dashboard__chart-hitbox'), function (hitbox) {
            function show() {
                var id = hitbox.getAttribute('data-chart-id');
                var index = Number(hitbox.getAttribute('data-index'));
                var values = id === 'traffic' ? data.traffic : data.submissions;
                var labels = data.labels || [];
                var svg = hitbox.ownerSVGElement;
                var x = Number(hitbox.getAttribute('x')) + Number(hitbox.getAttribute('width')) / 2;
                var wrap = svg.parentNode;
                var tooltip = wrap.querySelector('.oj-dashboard__tooltip');
                var line = svg.querySelector('.oj-dashboard__hover-line');
                var dot = svg.querySelector('.oj-dashboard__hover-dot');
                var top = 28, bottom = 174, scale = chartScale(Math.max.apply(null, values.concat([1])));
                var y = bottom - values[index] / scale.top * (bottom - top);
                line.setAttribute('x1', x); line.setAttribute('x2', x); line.style.opacity = '1';
                dot.setAttribute('cx', x); dot.setAttribute('cy', y); dot.style.opacity = '1';
                tooltip.textContent = labels[index] + '：' + values[index];
                tooltip.style.left = Math.min(88, Math.max(8, x / 650 * 100)) + '%';
                tooltip.style.opacity = '1';
            }
            function hide() {
                var svg = hitbox.ownerSVGElement;
                svg.querySelector('.oj-dashboard__hover-line').style.opacity = '0';
                svg.querySelector('.oj-dashboard__hover-dot').style.opacity = '0';
                svg.parentNode.querySelector('.oj-dashboard__tooltip').style.opacity = '0';
            }
            hitbox.addEventListener('mouseenter', show);
            hitbox.addEventListener('focus', show);
            hitbox.addEventListener('mouseleave', hide);
            hitbox.addEventListener('blur', hide);
        });
    }

    fetch('/admin/dashboard-metrics/', {credentials: 'same-origin'})
        .then(function (response) { if (!response.ok) throw new Error('dashboard response'); return response.json(); })
        .then(render)
        .catch(function () { root.innerHTML = '<div class="oj-dashboard__empty">运营数据暂时无法加载，请稍后刷新页面。</div>'; });
})();
