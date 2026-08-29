(function () {
    'use strict';

    var MAX_COUNTER = 5000;      // safety ceiling; real counters are ~10..50
    var WORKER_URL = '/static/js/altcha-worker.js?v=2';
    var SOLVE_TIMEOUT = 30000;    // ms

    function hexToBytes(hex) {
        var out = new Uint8Array(hex.length / 2);
        for (var i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
        return out;
    }

    function makePassword(nonceHex, counter) {
        var nonce = hexToBytes(nonceHex);
        var pw = new Uint8Array(nonce.length + 4);
        pw.set(nonce, 0);
        new DataView(pw.buffer, pw.byteOffset, pw.byteLength).setUint32(nonce.length, counter >>> 0, false);
        return pw;
    }

    function pbkdf2Hex(params, passwordBytes) {
        var hash = String(params.algorithm || 'PBKDF2/SHA-512').split('/').pop() || 'SHA-512';
        return crypto.subtle.importKey('raw', passwordBytes, 'PBKDF2', false, ['deriveBits'])
            .then(function (key) {
                return crypto.subtle.deriveBits(
                    {
                        name: 'PBKDF2',
                        salt: hexToBytes(params.salt),
                        iterations: params.cost,
                        hash: hash,
                    },
                    key,
                    (params.keyLength || 32) * 8
                );
            })
            .then(function (bits) {
                return Array.prototype.map.call(new Uint8Array(bits), function (b) {
                    return ('0' + b.toString(16)).slice(-2);
                }).join('');
            });
    }

    // Main-thread fallback (used only when Web Workers are unavailable).
    function solveOnMainThread(challenge) {
        var params = challenge.parameters;
        var prefix = params.keyPrefix || '00';
        function attempt(counter) {
            if (counter > MAX_COUNTER) return Promise.reject(new Error('no solution found'));
            return pbkdf2Hex(params, makePassword(params.nonce, counter)).then(function (hex) {
                if (hex.slice(0, prefix.length) === prefix) return { counter: counter, derivedKey: hex };
                return attempt(counter + 1);
            });
        }
        return attempt(0);
    }

    // Off-main-thread solve via a dedicated worker (keeps the UI smooth).
    function solveInWorker(challenge) {
        return new Promise(function (resolve, reject) {
            var worker;
            try {
                worker = new Worker(WORKER_URL);
            } catch (err) {
                reject(err);
                return;
            }
            var settled = false;
            var timer = setTimeout(function () { finish(null, new Error('solve timeout')); }, SOLVE_TIMEOUT);
            function finish(sol, err) {
                if (settled) return;
                settled = true;
                clearTimeout(timer);
                worker.terminate();
                if (err) reject(err); else resolve(sol);
            }
            worker.onmessage = function (e) {
                var msg = e.data || {};
                if (msg.type === 'solution') finish(msg, null);
                else if (msg.type === 'error') finish(null, new Error(msg.error || 'solve failed'));
            };
            worker.onerror = function () { finish(null, new Error('worker error')); };
            worker.postMessage({ type: 'solve', parameters: challenge.parameters, start: 0, max: MAX_COUNTER });
        });
    }

    function solveChallenge(challenge) {
        var promise = (typeof Worker !== 'undefined')
            ? solveInWorker(challenge)
            : Promise.reject(new Error('workers unavailable'));
        return promise.catch(function () {
            console.warn('[ALTCHA] worker solve failed; falling back to main thread');
            return solveOnMainThread(challenge);
        }).then(function (sol) {
            return btoa(JSON.stringify({
                challenge: challenge,
                solution: { counter: sol.counter, derivedKey: sol.derivedKey }
            }));
        });
    }

    function solve() {
        return fetch('/users/captcha/altcha/', { credentials: 'same-origin', cache: 'no-store' })
            .then(function (r) { if (!r.ok) throw new Error('challenge unavailable'); return r.json(); })
            .then(function (ch) {
                console.log('[ALTCHA] received challenge', {
                    algorithm: ch.parameters && ch.parameters.algorithm,
                    cost: ch.parameters && ch.parameters.cost,
                });
                return solveChallenge(ch);
            });
    }

    function bindForm(form) {
        var field = form.querySelector('input[name="altcha"]');
        if (!field || form.__ojAltchaBound) return;
        form.__ojAltchaBound = true;

        var solved = false;
        var solving = false;

        // Pre-solve in the background (off-thread via the worker) while the
        // user fills the form, so submissions usually resolve instantly.
        var promise = solve().then(function (payload) {
            field.value = payload;
            solved = true;
            console.log('[ALTCHA] proof-of-work solved; payload ready (length=' + payload.length + ')');
            return payload;
        }).catch(function (err) {
            console.warn('[ALTCHA] failed:', err);
        });

        form.addEventListener('submit', function (e) {
            if (solved) return;
            e.preventDefault();
            e.stopPropagation();
            if (solving) return;
            solving = true;
            promise.then(function () {
                solving = false;
                solved = true;
                if (form.requestSubmit) form.requestSubmit();
                else form.submit();
            }).catch(function () {
                solving = false;
                // Submit anyway — the server will reject gracefully.
                form.submit();
            });
        });
    }

    function initForms() {
        document.querySelectorAll('form').forEach(function (form) {
            if (form.querySelector('input[name="altcha"]')) bindForm(form);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initForms);
    } else {
        initForms();
    }

    window.OJAltcha = { solve: solve, bindForm: bindForm };
})();
