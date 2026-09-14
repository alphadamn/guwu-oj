// Extracted from templates/admin/index.html inline <script> (2026-09-13).
// 【重要】在 simpleui 的 index.js 初始化 Vue 之前先清理 sessionStorage 里的 tab 缓存。
// 否则即使菜单配置正确，Vue 启动时会从 sessionStorage 读回旧的 tab（旧的 url），
// 点击"评测机"仍然会打开"服务组件"的 iframe。
(function () {
    try {
        var known = {};
        var scan = function (items) {
            if (!items) return;
            for (var i = 0; i < items.length; i++) {
                var it = items[i];
                if (it && it.url) known[String(it.eid)] = String(it.url);
                if (it && it.models) scan(it.models);
            }
        };
        if (typeof menus !== 'undefined') scan(menus);

        if (window.sessionStorage) {
            var raw = sessionStorage.getItem('tabs');
            if (raw) {
                try {
                    var tabs = JSON.parse(raw);
                    var changed = false;
                    if (Array.isArray(tabs) && tabs.length) {
                        for (var j = tabs.length - 1; j >= 0; j--) {
                            var t = tabs[j];
                            if (!t) { tabs.splice(j, 1); changed = true; continue; }
                            if (t.eid === '1' || t.eid === 1 || t.id === '0' || t.id === 0) continue;
                            var eidKey = String(t.eid);
                            if (!known[eidKey] || known[eidKey] !== String(t.url)) {
                                tabs.splice(j, 1);
                                changed = true;
                            }
                        }
                        if (changed) sessionStorage.setItem('tabs', JSON.stringify(tabs));
                    }
                } catch (e) {
                    sessionStorage.removeItem('tabs');
                }
            }
        }
    } catch (e) { /* ignore storage errors */ }

    // 保险措施：当 window.app 出现后，patch 它的 openTab，
    // 使得复用 eid 相同的 tab 时，如果 url/名称不同，强制覆盖为菜单新的数据。
    var _tries = 0;
    var _pid = setInterval(function () {
        _tries++;
        if (_tries > 40) { clearInterval(_pid); return; }
        try {
            var app = window.app;
            if (app && typeof app.openTab === 'function' && !app.__openTabPatched) {
                var orig = app.openTab;
                app.__openTabPatched = true;
                app.openTab = function (data, index, selected, loading) {
                    if (data && data.eid && data.url && Array.isArray(this.tabs)) {
                        for (var i = 0; i < this.tabs.length; i++) {
                            var t = this.tabs[i];
                            if (t && String(t.eid) === String(data.eid)) {
                                if (String(t.url) !== String(data.url)) {
                                    t.url = data.url;
                                    if (data.name) t.name = data.name;
                                    if (data.icon) t.icon = data.icon;
                                }
                            }
                        }
                    }
                    return orig.apply(this, arguments);
                };
                clearInterval(_pid);
            }
        } catch (e) { /* ignore */ }
    }, 50);
})();
