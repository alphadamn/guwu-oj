/*global Calendar, findPosX, findPosY, get_format, gettext, gettext_noop, interpolate, ngettext, quickElement*/
// Inserts shortcut buttons after all of the following:
//     <input type="text" class="vDateField">
//     <input type="text" class="vTimeField">
'use strict';
{
    const DateTimeShortcuts = {
        calendars: [],
        calendarInputs: [],
        clockInputs: [],
        clockHours: {
            default_: [
                [gettext_noop('Now'), -1],
                [gettext_noop('Midnight'), 0],
                [gettext_noop('6 a.m.'), 6],
                [gettext_noop('Noon'), 12],
                [gettext_noop('6 p.m.'), 18]
            ]
        },
        dismissClockFunc: [],
        dismissCalendarFunc: [],
        calendarDivName1: 'calendarbox', // name of calendar <div> that gets toggled
        calendarDivName2: 'calendarin', // name of <div> that contains calendar
        calendarLinkName: 'calendarlink', // name of the link that is used to toggle
        clockDivName: 'clockbox', // name of clock <div> that gets toggled
        clockLinkName: 'clocklink', // name of the link that is used to toggle
        shortCutsClass: 'datetimeshortcuts', // class of the clock and cal shortcuts
        timezoneWarningClass: 'timezonewarning', // class of the warning for timezone mismatch
        timezoneOffset: 0,
        init: function() {
            const serverOffset = document.body.dataset.adminUtcOffset;
            if (serverOffset) {
                const localOffset = new Date().getTimezoneOffset() * -60;
                DateTimeShortcuts.timezoneOffset = localOffset - serverOffset;
            }

            for (const inp of document.getElementsByTagName('input')) {
                if (inp.type === 'text' && inp.classList.contains('vTimeField')) {
                    DateTimeShortcuts.addClock(inp);
                    DateTimeShortcuts.addTimezoneWarning(inp);
                }
                else if (inp.type === 'text' && inp.classList.contains('vDateField')) {
                    DateTimeShortcuts.addCalendar(inp);
                    DateTimeShortcuts.addTimezoneWarning(inp);
                }
            }
        },
        // Return the current time while accounting for the server timezone.
        now: function() {
            const serverOffset = document.body.dataset.adminUtcOffset;
            if (serverOffset) {
                const localNow = new Date();
                const localOffset = localNow.getTimezoneOffset() * -60;
                localNow.setTime(localNow.getTime() + 1000 * (serverOffset - localOffset));
                return localNow;
            } else {
                return new Date();
            }
        },
        // Add a warning when the time zone in the browser and backend do not match.
        addTimezoneWarning: function(inp) {
            const warningClass = DateTimeShortcuts.timezoneWarningClass;
            let timezoneOffset = DateTimeShortcuts.timezoneOffset / 3600;

            // Only warn if there is a time zone mismatch.
            if (!timezoneOffset) {
                return;
            }

            // Check if warning is already there.
            if (inp.parentNode.querySelectorAll('.' + warningClass).length) {
                return;
            }

            let message;
            if (timezoneOffset > 0) {
                message = ngettext(
                    'Note: You are %s hour ahead of server time.',
                    'Note: You are %s hours ahead of server time.',
                    timezoneOffset
                );
            }
            else {
                timezoneOffset *= -1;
                message = ngettext(
                    'Note: You are %s hour behind server time.',
                    'Note: You are %s hours behind server time.',
                    timezoneOffset
                );
            }
            message = interpolate(message, [timezoneOffset]);

            const warning = document.createElement('div');
            warning.classList.add('help', warningClass);
            warning.textContent = message;
            inp.parentNode.appendChild(warning);
        },
        // Add clock widget to a given field
        addClock: function(inp) {
            const num = DateTimeShortcuts.clockInputs.length;
            DateTimeShortcuts.clockInputs[num] = inp;
            DateTimeShortcuts.dismissClockFunc[num] = function() { DateTimeShortcuts.dismissClock(num); return true; };

            // Shortcut links (clock icon and "Now" link)
            const shortcuts_span = document.createElement('span');
            shortcuts_span.className = DateTimeShortcuts.shortCutsClass;
            inp.parentNode.insertBefore(shortcuts_span, inp.nextSibling);
            const now_link = document.createElement('a');
            now_link.href = "#";
            now_link.textContent = gettext('Now');
            now_link.addEventListener('click', function(e) {
                e.preventDefault();
                DateTimeShortcuts.handleClockQuicklink(num, -1);
            });
            const clock_link = document.createElement('a');
            clock_link.href = '#';
            clock_link.id = DateTimeShortcuts.clockLinkName + num;
            clock_link.addEventListener('click', function(e) {
                e.preventDefault();
                // avoid triggering the document click handler to dismiss the clock
                e.stopPropagation();
                DateTimeShortcuts.openClock(num);
            });

            quickElement(
                'span', clock_link, '',
                'class', 'clock-icon',
                'title', gettext('Choose a Time')
            );
            shortcuts_span.appendChild(document.createTextNode('\u00A0'));
            shortcuts_span.appendChild(now_link);
            shortcuts_span.appendChild(document.createTextNode('\u00A0|\u00A0'));
            shortcuts_span.appendChild(clock_link);

            // Create clock link div
            //
            // Markup looks like:
            // <div id="clockbox1" class="clockbox module">
            //     <h2>Choose a time</h2>
            //     <ul class="timelist">
            //         <li><a href="#">Now</a></li>
            //         <li><a href="#">Midnight</a></li>
            //         <li><a href="#">6 a.m.</a></li>
            //         <li><a href="#">Noon</a></li>
            //         <li><a href="#">6 p.m.</a></li>
            //     </ul>
            //     <p class="calendar-cancel"><a href="#">Cancel</a></p>
            // </div>

            const clock_box = document.createElement('div');
            clock_box.style.display = 'none';
            clock_box.style.position = 'absolute';
            clock_box.className = 'clockbox module';
            clock_box.id = DateTimeShortcuts.clockDivName + num;
            document.body.appendChild(clock_box);
            clock_box.addEventListener('click', function(e) { e.stopPropagation(); });

            quickElement('h2', clock_box, gettext('Choose a time'));
            const time_list = quickElement('ul', clock_box);
            time_list.className = 'timelist';
            // The list of choices can be overridden in JavaScript like this:
            // DateTimeShortcuts.clockHours.name = [['3 a.m.', 3]];
            // where name is the name attribute of the <input>.
            const name = typeof DateTimeShortcuts.clockHours[inp.name] === 'undefined' ? 'default_' : inp.name;
            DateTimeShortcuts.clockHours[name].forEach(function(element) {
                const time_link = quickElement('a', quickElement('li', time_list), gettext(element[0]), 'href', '#');
                time_link.addEventListener('click', function(e) {
                    e.preventDefault();
                    DateTimeShortcuts.handleClockQuicklink(num, element[1]);
                });
            });

            const cancel_p = quickElement('p', clock_box);
            cancel_p.className = 'calendar-cancel';
            const cancel_link = quickElement('a', cancel_p, gettext('Cancel'), 'href', '#');
            cancel_link.addEventListener('click', function(e) {
                e.preventDefault();
                DateTimeShortcuts.dismissClock(num);
            });

            document.addEventListener('keyup', function(event) {
                if (event.which === 27) {
                    // ESC key closes popup
                    DateTimeShortcuts.dismissClock(num);
                    event.preventDefault();
                }
            });
        },
        openClock: function(num) {
            const clock_box = document.getElementById(DateTimeShortcuts.clockDivName + num);
            const clock_link = document.getElementById(DateTimeShortcuts.clockLinkName + num);

            // Recalculate the clockbox position
            // is it left-to-right or right-to-left layout ?
            if (window.getComputedStyle(document.body).direction !== 'rtl') {
                clock_box.style.left = findPosX(clock_link) + 17 + 'px';
            }
            else {
                // since style's width is in em, it'd be tough to calculate
                // px value of it. let's use an estimated px for now
                clock_box.style.left = findPosX(clock_link) - 110 + 'px';
            }
            clock_box.style.top = Math.max(0, findPosY(clock_link) - 30) + 'px';

            // Show the clock box
            clock_box.style.display = 'block';
            document.addEventListener('click', DateTimeShortcuts.dismissClockFunc[num]);
        },
        dismissClock: function(num) {
            document.getElementById(DateTimeShortcuts.clockDivName + num).style.display = 'none';
            document.removeEventListener('click', DateTimeShortcuts.dismissClockFunc[num]);
        },
        handleClockQuicklink: function(num, val) {
            let d;
            if (val === -1) {
                d = DateTimeShortcuts.now();
            }
            else {
                d = new Date(1970, 1, 1, val, 0, 0, 0);
            }
            DateTimeShortcuts.clockInputs[num].value = d.strftime(get_format('TIME_INPUT_FORMATS')[0]);
            DateTimeShortcuts.clockInputs[num].focus();
            DateTimeShortcuts.dismissClock(num);
        },
        // Add calendar widget to a given field.
        addCalendar: function(inp) {
            const num = DateTimeShortcuts.calendars.length;

            DateTimeShortcuts.calendarInputs[num] = inp;
            DateTimeShortcuts.dismissCalendarFunc[num] = function() { DateTimeShortcuts.dismissCalendar(num); return true; };

            // Shortcut links (calendar icon and "Today" link)
            const shortcuts_span = document.createElement('span');
            shortcuts_span.className = DateTimeShortcuts.shortCutsClass;
            inp.parentNode.insertBefore(shortcuts_span, inp.nextSibling);
            const today_link = document.createElement('a');
            today_link.href = '#';
            today_link.appendChild(document.createTextNode(gettext('Today')));
            today_link.addEventListener('click', function(e) {
                e.preventDefault();
                DateTimeShortcuts.handleCalendarQuickLink(num, 0);
            });
            const cal_link = document.createElement('a');
            cal_link.href = '#';
            cal_link.id = DateTimeShortcuts.calendarLinkName + num;
            cal_link.addEventListener('click', function(e) {
                e.preventDefault();
                // avoid triggering the document click handler to dismiss the calendar
                e.stopPropagation();
                DateTimeShortcuts.openCalendar(num);
            });
            quickElement(
                'span', cal_link, '',
                'class', 'date-icon',
                'title', gettext('Choose a Date')
            );
            shortcuts_span.appendChild(document.createTextNode('\u00A0'));
            shortcuts_span.appendChild(today_link);
            shortcuts_span.appendChild(document.createTextNode('\u00A0|\u00A0'));
            shortcuts_span.appendChild(cal_link);

            // Create calendarbox div.
            //
            // Markup looks like:
            //
            // <div id="calendarbox3" class="calendarbox module">
            //     <h2>
            //           <a href="#" class="link-previous">&lsaquo;</a>
            //           <a href="#" class="link-next">&rsaquo;</a> February 2003
            //     </h2>
            //     <div class="calendar" id="calendarin3">
            //         <!-- (cal) -->
            //     </div>
            //     <div class="calendar-shortcuts">
            //          <a href="#">Yesterday</a> | <a href="#">Today</a> | <a href="#">Tomorrow</a>
            //     </div>
            //     <p class="calendar-cancel"><a href="#">Cancel</a></p>
            // </div>
            const cal_box = document.createElement('div');
            cal_box.style.display = 'none';
            cal_box.style.position = 'absolute';
            cal_box.className = 'calendarbox module';
            cal_box.id = DateTimeShortcuts.calendarDivName1 + num;
            document.body.appendChild(cal_box);
            cal_box.addEventListener('click', function(e) { e.stopPropagation(); });

            // next-prev links
            const cal_nav = quickElement('div', cal_box);
            const cal_nav_prev = quickElement('a', cal_nav, '<', 'href', '#');
            cal_nav_prev.className = 'calendarnav-previous';
            cal_nav_prev.addEventListener('click', function(e) {
                e.preventDefault();
                DateTimeShortcuts.drawPrev(num);
            });

            const cal_nav_next = quickElement('a', cal_nav, '>', 'href', '#');
            cal_nav_next.className = 'calendarnav-next';
            cal_nav_next.addEventListener('click', function(e) {
                e.preventDefault();
                DateTimeShortcuts.drawNext(num);
            });

            // main box
            const cal_main = quickElement('div', cal_box, '', 'id', DateTimeShortcuts.calendarDivName2 + num);
            cal_main.className = 'calendar';
            DateTimeShortcuts.calendars[num] = new Calendar(DateTimeShortcuts.calendarDivName2 + num, DateTimeShortcuts.handleCalendarCallback(num));
            DateTimeShortcuts.calendars[num].drawCurrent();

            // calendar shortcuts
            const shortcuts = quickElement('div', cal_box);
            shortcuts.className = 'calendar-shortcuts';
            let day_link = quickElement('a', shortcuts, gettext('Yesterday'), 'href', '#');
            day_link.addEventListener('click', function(e) {
                e.preventDefault();
                DateTimeShortcuts.handleCalendarQuickLink(num, -1);
            });
            shortcuts.appendChild(document.createTextNode('\u00A0|\u00A0'));
            day_link = quickElement('a', shortcuts, gettext('Today'), 'href', '#');
            day_link.addEventListener('click', function(e) {
                e.preventDefault();
                DateTimeShortcuts.handleCalendarQuickLink(num, 0);
            });
            shortcuts.appendChild(document.createTextNode('\u00A0|\u00A0'));
            day_link = quickElement('a', shortcuts, gettext('Tomorrow'), 'href', '#');
            day_link.addEventListener('click', function(e) {
                e.preventDefault();
                DateTimeShortcuts.handleCalendarQuickLink(num, +1);
            });

            // cancel bar
            const cancel_p = quickElement('p', cal_box);
            cancel_p.className = 'calendar-cancel';
            const cancel_link = quickElement('a', cancel_p, gettext('Cancel'), 'href', '#');
            cancel_link.addEventListener('click', function(e) {
                e.preventDefault();
                DateTimeShortcuts.dismissCalendar(num);
            });
            document.addEventListener('keyup', function(event) {
                if (event.which === 27) {
                    // ESC key closes popup
                    DateTimeShortcuts.dismissCalendar(num);
                    event.preventDefault();
                }
            });
        },
        openCalendar: function(num) {
            const cal_box = document.getElementById(DateTimeShortcuts.calendarDivName1 + num);
            const cal_link = document.getElementById(DateTimeShortcuts.calendarLinkName + num);
            const inp = DateTimeShortcuts.calendarInputs[num];

            // Determine if the current value in the input has a valid date.
            // If so, draw the calendar with that date's year and month.
            if (inp.value) {
                const format = get_format('DATE_INPUT_FORMATS')[0];
                const selected = inp.value.strptime(format);
                const year = selected.getUTCFullYear();
                const month = selected.getUTCMonth() + 1;
                const re = /\d{4}/;
                if (re.test(year.toString()) && month >= 1 && month <= 12) {
                    DateTimeShortcuts.calendars[num].drawDate(month, year, selected);
                }
            }

            // Recalculate the clockbox position
            // is it left-to-right or right-to-left layout ?
            if (window.getComputedStyle(document.body).direction !== 'rtl') {
                cal_box.style.left = findPosX(cal_link) + 17 + 'px';
            }
            else {
                // since style's width is in em, it'd be tough to calculate
                // px value of it. let's use an estimated px for now
                cal_box.style.left = findPosX(cal_link) - 180 + 'px';
            }
            cal_box.style.top = Math.max(0, findPosY(cal_link) - 75) + 'px';

            cal_box.style.display = 'block';
            document.addEventListener('click', DateTimeShortcuts.dismissCalendarFunc[num]);
        },
        dismissCalendar: function(num) {
            document.getElementById(DateTimeShortcuts.calendarDivName1 + num).style.display = 'none';
            document.removeEventListener('click', DateTimeShortcuts.dismissCalendarFunc[num]);
        },
        drawPrev: function(num) {
            DateTimeShortcuts.calendars[num].drawPreviousMonth();
        },
        drawNext: function(num) {
            DateTimeShortcuts.calendars[num].drawNextMonth();
        },
        handleCalendarCallback: function(num) {
            const format = get_format('DATE_INPUT_FORMATS')[0];
            return function(y, m, d) {
                DateTimeShortcuts.calendarInputs[num].value = new Date(y, m - 1, d).strftime(format);
                DateTimeShortcuts.calendarInputs[num].focus();
                document.getElementById(DateTimeShortcuts.calendarDivName1 + num).style.display = 'none';
            };
        },
        handleCalendarQuickLink: function(num, offset) {
            const d = DateTimeShortcuts.now();
            d.setDate(d.getDate() + offset);
            DateTimeShortcuts.calendarInputs[num].value = d.strftime(get_format('DATE_INPUT_FORMATS')[0]);
            DateTimeShortcuts.calendarInputs[num].focus();
            DateTimeShortcuts.dismissCalendar(num);
        }
    };

    window.addEventListener('load', DateTimeShortcuts.init);
    window.DateTimeShortcuts = DateTimeShortcuts;
}

// ============================================================
// Guwu: 日历/时钟弹窗定位重写（SimpleUI iframe 版）
// 思路：
//   1) 在 DOMContentLoaded 后立刻覆盖 window.DateTimeShortcuts.openCalendar / openClock
//      使用 getBoundingClientRect 计算 link 的视口坐标，按视口坐标绝对定位弹窗。
//   2) 再用 MutationObserver 兜底：页面上出现任何 #calendarboxN / #clockboxN 时，
//      立刻把它挂到顶层宿主并按视口坐标重新定位。
//   3) 顶层宿主 #__guwu_popup_root 是一个 position:fixed、无 padding 的 body 直连元素，
//      它的 transform:none 保证不会被 SimpleUI 的任何 transform 祖先捕获。
// ============================================================
(function () {
    'use strict';

    function getPopupRoot() {
        var root = document.getElementById('__guwu_popup_root');
        if (!root) {
            root = document.createElement('div');
            root.id = '__guwu_popup_root';
            var s = root.style;
            s.position = 'fixed';
            s.top = '0px';
            s.left = '0px';
            s.width = '0px';
            s.height = '0px';
            s.overflow = 'visible';
            s.zIndex = '2147483000';
            s.pointerEvents = 'none';
            s.transform = 'none';
            s.webkitTransform = 'none';
            s.willChange = 'auto';
            s.contain = 'none';
            // 附加到 body 最前面，使祖先链只有 <html>/<body>
            if (document.body && document.body.firstChild) {
                document.body.insertBefore(root, document.body.firstChild);
            } else if (document.body) {
                document.body.appendChild(root);
            }
        }
        return root;
    }

    function vpSize() {
        var d = document.documentElement;
        return {
            width: window.innerWidth || d.clientWidth || (document.body && document.body.clientWidth) || 800,
            height: window.innerHeight || d.clientHeight || (document.body && document.body.clientHeight) || 600
        };
    }

    function findLinkForBox(box) {
        // 通过 box 的 id 推断 link id
        if (!box || !box.id) return null;
        var m = box.id.match(/^(calendar|clock)(?:box)?(\d+)$/i);
        if (!m) return null;
        var kind = m[1].toLowerCase();
        var idx = m[2];
        if (kind === 'calendar') {
            return document.getElementById('calendarlink' + idx);
        } else {
            return document.getElementById('clocklink' + idx);
        }
    }

    function placePopup(box, link, xOffset, defaultTopOffset) {
        if (!box) return;
        if (!link) link = findLinkForBox(box);
        if (!link) return;

        var root = getPopupRoot();
        if (box.parentNode !== root) {
            try { root.appendChild(box); } catch (e) {}
        }
        box.style.position = 'absolute';
        box.style.transform = 'none';
        box.style.webkitTransform = 'none';
        box.style.willChange = 'auto';
        box.style.margin = '0';
        box.style.float = 'none';
        box.style.display = 'block';
        box.style.pointerEvents = 'auto';
        box.style.zIndex = '2147483000';

        var v = vpSize();
        var rect = link.getBoundingClientRect();
        var boxH = box.offsetHeight || 260;
        var boxW = box.offsetWidth || 300;
        var topOffset = (typeof defaultTopOffset === 'number') ? defaultTopOffset : 75;
        var gap = 8;

        var top;
        if (rect.top - topOffset >= 8) {
            top = rect.top - topOffset;
        } else if (rect.bottom + gap + boxH <= v.height - 8) {
            top = rect.bottom + gap;
        } else {
            top = 8;
        }
        if (top + boxH > v.height - 8) {
            top = Math.max(8, v.height - boxH - 8);
        }

        var left = rect.left + (typeof xOffset === 'number' ? xOffset : 17);
        if (left + boxW > v.width - 8) left = Math.max(8, v.width - boxW - 8);
        if (left < 8) left = 8;

        box.style.top = Math.round(top) + 'px';
        box.style.left = Math.round(left) + 'px';

        // 20ms 兜底校正
        setTimeout(function () {
            try {
                if (!box || box.style.display === 'none') return;
                var br = box.getBoundingClientRect();
                var v2 = vpSize();
                if (br.bottom > v2.height - 4) box.style.top = Math.max(4, v2.height - br.height - 4) + 'px';
                if (br.top < 4) box.style.top = '4px';
                if (br.right > v2.width - 4) box.style.left = Math.max(4, v2.width - br.width - 4) + 'px';
                if (br.left < 4) box.style.left = '4px';
            } catch (e) {}
        }, 20);
    }

    function applyToAllNewBoxes() {
        var ids = ['calendarbox', 'clockbox'];
        for (var k = 0; k < ids.length; k++) {
            var i = 0;
            while (i < 30) {  // 页面里最多 30 个日期字段足够
                var el = document.getElementById(ids[k] + i);
                if (!el) break;
                if (el.style.display !== 'none' && el.getAttribute('data-guwu-placed') !== '1') {
                    el.setAttribute('data-guwu-placed', '1');
                    var link = findLinkForBox(el);
                    placePopup(el, link, 17, ids[k] === 'calendarbox' ? 75 : 30);
                }
                i++;
            }
        }
    }

    function patch() {
        if (window.DateTimeShortcuts && !window.DateTimeShortcuts.__guwuPatched) {
            window.DateTimeShortcuts.__guwuPatched = true;
            window.DateTimeShortcuts.openCalendar = function (num) {
                var calBox = document.getElementById(window.DateTimeShortcuts.calendarDivName1 + num);
                var calLink = document.getElementById(window.DateTimeShortcuts.calendarLinkName + num);
                var inp = window.DateTimeShortcuts.calendarInputs[num];
                if (inp && inp.value) {
                    try {
                        var fmt = window.get_format('DATE_INPUT_FORMATS')[0];
                        var selected = inp.value.strptime(fmt);
                        var year = selected.getUTCFullYear();
                        var month = selected.getUTCMonth() + 1;
                        if (/^\d{4}$/.test(String(year)) && month >= 1 && month <= 12) {
                            if (window.DateTimeShortcuts.calendars && window.DateTimeShortcuts.calendars[num]) {
                                window.DateTimeShortcuts.calendars[num].drawDate(month, year, selected);
                            }
                        }
                    } catch (e) {}
                }
                if (calBox) {
                    calBox.style.display = 'block';
                    placePopup(calBox, calLink, 17, 75);
                }
                document.addEventListener('click', window.DateTimeShortcuts.dismissCalendarFunc[num]);
            };
            window.DateTimeShortcuts.openClock = function (num) {
                var clockBox = document.getElementById(window.DateTimeShortcuts.clockDivName + num);
                var clockLink = document.getElementById(window.DateTimeShortcuts.clockLinkName + num);
                if (clockBox) {
                    clockBox.style.display = 'block';
                    placePopup(clockBox, clockLink, 17, 30);
                }
                document.addEventListener('click', window.DateTimeShortcuts.dismissClockFunc[num]);
            };
            window.DateTimeShortcuts.dismissCalendar = function (num) {
                var box = document.getElementById(window.DateTimeShortcuts.calendarDivName1 + num);
                if (box) box.style.display = 'none';
                document.removeEventListener('click', window.DateTimeShortcuts.dismissCalendarFunc[num]);
            };
            window.DateTimeShortcuts.dismissClock = function (num) {
                var box = document.getElementById(window.DateTimeShortcuts.clockDivName + num);
                if (box) box.style.display = 'none';
                document.removeEventListener('click', window.DateTimeShortcuts.dismissClockFunc[num]);
            };
        }
        // MutationObserver：即使以上 openCalendar/openClock 没被替换，也能兜住。
        if (!window.__guwuMo) {
            window.__guwuMo = true;
            if (typeof MutationObserver !== 'undefined') {
                var mo = new MutationObserver(function (mutations) {
                    var triggered = false;
                    for (var i = 0; i < mutations.length; i++) {
                        var added = mutations[i].addedNodes;
                        for (var j = 0; j < added.length; j++) {
                            var n = added[j];
                            if (n.nodeType === 1) {
                                if (n.id && /^(calendarbox\d+|clockbox\d+)$/i.test(n.id)) {
                                    triggered = true;
                                    break;
                                }
                                if (n.querySelector && n.querySelector('[id^="calendarbox"], [id^="clockbox"]')) {
                                    triggered = true;
                                    break;
                                }
                            }
                        }
                        if (triggered) break;
                    }
                    if (triggered) {
                        try { applyToAllNewBoxes(); } catch (e) {}
                    }
                });
                try { mo.observe(document.body, { childList: true, subtree: true }); } catch (e) {}
            }
        }
        // 兜底：直接轮询一次（setTimeout）
        setTimeout(applyToAllNewBoxes, 30);
        setTimeout(applyToAllNewBoxes, 300);
    }

    if (document.readyState === 'complete' || document.readyState === 'interactive') {
        patch();
    } else {
        document.addEventListener('DOMContentLoaded', patch, { once: true });
        window.addEventListener('load', patch, { once: true });
    }
    // 额外保险：即使上面所有事件都错过，也在 100ms / 1000ms 再执行一次 patch
    setTimeout(patch, 100);
    setTimeout(patch, 1000);
})();
