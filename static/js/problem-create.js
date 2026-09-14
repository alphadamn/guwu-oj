// Extracted from templates/problems/create_problem.html inline <script> (2026-09-13).
(function () {
    var MIN_CASES = 3;
    var caseIndex = 0;
    var container = document.getElementById('test-cases-container');
    var countBadge = document.getElementById('case-count');

    function updateCount() {
        countBadge.textContent = container.children.length;
    }

    function createCaseCard(index) {
        var card = document.createElement('div');
        card.className = 'card test-case-card mb-3';
        card.dataset.index = index;
        card.innerHTML = '<div class="card-header d-flex justify-content-between align-items-center py-2">' +
            '<span class="fw-bold">测试用例 #' + (index + 1) + '</span>' +
            '<button type="button" class="btn btn-sm btn-outline-danger remove-case-btn" ' + (index < MIN_CASES ? 'disabled title="至少保留3个用例"' : '') + '>' +
            '<i class="bi bi-trash"></i></button></div>' +
            '<div class="card-body"><div class="row">' +
            '<div class="col-md-6 mb-2"><label class="form-label">输入</label>' +
            '<textarea class="form-control font-monospace" name="test_input_' + index + '" rows="4" required></textarea></div>' +
            '<div class="col-md-6 mb-2"><label class="form-label">期望输出</label>' +
            '<textarea class="form-control font-monospace" name="test_output_' + index + '" rows="4" required></textarea></div>' +
            '</div></div>';
        card.querySelector('.remove-case-btn').addEventListener('click', function () {
            if (container.children.length <= MIN_CASES) return;
            card.remove();
            renumberCases();
        });
        return card;
    }

    function renumberCases() {
        Array.from(container.children).forEach(function (card) {
            card.querySelector('.card-header span').textContent = '测试用例 #' + (Number(card.dataset.index) + 1);
            var inp = card.querySelector('textarea[name^="test_input_"]');
            var out = card.querySelector('textarea[name^="test_output_"]');
            var idx = card.dataset.index;
            inp.name = 'test_input_' + idx;
            out.name = 'test_output_' + idx;
            var btn = card.querySelector('.remove-case-btn');
            btn.disabled = container.children.length <= MIN_CASES;
        });
        updateCount();
    }

    document.getElementById('add-case-btn').addEventListener('click', function () {
        container.appendChild(createCaseCard(caseIndex++));
        renumberCases();
    });

    for (var i = 0; i < MIN_CASES; i++) {
        container.appendChild(createCaseCard(caseIndex++));
    }
    updateCount();
})();
