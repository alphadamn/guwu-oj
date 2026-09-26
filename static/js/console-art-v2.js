/* 谷物 OJ 控制台像素画
 * 把 favicon 经 canvas 重新编码后，用 background-image + padding 的方式打到大尺寸打印到控制台。
 * 图片以 64×64 的 base64 data URI 内嵌，因此没有跨域限制、也不产生额外网络请求。 */
(function () {
    'use strict';

    if (window.__guwuConsoleArt) { return; }
    window.__guwuConsoleArt = true;

    var DATA_URI = 'data:image/png;base64,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';
    var SCALE = 1;

    // 与 https://github.com/... 流传的 console.image 同一思路：
    // padding 撑开元素尺寸，背景图按 background-size 铺满，文字色透明只留图。
    function consoleImage(url, scale) {
        var img = new Image();
        img.onload = function () {
            var c = document.createElement('canvas');
            var ctx = c.getContext('2d');
            if (!ctx) { return; }
            c.width = img.width;
            c.height = img.height;
            // 不填充底色，保留 favicon 圆角处的透明像素
            ctx.drawImage(img, 0, 0);
            var dataUri = c.toDataURL('image/png');
            console.log(
                '%c ',
                'font-size:1px;' +
                'padding:' + Math.floor(img.height * scale / 2) + 'px ' + Math.floor(img.width * scale / 2) + 'px;' +
                'background-image:url(' + dataUri + ');' +
                'background-repeat:no-repeat;' +
                'background-size:' + (img.width * scale) + 'px ' + (img.height * scale) + 'px;' +
                'color:transparent;'
            );
            console.log(
                '%c谷物 OJ%c 让每一行代码都发光 · https://guwu.camluni.cn',
                'font-size:16px;font-weight:700;color:#209cee;',
                'font-size:12px;color:#7cc7f5;'
            );
        };
        img.src = url;
    }

    try {
        consoleImage(DATA_URI, SCALE);
    } catch (e) {
        /* 控制台装饰不应影响页面运行 */
    }
})();
