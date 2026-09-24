from django import forms
from .models import Problem, TestCase

MIN_TEST_CASES = 3


class ProblemForm(forms.ModelForm):
    class Meta:
        model = Problem
        fields = [
            'title', 'description', 'input_format', 'output_format',
            'sample_input', 'sample_output', 'hint', 'difficulty',
            'time_limit', 'memory_limit', 'tags', 'problem_type',
        ]
        widgets = {
            'title': forms.TextInput(attrs={'class': 'form-control'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 8}),
            'input_format': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'output_format': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'sample_input': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'sample_output': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'hint': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'difficulty': forms.Select(attrs={'class': 'form-select'}),
            'time_limit': forms.NumberInput(attrs={
                'class': 'form-control',
                'min': 1,
                'step': 1,
                'title': '单位：毫秒',
            }),
            'memory_limit': forms.NumberInput(attrs={'class': 'form-control'}),
            'tags': forms.TextInput(attrs={'class': 'form-control'}),
            'problem_type': forms.RadioSelect(attrs={'class': 'form-check-input'}),
        }
        help_texts = {
            'time_limit': '单位：毫秒（例如 1000 表示 1 秒）',
            'memory_limit': '单位：MB',
        }


def parse_test_cases_from_post(post_data):
    """Extract (input, output) pairs from POST fields test_input_N / test_output_N."""
    indices = set()
    for key in post_data:
        if key.startswith('test_input_'):
            suffix = key[len('test_input_'):]
            if suffix.isdigit():
                indices.add(int(suffix))

    cases = []
    for i in sorted(indices):
        inp = post_data.get(f'test_input_{i}', '').strip()
        out = post_data.get(f'test_output_{i}', '').strip()
        if inp or out:
            cases.append((inp, out))
    return cases


def validate_test_cases(cases):
    if len(cases) < MIN_TEST_CASES:
        return f'至少需要 {MIN_TEST_CASES} 个测试用例，当前提供了 {len(cases)} 个。'

    for i, (inp, out) in enumerate(cases, start=1):
        # if not inp:
        #     return f'测试用例 #{i} 的输入不能为空。'
        if not out:
            return f'测试用例 #{i} 的输出不能为空。'
    return None


def save_test_cases(problem, cases):
    TestCase.objects.filter(problem=problem).delete()
    for order, (inp, out) in enumerate(cases):
        TestCase.objects.create(
            problem=problem,
            input_data=inp,
            expected_output=out,
            order=order,
            is_sample=(order == 0),
        )


def parse_function_files_from_post(post_data):
    """Extract [{name, content}, ...] from POST fields func_file_name_N / func_file_content_N.

    Mirrors parse_test_cases_from_post so the create-page JS can clone rows
    the same way. Empty-name rows are dropped; rows with name but empty
    content are kept (a header can legitimately be empty while drafting).
    """
    indices = set()
    for key in post_data:
        if key.startswith('func_file_name_'):
            suffix = key[len('func_file_name_'):]
            if suffix.isdigit():
                indices.add(int(suffix))

    files = []
    for i in sorted(indices):
        name = post_data.get(f'func_file_name_{i}', '').strip()
        content = post_data.get(f'func_file_content_{i}', '')
        if name:
            # Defence in depth: no path separators that escape the work dir
            # at judge time. The judge ALSO validates, but blocking here
            # gives the user immediate feedback.
            if '/' in name or '\\' in name or name.startswith('.'):
                continue
            files.append({'name': name, 'content': content})
    return files


def validate_function_files(files, problem_type):
    """Return an error message string or None.

    Function problems require at least one .cpp file (the grader with
    ``main``); a header-only package would have nothing to link against
    the user's submission. Interactive problems use the same bundle: the
    manager plays the grader role (it drives the user process and prints
    the verdict).
    """
    if problem_type not in ('function', 'interactive'):
        return None
    kind = '交互题' if problem_type == 'interactive' else '函数题'
    if not files:
        return f'{kind}至少需要提供一个 .cpp 文件（判题用 grader/manager）。'
    has_cpp = any(f['name'].endswith('.cpp') for f in files)
    if not has_cpp:
        return f'{kind}至少需要提供一个 .cpp 文件（含 main 的 grader/manager）。'
    return None
