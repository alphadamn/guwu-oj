(function () {
    'use strict';

    var prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    function initNavbarScroll() {
        var nav = document.querySelector('.oj-navbar');
        if (!nav) return;

        function onScroll() {
            nav.classList.toggle('scrolled', window.scrollY > 8);
        }

        window.addEventListener('scroll', onScroll, { passive: true });
        onScroll();
    }

    function initScrollAnimations() {
        if (prefersReducedMotion) {
            document.querySelectorAll('.animate-in').forEach(function (el) {
                el.classList.add('visible');
            });
            return;
        }

        var observer = new IntersectionObserver(
            function (entries) {
                entries.forEach(function (entry) {
                    if (entry.isIntersecting) {
                        entry.target.classList.add('visible');
                        observer.unobserve(entry.target);
                    }
                });
            },
            { threshold: 0.12, rootMargin: '0px 0px -40px 0px' }
        );

        document.querySelectorAll('.animate-in').forEach(function (el) {
            observer.observe(el);
        });
    }

    function animateCounter(el) {
        var target = parseInt(el.getAttribute('data-count'), 10) || 0;
        var duration = 1200;
        var start = 0;
        var startTime = null;

        function step(timestamp) {
            if (!startTime) startTime = timestamp;
            var progress = Math.min((timestamp - startTime) / duration, 1);
            var eased = 1 - Math.pow(1 - progress, 3);
            el.textContent = Math.floor(start + (target - start) * eased);
            if (progress < 1) {
                requestAnimationFrame(step);
            } else {
                el.textContent = target;
            }
        }

        if (prefersReducedMotion) {
            el.textContent = target;
            return;
        }

        requestAnimationFrame(step);
    }

    function initCounters() {
        var counters = document.querySelectorAll('[data-count]');
        if (!counters.length) return;

        if (prefersReducedMotion) {
            counters.forEach(function (el) {
                el.textContent = el.getAttribute('data-count');
            });
            return;
        }

        var observer = new IntersectionObserver(
            function (entries) {
                entries.forEach(function (entry) {
                    if (entry.isIntersecting) {
                        animateCounter(entry.target);
                        observer.unobserve(entry.target);
                    }
                });
            },
            { threshold: 0.5 }
        );

        counters.forEach(function (el) {
            observer.observe(el);
        });
    }

    function normalizePath(p) {
        if (!p || p === '/') return '/';
        return p.endsWith('/') ? p.slice(0, -1) : p;
    }

    function markActiveNav() {
        var path = normalizePath(window.location.pathname);
        document.querySelectorAll('.oj-navbar .nav-link').forEach(function (link) {
            var href = link.getAttribute('href');
            if (!href || href === '#') return;
            var linkPath = normalizePath(href.split('?')[0].split('#')[0]);
            if (path === linkPath) {
                link.classList.add('active');
            }
        });
    }

    function initThemeToggle() {
        var media = window.matchMedia('(prefers-color-scheme: dark)');

        function currentTheme() {
            return document.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'dark' : 'light';
        }

        function applyIcon(theme) {
            document.querySelectorAll('[data-theme-toggle]').forEach(function (btn) {
                var icon = btn.querySelector('i');
                if (icon) {
                    icon.classList.toggle('bi-moon-stars', theme === 'light');
                    icon.classList.toggle('bi-sun', theme === 'dark');
                }
                var label = theme === 'dark' ? '切换亮色模式' : '切换深色模式';
                btn.setAttribute('aria-label', label);
                btn.title = label;
            });
        }

        function setTheme(theme, persist) {
            document.documentElement.setAttribute('data-bs-theme', theme);
            if (persist) {
                try { localStorage.setItem('oj-theme', theme); } catch (e) { /* ignore */ }
            }
            applyIcon(theme);
        }

        applyIcon(currentTheme());

        document.querySelectorAll('[data-theme-toggle]').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var root = document.documentElement;
                var icon = btn.querySelector('i');
                // 加过渡类，让背景/文字/边框颜色平滑渐变；动画结束后移除
                root.classList.add('oj-theme-transition');
                if (icon) {
                    icon.classList.remove('oj-icon-swap');
                    void icon.offsetWidth; // 重启动画
                    icon.classList.add('oj-icon-swap');
                }
                setTheme(currentTheme() === 'dark' ? 'light' : 'dark', true);
                setTimeout(function () { root.classList.remove('oj-theme-transition'); }, 450);
            });
        });

        // 用户未手动选择过主题时，跟随系统亮暗变化
        var stored = null;
        try { stored = localStorage.getItem('oj-theme'); } catch (e) { /* ignore */ }
        if (!stored && media.addEventListener) {
            media.addEventListener('change', function (evt) {
                var saved = null;
                try { saved = localStorage.getItem('oj-theme'); } catch (e) { /* ignore */ }
                if (saved) return;
                setTheme(evt.matches ? 'dark' : 'light', false);
            });
        }
    }

    function initAll() {
        initNavbarScroll();
        initScrollAnimations();
        initCounters();
        markActiveNav();
        initThemeToggle();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initAll);
    } else {
        // 某些场景（如 Cloudflare Rocket Loader 延迟执行脚本）下
        // DOMContentLoaded 已经触发，监听不会再被调用。
        initAll();
    }
})();
