(function () {
    'use strict';

    var pending = [];
    var modal = null;
    var challengeId = '';
    var imageUrl = '';
    var verificationInProgress = false;

    function csrfToken() {
        var match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
        return match ? decodeURIComponent(match[1]) : '';
    }

    function ensureModal() {
        if (modal) return modal;
        modal = document.createElement('div');
        modal.className = 'modal fade';
        modal.id = 'avatar-captcha-modal';
        modal.tabIndex = -1;
        modal.innerHTML =
            '<div class="modal-dialog modal-dialog-centered modal-sm">' +
                '<div class="modal-content">' +
                    '<div class="modal-header"><h5 class="modal-title">头像访问验证</h5>' +
                        '<button type="button" class="btn-close" data-bs-dismiss="modal"></button></div>' +
                    '<div class="modal-body">' +
                        '<p class="small text-muted">头像访问较频繁，请完成图形验证码。</p>' +
                        '<img class="avatar-captcha-image img-fluid border rounded mb-2" alt="图形验证码">' +
                        '<button type="button" class="btn btn-link btn-sm p-0 d-block avatar-captcha-refresh">换一张</button>' +
                        '<input class="form-control mt-2 avatar-captcha-answer" autocomplete="off" maxlength="12" placeholder="请输入验证码">' +
                        '<div class="small text-danger mt-2 avatar-captcha-error"></div>' +
                    '</div>' +
                    '<div class="modal-footer"><button type="button" class="btn btn-primary avatar-captcha-submit">验证</button></div>' +
                '</div>' +
            '</div>';
        document.body.appendChild(modal);

        modal.querySelector('.avatar-captcha-refresh').addEventListener('click', loadChallenge);
        modal.querySelector('.avatar-captcha-submit').addEventListener('click', verify);
        modal.querySelector('.avatar-captcha-answer').addEventListener('keydown', function (event) {
            if (event.key === 'Enter') verify();
        });
        return modal;
    }

    function showModal() {
        var element = ensureModal();
        if (window.bootstrap && window.bootstrap.Modal) {
            window.bootstrap.Modal.getOrCreateInstance(element).show();
        } else {
            element.style.display = 'block';
            element.classList.add('show');
        }
    }

    function loadChallenge() {
        var element = ensureModal();
        var error = element.querySelector('.avatar-captcha-error');
        error.textContent = '';
        return fetch('/users/captcha/image/', {
            credentials: 'same-origin',
            cache: 'no-store'
        }).then(function (response) {
            var newId = (response.headers.get('X-Captcha-Id') || '').trim();
            if (!response.ok || !newId) throw new Error('验证码暂时不可用，请稍后重试。');
            challengeId = newId;
            return response.blob();
        }).then(function (blob) {
            if (imageUrl) URL.revokeObjectURL(imageUrl);
            imageUrl = URL.createObjectURL(blob);
            element.querySelector('.avatar-captcha-image').src = imageUrl;
            element.querySelector('.avatar-captcha-answer').focus();
        }).catch(function (error) {
            element.querySelector('.avatar-captcha-error').textContent = error.message;
        });
    }

    function verify() {
        if (verificationInProgress || !challengeId) return;
        var element = ensureModal();
        var answerInput = element.querySelector('.avatar-captcha-answer');
        var answer = answerInput.value.trim();
        var error = element.querySelector('.avatar-captcha-error');
        if (!answer) {
            error.textContent = '请输入验证码。';
            return;
        }
        verificationInProgress = true;
        error.textContent = '';
        var body = new URLSearchParams();
        body.set('captcha_id', challengeId);
        body.set('captcha_answer', answer);
        fetch('/users/avatar-captcha/verify/', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
                'X-CSRFToken': csrfToken(),
            },
            body: body.toString(),
        }).then(function (response) {
            return response.json().then(function (data) {
                if (!response.ok || !data.ok) throw new Error(data.message || '验证失败。');
                return data;
            });
        }).then(function () {
            challengeId = '';
            pending.slice().forEach(loadAvatar);
            pending = [];
            if (window.bootstrap && window.bootstrap.Modal) {
                window.bootstrap.Modal.getOrCreateInstance(element).hide();
            } else {
                element.style.display = 'none';
                element.classList.remove('show');
            }
        }).catch(function (error) {
            error = error || new Error('验证失败。');
            element.querySelector('.avatar-captcha-error').textContent = error.message;
            challengeId = '';
            loadChallenge();
        }).finally(function () {
            verificationInProgress = false;
        });
    }

    function loadAvatar(image) {
        var url = image.getAttribute('data-avatar-url');
        if (!url) return;
        fetch(url, { credentials: 'same-origin', cache: 'no-store' }).then(function (response) {
            if (response.status === 429 && response.headers.get('X-Captcha-Required') === '1') {
                if (pending.indexOf(image) === -1) pending.push(image);
                showModal();
                if (!challengeId) loadChallenge();
                return null;
            }
            if (!response.ok) throw new Error('头像加载失败。');
            return response.blob();
        }).then(function (blob) {
            if (!blob) return;
            var objectUrl = URL.createObjectURL(blob);
            var previous = image.getAttribute('data-avatar-object-url');
            if (previous) URL.revokeObjectURL(previous);
            image.setAttribute('data-avatar-object-url', objectUrl);
            image.src = objectUrl;
        }).catch(function () {
            image.classList.add('avatar-load-failed');
        });
    }

    function init() {
        document.querySelectorAll('img[data-avatar-url]').forEach(loadAvatar);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
