/* 谷物 OJ 控制台像素画（字符版）
 * 本文件由 scripts/gen_console_art.py 从 favicon.ico 的 64×64 帧生成，请勿手改。
 * 半格字符：color 画上半像素、background-color 画下半像素，1 个字符 = 2 像素高。
 * 不用 background-image，Firefox 的 DevTools 不会裁切。 */
(function () {
    'use strict';

    if (window.__guwuConsoleArt) { return; }
    window.__guwuConsoleArt = true;

    // 调色板：favicon 主色蓝 + 白色「谷物 OJ」字样
    var PALETTE = {
        b: '#209cee',
        w: '#ffffff'
    };

    // 每两项一组（上半像素行、下半像素行），'.' 表示透明（圆角与画布外）
    var GRID = [
        '....bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb....',
        '..bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb..',
        '.bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.',
        '.bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbwbbbbwbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbwwbbbbwwbbbbbbwwbwbbbbwbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbwwbbbbbbwwbbbbbwbbwbbbwwbbbbbbbbbbbbbwwwbbbbbbwbbbbwwbbbb',
        'bbbbbbwwbbbbwbbbwwwbbbwwwwwbbwwwwwwwwbbbbbwwwwwbbbbbbwwwwwwwbbbb',
        'bbbbbwwbbbbwwbbbbwwbbbwwwwwwwwbwwbwwwbbbbwwwbbbbwwbbbbbwwwwwbbbb',
        'bbbbbbbbbbwwwwbbbbbbbbwbbwbbwbbwbbwbwbbbwwwbbbbwwwwbbbbbbbwwbbbb',
        'bbbbbbbbbwwbbwwbbbbbbwwbbwbwwbbwbwwbwbbbwwbbbbbwwwwbbbbbbbwbbbbb',
        'bbbbbbbwwwbbbbwwwbbbbbbbbwbbbbwwbwbbwbbwwwbbbbbbwwwbbbbbbwwbbbbb',
        'bbbbbbwwwbbbbbbwwwbbbbbbwwwwbbwbbwbbwbbwwbbbbbbbbwwbbbbbbwwbbbbb',
        'bbbbwwwwwwwwwwwwwwwwbbwwwwwbbwwbwwbbwbbwwbbbbbbbbwbbbbbbbwwbbbbb',
        'bbbbbbbwwbbbbbbwwbbbbbwbwwbbwwbbwbbwwbbwwbbbbbbbbwbbbbbbbwwbbbbb',
        'bbbbbbbwwbbbbbbwwbbbbbbbbwbbwbbwwbbwwbbwwwbbbbbbwwbbbbbbbwwbbbbb',
        'bbbbbbbwwbbbbbbwwbbbbbbbbwbbbbbwbbbwwbbbwwbbbbbwwbbbwbbbwwbbbbbb',
        'bbbbbbbwwbbbbbbwwbbbbbbbbwbbbbwwbbbwwbbbbwwbbbwwbbbbwwbbwwbbbbbb',
        'bbbbbbbwwwwwwwwwwbbbbbbbbwbbbwwbbwwwbbbbbbwwwwwbbbbbwwwwbbbbbbbb',
        'bbbbbbbwwbbbbbbwwbbbbbbbbwbbbbbbbwwbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
        '.bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.',
        '.bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.',
        '..bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb..',
        '....bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb....'
    ];

    // 等宽字符宽约 0.6em，line-height 取 2 倍字号让像素接近正方形
    var CELL_CSS = 'font-size:8px;line-height:8px;';

    function colorOf(ch) {
        return PALETTE[ch] || 'transparent';
    }

    // 把上下两行像素合成一行半格字符，再把相邻同色像素合并成一个 %c 段以降低噪声
    function buildLine(top, bottom) {
        var width = Math.max(top.length, bottom.length);
        var chars = [];
        var x, t, b, ch, fg, bg;

        for (x = 0; x < width; x++) {
            t = top.charAt(x) || '.';
            b = bottom.charAt(x) || '.';

            if (t === '.' && b === '.') {
                ch = ' ';
                fg = 'transparent';
                bg = 'transparent';
            } else if (t !== '.' && b === '.') {
                ch = '\u2580';
                fg = colorOf(t);
                bg = 'transparent';
            } else if (t === '.' && b !== '.') {
                ch = '\u2584';
                fg = colorOf(b);
                bg = 'transparent';
            } else {
                ch = '\u2580';
                fg = colorOf(t);
                bg = colorOf(b);
            }
            chars.push({ ch: ch, fg: fg, bg: bg });
        }

        // 去掉行尾空白像素
        while (chars.length && chars[chars.length - 1].ch === ' ') {
            chars.pop();
        }

        var format = '';
        var styles = [];
        var run = '';
        var runFg = null;
        var runBg = null;

        function flush() {
            if (!run) { return; }
            format += '%c' + run;
            styles.push('color:' + runFg + ';background-color:' + runBg + ';' + CELL_CSS);
            run = '';
        }

        for (x = 0; x < chars.length; x++) {
            if (chars[x].fg !== runFg || chars[x].bg !== runBg) {
                flush();
                runFg = chars[x].fg;
                runBg = chars[x].bg;
            }
            run += chars[x].ch;
        }
        flush();

        return { format: format, styles: styles };
    }

    try {
        var y, line, args;
        for (y = 0; y < GRID.length; y += 2) {
            line = buildLine(GRID[y] || '', GRID[y + 1] || '');
            if (!line.format) { continue; }
            args = [line.format].concat(line.styles);
            console.log.apply(console, args);
        }

        console.log(
            '%c谷物 OJ%c 让每一行代码都发光 · https://guwu.camluni.cn',
            'font-size:16px;font-weight:700;color:#209cee;',
            'font-size:12px;color:#7cc7f5;'
        );
    } catch (e) {
        /* 控制台装饰不应影响页面运行 */
    }
})();
