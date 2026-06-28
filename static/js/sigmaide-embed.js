/**
 * SigmaIDE iframe embed on problem detail page.
 */
(function (global) {
    const STARTERS = {
        Python: `# Python 3\n\n`,
        'C++': `#include <iostream>\nusing namespace std;\n\nint main() {\n\n    return 0;\n}\n`,
        C: `#include <stdio.h>\n\nint main(void) {\n    return 0;\n}\n`,
    };

    function initSigmaIdeEmbed(config) {
        const panel = document.getElementById(config.panelId);
        const iframe = document.getElementById(config.iframeId);
        const langSelect = document.getElementById(config.languageSelectId);
        const btnFullscreen = document.getElementById(config.fullscreenBtnId);
        const btnExitFullscreen = document.getElementById(config.exitFullscreenBtnId);

        if (!panel || !iframe) return;

        let ready = false;

        function postToIframe(message) {
            if (!iframe.contentWindow) return;
            iframe.contentWindow.postMessage(message, '*');
        }

        function sendConfig(lang, withStarter) {
            const payload = { type: 'sigmaide:config', lang: lang };
            if (withStarter && STARTERS[lang]) {
                payload.starter = STARTERS[lang];
            }
            postToIframe(payload);
        }

        function enterFullscreen() {
            panel.classList.add('sigmaide-fullscreen');
            document.body.classList.add('sigmaide-fullscreen-active');
            if (btnFullscreen) btnFullscreen.classList.add('d-none');
            if (btnExitFullscreen) btnExitFullscreen.classList.remove('d-none');
        }

        function exitFullscreen() {
            panel.classList.remove('sigmaide-fullscreen');
            document.body.classList.remove('sigmaide-fullscreen-active');
            if (btnFullscreen) btnFullscreen.classList.remove('d-none');
            if (btnExitFullscreen) btnExitFullscreen.classList.add('d-none');
        }

        window.addEventListener('message', function (event) {
            if (event.source !== iframe.contentWindow) return;
            const data = event.data;
            if (!data || typeof data.type !== 'string') return;

            if (data.type === 'sigmaide:ready') {
                ready = true;
                if (langSelect) {
                    sendConfig(langSelect.value, true);
                }
            }
        });

        if (langSelect) {
            langSelect.addEventListener('change', function () {
                if (ready) {
                    sendConfig(langSelect.value, true);
                }
            });
        }

        if (btnFullscreen) {
            btnFullscreen.addEventListener('click', enterFullscreen);
        }

        if (btnExitFullscreen) {
            btnExitFullscreen.addEventListener('click', exitFullscreen);
        }

        document.addEventListener('keydown', function (event) {
            if (event.key === 'Escape' && panel.classList.contains('sigmaide-fullscreen')) {
                exitFullscreen();
            }
        });
    }

    global.initSigmaIdeEmbed = initSigmaIdeEmbed;
})(window);
