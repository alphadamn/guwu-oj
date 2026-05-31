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

    document.addEventListener('DOMContentLoaded', function () {
        initNavbarScroll();
        initScrollAnimations();
        initCounters();
        markActiveNav();
    });
})();
