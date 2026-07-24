(function () {
    'use strict';
    var chart = document.getElementById('oj-user-traffic-chart');
    if (!chart) return;
    var id = chart.dataset.userId;
    fetch('/admin/users/user/' + encodeURIComponent(id) + '/traffic-data/?_=' + Date.now(), {credentials: 'same-origin', cache: 'no-store'})
        .then(function (response) { if (!response.ok) throw new Error('traffic data'); return response.json(); })
        .then(function (data) {
            var hourly = {};
            (data.rows || []).forEach(function (row) {
                var parsed = new Date(row.hour);
                if (isNaN(parsed.getTime())) return;
                var key = parsed.toISOString().slice(0, 13) + ':00:00.000Z';
                hourly[key] = (hourly[key] || 0) + Number(row.page_views || 0);
            });
            var now = new Date();
            now.setMinutes(0, 0, 0);
            var labels = [], values = [];
            for (var i = 24 * 90 - 1; i >= 0; i -= 1) {
                var current = new Date(now.getTime() - i * 3600000);
                var key = current.toISOString().slice(0, 13) + ':00:00.000Z';
                labels.push(current.toLocaleString([], {month: '2-digit', day: '2-digit', hour: '2-digit'}));
                values.push(hourly[key] || 0);
            }
            var max = Math.max.apply(null, values.concat([1]));
            var width = Math.max(1100, values.length * 5.2);
            var bars = values.map(function (value, index) {
                var height = value / max * 170;
                return '<rect class="oj-user-traffic-bar" data-label="' + labels[index] + '" data-value="' + value + '" x="' + (index * 5.2) + '" y="' + (190 - height) + '" width="4" height="' + height + '" rx="1"></rect>';
            }).join('');
            chart.innerHTML = '<p class="help">近 90 天，每列 1 小时；鼠标悬停查看访问次数</p><div style="overflow-x:auto;position:relative"><svg id="oj-user-traffic-svg" viewBox="0 0 ' + width + ' 215" role="img" aria-label="用户每小时浏览频率图" style="width:' + width + 'px;max-width:none;height:260px"><line x1="0" y1="190" x2="' + width + '" y2="190" stroke="#cbd5e1"></line><g fill="#3b82f6">' + bars + '</g><line id="oj-user-traffic-guide" x1="0" y1="10" x2="0" y2="190" stroke="#64748b" stroke-dasharray="4 4" opacity="0"></line></svg><div id="oj-user-traffic-tooltip" style="position:absolute;display:none;pointer-events:none;padding:.45rem .65rem;border-radius:.5rem;background:#172033;color:#fff;font-size:.8rem;white-space:nowrap;z-index:2"></div></div>';
            var svg = document.getElementById('oj-user-traffic-svg');
            var guide = document.getElementById('oj-user-traffic-guide');
            var tooltip = document.getElementById('oj-user-traffic-tooltip');
            Array.prototype.forEach.call(svg.querySelectorAll('.oj-user-traffic-bar'), function (bar) {
                function show(event) {
                    var x = Number(bar.getAttribute('x')) + 2;
                    guide.setAttribute('x1', x); guide.setAttribute('x2', x); guide.setAttribute('opacity', '1');
                    tooltip.textContent = bar.dataset.label + '：' + bar.dataset.value + ' 次';
                    tooltip.style.display = 'block';
                    tooltip.style.left = Math.max(4, event.offsetX - 42) + 'px';
                    tooltip.style.top = Math.max(4, event.offsetY - 42) + 'px';
                }
                function hide() { guide.setAttribute('opacity', '0'); tooltip.style.display = 'none'; }
                bar.addEventListener('mouseenter', show); bar.addEventListener('mousemove', show); bar.addEventListener('mouseleave', hide);
            });
        })
        .catch(function () { chart.textContent = '浏览数据暂时无法加载。'; });
})();
