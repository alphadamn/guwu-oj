/* ALTCHA v2 PBKDF2/SHA-512 proof-of-work solver — runs off the main thread. */
'use strict';

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

self.onmessage = function (e) {
    var msg = e.data || {};
    if (msg.type !== 'solve') return;

    var params = msg.parameters || {};
    var prefix = params.keyPrefix || '00';
    var start = msg.start || 0;
    var max = msg.max || 5000;
    var counter = start;

    function attempt() {
        if (counter > max) {
            self.postMessage({ type: 'error', error: 'no solution found' });
            return;
        }
        pbkdf2Hex(params, makePassword(params.nonce, counter)).then(function (hex) {
            if (hex.slice(0, prefix.length) === prefix) {
                self.postMessage({ type: 'solution', counter: counter, derivedKey: hex });
            } else {
                counter += 1;
                attempt();
            }
        }).catch(function (err) {
            self.postMessage({ type: 'error', error: String((err && err.message) || err) });
        });
    }

    attempt();
};
