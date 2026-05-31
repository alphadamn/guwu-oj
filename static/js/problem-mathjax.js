/**
 * MathJax 3 for problem statement LaTeX (Luogu-style $$ ... $$).
 */
(function () {
    window.MathJax = {
        tex: {
            inlineMath: [['$', '$'], ['\\(', '\\)']],
            displayMath: [['$$', '$$'], ['\\[', '\\]']],
            processEscapes: true,
            processEnvironments: true,
            packages: { '[+]': ['ams', 'newcommand', 'configmacros', 'textmacros', 'boldsymbol'] },
            tags: 'ams',
        },
        options: {
            skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code'],
            renderActions: {
                addMenu: [0, '', ''],
            },
        },
        startup: {
            pageReady: function () {
                return MathJax.startup.defaultPageReady().then(function () {
                    var nodes = document.querySelectorAll('.problem-markdown');
                    if (nodes.length) {
                        return MathJax.typesetPromise(nodes);
                    }
                });
            },
        },
    };
})();
