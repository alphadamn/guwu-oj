from django import forms
from .models import Problem, TestCase

MIN_TEST_CASES = 3


class ProblemForm(forms.ModelForm):
    class Meta:
        model = Problem
        fields = [
            'title', 'description', 'input_format', 'output_format',
            'sample_input', 'sample_output', 'hint', 'difficulty',
            'time_limit', 'memory_limit', 'tags', 'is_public',
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
            'is_public': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
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
