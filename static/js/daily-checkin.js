// Extracted from templates/base.html inline <script> (2026-09-13).
// The daily check-in modal is rendered and this script only included when
// request.daily_checkin_available is true. The POST endpoint URL is passed
// via data-url on #daily-checkin-modal.
(function () {
    var modal = document.getElementById('daily-checkin-modal');
    var submit = document.getElementById('daily-checkin-submit');
    var done = document.getElementById('daily-checkin-done');
    var error = document.getElementById('daily-checkin-error');
    if (!modal || !submit || !window.bootstrap) return;
    var url = modal.getAttribute('data-url');
    if (!url) return;
    var csrf = (document.cookie.match(/csrftoken=([^;]+)/) || [, ''])[1];
    submit.addEventListener('click', function () {
        submit.disabled = true;
        fetch(url, {method: 'POST', credentials: 'same-origin', headers: {'X-CSRFToken': csrf, 'Accept': 'application/json'}})
            .then(function (response) { return response.json().then(function (data) { if (!response.ok) throw new Error(data.message || '签到失败'); return data; }); })
            .then(function (data) {
                document.getElementById('daily-checkin-streak').textContent = data.streak;
                document.getElementById('daily-checkin-reward-text').textContent = '本次获得 +' + data.points + ' 积分';
                submit.classList.add('d-none'); done.classList.remove('d-none');
            })
            .catch(function (err) { error.textContent = err.message; error.classList.remove('d-none'); submit.disabled = false; });
    });
    new window.bootstrap.Modal(modal).show();
})();
