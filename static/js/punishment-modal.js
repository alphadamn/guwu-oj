// Extracted from templates/base.html inline <script> (2026-09-13).
// This file is loaded at the BOTTOM of <body>, so the shared modal
// (#guwu-punishment-modal) is already in the DOM before we run.
//
// Dynamic values are passed via data attributes on #guwu-punishment-modal:
//   data-page-punishment  - JSON of the punishment set in the login view context
//   data-session-notice   - JSON of the session punishment notice (feature-ban)
//   data-clear-url        - URL that clears the session notice after showing
(function () {
    var modal = document.getElementById('guwu-punishment-modal');
    if (!modal) return;

    var pagePunishment = null;
    var rawPage = modal.getAttribute('data-page-punishment');
    var rawNotice = modal.getAttribute('data-session-notice');
    if (rawPage) {
        try { pagePunishment = JSON.parse(rawPage); }
        catch (e) { pagePunishment = null; }
    } else if (rawNotice) {
        try { pagePunishment = JSON.parse(rawNotice); }
        catch (e) { pagePunishment = null; }
    }

    if (!pagePunishment || !pagePunishment.kind) return;

    // Header: pick an icon + color class per punishment kind.
    var header = modal.querySelector('.guwu-punishment-header');
    var iconEl = modal.querySelector('.guwu-punishment-icon');
    var titleEl = modal.querySelector('.guwu-punishment-title-text');
    var reasonEl = modal.querySelector('.guwu-punishment-reason');
    var endsAtBox = modal.querySelector('.guwu-punishment-ends-at');
    var featuresBox = modal.querySelector('.guwu-punishment-features');
    var closeBtn = modal.querySelector('.guwu-punishment-close');

    // Reset per-kind classes / defaults
    header.className =
        'modal-header text-white guwu-punishment-header d-flex align-items-center gap-2';
    header.classList.add('kind-' + (pagePunishment.kind || 'feature_ban'));

    var iconClass = 'bi-info-circle-fill';
    var buttonLabel = '我已了解';
    var reasonPrefix = '原因：';

    if (pagePunishment.kind === 'permanent_ban') {
        iconClass = 'bi-shield-fill-exclamation';
        reasonPrefix = '';
        buttonLabel = '关闭';
    } else if (pagePunishment.kind === 'temporary_ban') {
        iconClass = 'bi-stop-circle-fill';
        reasonPrefix = '';
        buttonLabel = '关闭';
    } else {
        // feature ban
        iconClass = 'bi-exclamation-triangle-fill';
        buttonLabel = '知道了';
    }
    if (iconEl) iconEl.className = iconEl.className.replace(/bi-[a-z0-9-]+/i, iconClass);

    // Title and reason
    if (titleEl) titleEl.textContent = pagePunishment.title || '账号状态';
    if (reasonEl) reasonEl.textContent =
        reasonPrefix + (pagePunishment.reason || '未填写');

    // Deadline
    if (pagePunishment.ends_at) {
        endsAtBox.classList.remove('d-none');
        endsAtBox.querySelector('.guwu-punishment-ends-at-value').textContent =
            pagePunishment.ends_at;
    } else {
        endsAtBox.classList.add('d-none');
    }

    // Feature list (for feature_ban; ignored for hard bans)
    if (pagePunishment.feature_labels && pagePunishment.feature_labels.length > 0) {
        featuresBox.classList.remove('d-none');
        featuresBox.innerHTML = '';
        pagePunishment.feature_labels.forEach(function (label) {
            var li = document.createElement('li');
            li.className = 'list-group-item small';
            li.textContent = '• 已禁用：' + label;
            featuresBox.appendChild(li);
        });
    } else {
        featuresBox.classList.add('d-none');
    }

    // CTA button label
    if (closeBtn) closeBtn.textContent = buttonLabel;

    if (window.bootstrap && window.bootstrap.Modal) {
        new window.bootstrap.Modal(modal).show();
    } else {
        modal.classList.add('show');
        modal.setAttribute('aria-modal', 'true');
        modal.style.display = 'block';
    }
    // After showing the session notice, clear it on the server side so
    // the modal only appears once.
    var clearUrl = modal.getAttribute('data-clear-url');
    if (clearUrl) {
        try {
            fetch(clearUrl, {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'X-CSRFToken': (document.cookie.match(/csrftoken=([^;]+)/) || [, ''])[1] },
            }).catch(function () {});
        } catch (e) {}
    }
})();
