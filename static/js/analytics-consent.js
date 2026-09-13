// Extracted from templates/base.html inline <script> (2026-09-13).
// Dynamic values are passed via data attributes on #oj-analytics-consent:
//   data-geo-enabled          - present only when browser geolocation upload is enabled
//   data-record-location-url - URL that records the browser location
(function () {
    var banner = document.getElementById('oj-analytics-consent');
    if (!banner) return;
    var key = 'oj_analytics_consent';
    var value = document.cookie.split('; ').reduce(function (found, item) {
        var pair = item.split('='); return pair[0] === key ? decodeURIComponent(pair[1] || '') : found;
    }, '');
    if (!value) banner.classList.remove('d-none');
    function csrfToken() {
        var hidden = banner.querySelector('input[name="csrfmiddlewaretoken"]');
        if (hidden && hidden.value) return hidden.value;
        var match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
        if (!match) return '';
        try { return decodeURIComponent(match[1]); } catch (e) { return match[1]; }
    }
    function askLocation() {
        var asked = false;
        try { asked = window.sessionStorage.getItem('oj_geo_done') === '1'; } catch (e) { asked = false; }
        var recordUrl = banner.getAttribute('data-record-location-url');
        var geoEnabled = banner.getAttribute('data-geo-enabled') === 'true';
        if (value !== 'accepted' || !geoEnabled || !navigator.geolocation || asked || !recordUrl) return;
        navigator.geolocation.getCurrentPosition(function (position) {
            // The authoritative copy of the location now lives in the
            // server-side session, so we cannot read it from a cookie.
            // Keep the last uploaded point in localStorage and only
            // re-POST when the reported position actually changes —
            // this is the "定位改变是重新上传" behaviour.
            var lat = position.coords.latitude.toFixed(1);
            var lon = position.coords.longitude.toFixed(1);
            var current = lat + ',' + lon;
            var last = '';
            try { last = window.localStorage.getItem('oj_geo_last') || ''; } catch (e) { last = ''; }
            if (last === current) {
                try { window.sessionStorage.setItem('oj_geo_done', '1'); } catch (e) {}
                return;
            }
            fetch(recordUrl, {method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json','X-CSRFToken':csrfToken()}, body:JSON.stringify({latitude:position.coords.latitude, longitude:position.coords.longitude})})
                .then(function (response) {
                    // Mark the attempt before reloading: if the session
                    // write does not persist we must not re-prompt and
                    // reload forever.
                    try { window.sessionStorage.setItem('oj_geo_done', '1'); } catch (e) {}
                    if (response.ok) {
                        try { window.localStorage.setItem('oj_geo_last', current); } catch (e) {}
                        window.location.reload();
                    }
                })
                .catch(function () {});
        }, function () {
            try { window.sessionStorage.setItem('oj_geo_done', '1'); } catch (e) {}
        }, {enableHighAccuracy:false, timeout:10000, maximumAge:86400000});
    }
    askLocation();
    function decide(choice) {
        document.cookie = key + '=' + choice + '; Max-Age=31536000; Path=/; SameSite=Lax';
        banner.classList.add('d-none');
        value = choice;
        if (choice === 'accepted') askLocation();
    }
    document.getElementById('oj-analytics-accept').addEventListener('click', function () { decide('accepted'); });
    document.getElementById('oj-analytics-reject').addEventListener('click', function () { decide('rejected'); });
})();
