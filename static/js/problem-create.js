// Extracted from templates/problems/create_problem.html inline <script> (2026-09-13).
// Extended 2026-09-23: problem_type radio + function-files section for IOI-style problems.
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

    // ── Function-files section (IOI-style problems) ──────────────────────
    // Lazy-initialised: rows are only created when the user first picks
    // "function", so standard-problem submissions stay clean.

    var DEFAULT_FILES = [
        {
            name: 'problem.h',
            content: '// 函数声明，由出题者填写\n'
        },
        {
            name: 'grader.cpp',
            content: '#include "problem.h"\n#include <iostream>\n\nint main() {\n    // 调用用户在 submission.cpp 中实现的函数，读取输入、打印结果\n    return 0;\n}\n'
        }
    ];
    // Interactive problems: the manager (with its own main) drives the user
    // process over stdin/stdout or the FIFOs handed to it via argv, then
    // prints the verdict that gets compared with each test's expected output.
    var DEFAULT_INTERACTIVE_FILES = [
        {
            name: 'manager.cpp',
            content: '// 交互题 manager：启动/驱动用户进程，通信结束后打印判定结果\n'
        }
    ];
    var fileIndex = 0;
    var fileContainer = document.getElementById('function-files-container');
    var fileCountBadge = document.getElementById('file-count');
    var filesInited = false;

    function updateFileCount() {
        fileCountBadge.textContent = fileContainer.children.length;
    }

    function createFileCard(index, name, content) {
        var card = document.createElement('div');
        card.className = 'card function-file-card mb-3';
        card.dataset.index = index;
        card.innerHTML = '<div class="card-header d-flex justify-content-between align-items-center py-2">' +
            '<span class="fw-bold">文件 #' + (index + 1) + '</span>' +
            '<button type="button" class="btn btn-sm btn-outline-danger remove-file-btn" ' +
            (fileContainer.children.length <= 1 ? 'disabled title="至少保留1个文件"' : '') + '>' +
            '<i class="bi bi-trash"></i></button></div>' +
            '<div class="card-body">' +
            '<div class="mb-2"><label class="form-label">文件名</label>' +
            '<input type="text" class="form-control font-monospace" name="func_file_name_' + index +
            '" value="' + (name || '').replace(/"/g, '&quot;') + '" required ' +
            'pattern="[A-Za-z0-9_.\\-]+" title="只允许字母、数字、下划线、点、短横线；不含路径分隔符" /></div>' +
            '<div><label class="form-label">文件内容</label>' +
            '<textarea class="form-control font-monospace" name="func_file_content_' + index +
            '" rows="6">' + (content || '').replace(/</g, '&lt;') + '</textarea></div>' +
            '</div>';
        card.querySelector('.remove-file-btn').addEventListener('click', function () {
            if (fileContainer.children.length <= 1) return;
            card.remove();
            renumberFiles();
        });
        return card;
    }

    function renumberFiles() {
        Array.from(fileContainer.children).forEach(function (card, i) {
            card.dataset.index = i;
            card.querySelector('.card-header span').textContent = '文件 #' + (i + 1);
            var inp = card.querySelector('input[name^="func_file_name_"]');
            var out = card.querySelector('textarea[name^="func_file_content_"]');
            inp.name = 'func_file_name_' + i;
            out.name = 'func_file_content_' + i;
            var btn = card.querySelector('.remove-file-btn');
            btn.disabled = fileContainer.children.length <= 1;
        });
        updateFileCount();
    }

    function initFunctionFiles(isInteractive) {
        if (filesInited) return;
        filesInited = true;
        var defaults = isInteractive ? DEFAULT_INTERACTIVE_FILES : DEFAULT_FILES;
        defaults.forEach(function (f) {
            fileContainer.appendChild(createFileCard(fileIndex++, f.name, f.content));
        });
        // Cards are born with the "disabled" flag evaluated against a
        // half-built list, so re-evaluate every remove button now that all
        // defaults exist (2 files => both deletable, down to the 1-file floor).
        renumberFiles();
    }

    // 'function' / 'interactive' show the bundle section; 'standard' hides it.
    function refreshFileSection() {
        var checked = document.querySelector('input[name="problem_type"]:checked');
        var type = checked ? checked.value : '';
        var wantsFiles = type === 'function' || type === 'interactive';
        document.getElementById('function-files-section').style.display = wantsFiles ? '' : 'none';
        if (wantsFiles) initFunctionFiles(type === 'interactive');
    }

    var radios = document.querySelectorAll('input[name="problem_type"]');
    radios.forEach(function (r) {
        r.addEventListener('change', refreshFileSection);
    });
    // Honour server-rendered state (e.g. when form.is_valid() failed and
    // Django re-renders the user's last selection).
    refreshFileSection();

    document.getElementById('add-file-btn').addEventListener('click', function () {
        initFunctionFiles(); // safety net: button shouldn't be visible otherwise
        fileContainer.appendChild(createFileCard(fileIndex++, '', ''));
        renumberFiles();
    });
})();
