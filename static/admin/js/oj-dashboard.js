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

    function renderLocationMap(data) {
        var locations = data.locations || [];
        var destination = data.server_location;
        function project(item) {
            return [25 + (Number(item.longitude) + 180) / 360 * 600, 25 + (90 - Number(item.latitude)) / 180 * 250];
        }
        var server = destination ? project(destination) : null;
        var lines = server ? locations.map(function (item, index) {
            var source = project(item), midX = (source[0] + server[0]) / 2, midY = Math.min(source[1], server[1]) - 32 - (index % 3) * 8;
            var path = 'M' + source[0].toFixed(1) + ' ' + source[1].toFixed(1) + ' Q' + midX.toFixed(1) + ' ' + midY.toFixed(1) + ' ' + server[0].toFixed(1) + ' ' + server[1].toFixed(1);
            return '<path class="oj-dashboard__request-route" d="' + path + '" style="--route-delay:' + (index * 180) + 'ms"></path><circle class="oj-dashboard__request-packet" r="3.5"><animateMotion dur="2.2s" begin="' + (index * 180) + 'ms" repeatCount="indefinite" path="' + path + '"></animateMotion></circle>';
        }).join('') : '';
        var dots = locations.map(function (item) {
            var point = project(item);
            return '<g class="oj-dashboard__geo-point"><circle cx="' + point[0].toFixed(1) + '" cy="' + point[1].toFixed(1) + '" r="5"><title>' + escapeHtml(item.country_name) + '：' + item.requests + ' 次请求</title></circle></g>';
        }).join('');
        var serverDot = server ? '<g class="oj-dashboard__server-point"><circle cx="' + server[0].toFixed(1) + '" cy="' + server[1].toFixed(1) + '" r="8"></circle></g>' : '';
        var list = locations.slice(0, 8).map(function (item) { return '<li><span>' + escapeHtml(item.country_name) + '</span><b>' + item.requests + '</b></li>'; }).join('');
        if (!locations.length) list = '<li class="oj-dashboard__geo-empty">暂无可定位的公开请求。请访问前台页面生成请求来源数据。</li>';
        if (locations.length && !destination) list += '<li class="oj-dashboard__geo-empty">已记录来源，但未配置 OJ_SERVER_IP，无法绘制到服务器的线路。</li>';
        return '<section class="oj-dashboard__card oj-dashboard__geo-card"><header class="oj-dashboard__card-header"><div><h3 class="oj-dashboard__card-title"><i class="fas fa-globe-asia"></i> 请求来源地图</h3><p class="oj-dashboard__card-subtitle">请求沿线路流向服务器 · 近 14 天国家级聚合 · 鼠标中键滚轮缩放</p></div></header><div class="oj-dashboard__geo-layout"><div class="oj-dashboard__map-wrap"><svg class="oj-dashboard__map" viewBox="0 0 650 300" role="img" aria-label="请求来源到服务器的线路地图"><rect width="650" height="300" rx="12" fill="#eff7ff"/><g class="oj-dashboard__map-scene"><g class="oj-dashboard__countries"></g><path class="oj-dashboard__graticule" d="M20 75h610M20 150h610M20 225h610M182 20v260M325 20v260M468 20v260"/>' + lines + dots + serverDot + '</g></svg></div><ol class="oj-dashboard__geo-list">' + list + '</ol></div></section>';
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
        root.innerHTML = '<div class="oj-dashboard__summary">' + metricHtml + '</div>' + renderLocationMap(data) + '<div class="oj-dashboard__charts">' + renderChart('traffic', '公开页面访问量', 'fas fa-chart-line', data.labels || [], data.traffic || [], '#3b82f6', !data.traffic_has_data) + renderChart('submissions', '提交量', 'fas fa-code', data.labels || [], data.submissions || [], '#10b981', false) + '</div><section class="oj-dashboard__card"><header class="oj-dashboard__verdict-header"><h3 class="oj-dashboard__card-title"><i class="fas fa-chart-pie"></i> 提交结果分布</h3><p class="oj-dashboard__card-subtitle">所有历史提交</p></header>' + verdictHtml + '</section><div class="oj-dashboard__rankings"><section class="oj-dashboard__card"><header class="oj-dashboard__verdict-header"><h3 class="oj-dashboard__card-title"><i class="fas fa-eye"></i> 最常访问页面</h3><p class="oj-dashboard__card-subtitle">近 14 天公开页面访问</p></header><ol class="oj-dashboard__rank-list">' + pageHtml + '</ol></section><section class="oj-dashboard__card"><header class="oj-dashboard__verdict-header"><h3 class="oj-dashboard__card-title"><i class="fas fa-fire"></i> 提交最多的题目</h3><p class="oj-dashboard__card-subtitle">所有历史普通题目提交</p></header><ol class="oj-dashboard__rank-list">' + problemHtml + '</ol></section></div>';
        renderWorldCountries();
        bindMapZoom();
        bindChartTooltips(data);
    }


    function renderWorldCountries() {
        var container = root.querySelector('.oj-dashboard__countries');
        if (!container || container.dataset.loaded) return;
        container.dataset.loaded = 'true';
        fetch('/static/admin/data/world-countries.geojson', {credentials: 'same-origin'})
            .then(function (response) { if (!response.ok) throw new Error('world map response'); return response.json(); })
            .then(function (world) {
                var features = (world && world.features) || [];
                function project(point) {
                    return [25 + (point[0] + 180) / 360 * 600, 25 + (90 - point[1]) / 180 * 250];
                }
                function ringPath(ring) {
                    return ring.map(function (point, index) {
                        var projected = project(point);
                        return (index ? 'L' : 'M') + projected[0].toFixed(1) + ' ' + projected[1].toFixed(1);
                    }).join(' ') + 'Z';
                }
                var paths = features.map(function (feature) {
                    var geometry = feature.geometry || {};
                    var polygons = geometry.type === 'Polygon' ? [geometry.coordinates] : geometry.coordinates || [];
                    return polygons.map(function (polygon) {
                        return '<path class="oj-dashboard__country" d="' + polygon.map(ringPath).join(' ') + '"></path>';
                    }).join('');
                }).join('');
                Array.prototype.forEach.call(root.querySelectorAll('.oj-dashboard__countries'), function (countryLayer) {
                    countryLayer.innerHTML = paths;
                });
            })
            .catch(function () { container.classList.add('oj-dashboard__countries--unavailable'); });
    }
    function bindMapZoom() {
        var map = root.querySelector('.oj-dashboard__map');
        var scene = root.querySelector('.oj-dashboard__map-scene');
        if (!map || !scene || map.dataset.zoomBound) return;
        map.dataset.zoomBound = 'true';
        var zoom = 1, tx = 0, ty = 0, dragging = false, lastX = 0, lastY = 0;
        var worldWidth = 650, worldHeight = 300, tileCache = {};
        var sceneCopies = [scene];
        scene.dataset.xOffset = '0'; scene.dataset.yOffset = '0';
        function rebuildTiles() {
            var tileWidth = worldWidth * zoom, tileHeight = worldHeight * zoom;
            var firstX = Math.floor(-tx / tileWidth) - 1;
            var lastX = Math.ceil((650 - tx) / tileWidth) + 1;
            var firstY = Math.floor(-ty / tileHeight) - 1;
            var lastY = Math.ceil((300 - ty) / tileHeight) + 1;
            var wanted = {};
            for (var x = firstX; x <= lastX; x += 1) {
                for (var y = firstY; y <= lastY; y += 1) {
                    var key = x + ':' + y;
                    wanted[key] = true;
                    if (!tileCache[key]) {
                        var copy = x === 0 && y === 0 ? scene : scene.cloneNode(true);
                        if (copy !== scene) {
                            copy.classList.add('oj-dashboard__map-scene-copy');
                            scene.parentNode.appendChild(copy);
                        }
                        copy.dataset.xOffset = String(x); copy.dataset.yOffset = String(y);
                        tileCache[key] = copy;
                    }
                }
            }
            Object.keys(tileCache).forEach(function (key) {
                if (!wanted[key] && tileCache[key] !== scene) {
                    tileCache[key].remove(); delete tileCache[key];
                }
            });
            sceneCopies = Object.keys(wanted).map(function (key) { return tileCache[key]; });
        }
        function updateMarkerScale() {
            sceneCopies.forEach(function (copy) {
                Array.prototype.forEach.call(copy.querySelectorAll('.oj-dashboard__geo-point circle:first-child'), function (marker) { marker.setAttribute('r', (5 / zoom).toFixed(2)); });
                Array.prototype.forEach.call(copy.querySelectorAll('.oj-dashboard__server-point circle'), function (marker) { marker.setAttribute('r', (8 / zoom).toFixed(2)); });
                Array.prototype.forEach.call(copy.querySelectorAll('.oj-dashboard__request-packet'), function (packet) { packet.setAttribute('r', (3.5 / zoom).toFixed(2)); });
            });
        }
        function apply() {
            rebuildTiles();
            sceneCopies.forEach(function (copy) {
                var x = tx + Number(copy.dataset.xOffset || 0) * worldWidth * zoom;
                var y = ty + Number(copy.dataset.yOffset || 0) * worldHeight * zoom;
                copy.setAttribute('transform', 'translate(' + x.toFixed(2) + ' ' + y.toFixed(2) + ') scale(' + zoom.toFixed(3) + ')');
            });
            updateMarkerScale();
        }
        map.addEventListener('pointerdown', function (event) {
            if (event.button !== 0) return;
            dragging = true; lastX = event.clientX; lastY = event.clientY;
            map.setPointerCapture(event.pointerId); map.classList.add('oj-dashboard__map--dragging'); event.preventDefault();
        });
        map.addEventListener('pointermove', function (event) {
            if (!dragging) return;
            var rect = map.getBoundingClientRect();
            tx += (event.clientX - lastX) / rect.width * worldWidth;
            ty += (event.clientY - lastY) / rect.height * worldHeight;
            lastX = event.clientX; lastY = event.clientY; apply();
        });
        function stopDragging(event) {
            if (!dragging) return;
            dragging = false;
            if (event && map.hasPointerCapture(event.pointerId)) map.releasePointerCapture(event.pointerId);
            map.classList.remove('oj-dashboard__map--dragging');
        }
        map.addEventListener('pointerup', stopDragging); map.addEventListener('pointercancel', stopDragging);
        map.addEventListener('wheel', function (event) {
            event.preventDefault();
            var rect = map.getBoundingClientRect();
            var px = (event.clientX - rect.left) / rect.width * worldWidth;
            var py = (event.clientY - rect.top) / rect.height * worldHeight;
            var next = Math.max(1, Math.min(5, zoom * (event.deltaY < 0 ? 1.18 : 1 / 1.18)));
            tx = px - (px - tx) * next / zoom; ty = py - (py - ty) * next / zoom; zoom = next; apply();
        }, {passive: false});
        apply();
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
