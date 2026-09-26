#!/usr/bin/env python3
"""从 favicon.ico 生成控制台像素画脚本 static/js/console-art-v3.js。

用半格字符（▀ / ▄）在浏览器控制台重绘 favicon：
字符的 color 画上半像素、background-color 画下半像素，所以 1 个字符 = 2 像素高。
纯字符 + 颜色实现，不依赖 background-image，Chrome / Firefox / Safari 表现一致
（Firefox 的 DevTools 会把 background-image + padding 的图片盒子裁掉一半）。

用法：
    venv/bin/python scripts/gen_console_art.py
生成后记得把模板里的引用换成新文件名——Cloudflare 静态缓存 7 天且忽略查询串，
沿用旧文件名会导致改动刷不出来。
"""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / 'favicon.ico'
OUT = ROOT / 'static/js/console-art-v3.js'

# favicon.ico 内含 16/32/48/64 四帧，只有 64 帧的字形是原生渲染的（48 帧糊、32 帧文字只有 7 像素高）
SIZE = 64
PALETTE = {'b': '#209cee', 'w': '#ffffff'}
CELL_CSS = 'font-size:8px;line-height:8px;'

TEMPLATE = r'''/* 谷物 OJ 控制台像素画（字符版）
 * 本文件由 scripts/gen_console_art.py 从 favicon.ico 的 64×64 帧生成，请勿手改。
 * 半格字符：color 画上半像素、background-color 画下半像素，1 个字符 = 2 像素高。
 * 不用 background-image，Firefox 的 DevTools 不会裁切。 */
(function () {
    'use strict';

    if (window.__guwuConsoleArt) { return; }
    window.__guwuConsoleArt = true;

    // 调色板：favicon 主色蓝 + 白色「谷物 OJ」字样
    var PALETTE = {
        b: '__COLOR_B__',
        w: '__COLOR_W__'
    };

    // 每两项一组（上半像素行、下半像素行），'.' 表示透明（圆角与画布外）
    var GRID = [
__GRID__
    ];

    // 等宽字符宽约 0.6em，line-height 取 2 倍字号让像素接近正方形
    var CELL_CSS = '__CELL_CSS__';

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
'''


def quantize():
    """把指定尺寸的 favicon 帧量化成 '.'（透明）/ 'b'（蓝）/ 'w'（白）。"""
    im = Image.open(SRC)
    im.size = (SIZE, SIZE)
    px = im.convert('RGBA').load()

    grid = []
    for y in range(SIZE):
        row = []
        for x in range(SIZE):
            r, g, b, a = px[x, y]
            if a < 128:
                row.append('.')
            elif min(r, g, b) > 160:
                row.append('w')
            else:
                row.append('b')
        grid.append(''.join(row))
    return grid


def main():
    grid = quantize()
    rows = ',\n'.join("        '%s'" % row for row in grid)

    js = (TEMPLATE
          .replace('__GRID__', rows)
          .replace('__COLOR_B__', PALETTE['b'])
          .replace('__COLOR_W__', PALETTE['w'])
          .replace('__CELL_CSS__', CELL_CSS))

    OUT.write_text(js, encoding='utf-8')

    opaque = sum(row.count('b') + row.count('w') for row in grid)
    print('%s %dx%d -> %s' % (SRC.name, SIZE, SIZE, OUT.relative_to(ROOT)))
    print('  输出 %d 行像素 / %d 行字符，%d 个不透明像素，%d 字节'
          % (SIZE, SIZE // 2, opaque, len(js.encode('utf-8'))))


if __name__ == '__main__':
    main()
