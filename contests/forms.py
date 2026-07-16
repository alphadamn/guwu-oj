from django import forms
from django.utils import timezone

from .models import ContestProblem, ContestTestCase

MIN_TEST_CASES = 3


class ContestForm(forms.Form):
    name = forms.CharField(max_length=200, widget=forms.TextInput(attrs={'class': 'form-control'}))
    description = forms.CharField(required=False, widget=forms.Textarea(attrs={'class': 'form-control', 'rows': 4}))
    start_at = forms.DateTimeField(input_formats=['%Y-%m-%dT%H:%M'], widget=forms.DateTimeInput(attrs={'class': 'form-control', 'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'))
    end_at = forms.DateTimeField(input_formats=['%Y-%m-%dT%H:%M'], widget=forms.DateTimeInput(attrs={'class': 'form-control', 'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'))
    max_submissions_per_problem = forms.IntegerField(min_value=1, max_value=1000, initial=3, widget=forms.NumberInput(attrs={'class': 'form-control', 'min': 1}))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('start_at') and cleaned.get('end_at') and cleaned['end_at'] <= cleaned['start_at']:
            raise forms.ValidationError('结束时间必须晚于开始时间。')
        if cleaned.get('end_at') and cleaned['end_at'] <= timezone.now():
            raise forms.ValidationError('结束时间必须晚于当前时间。')
        return cleaned


class ContestProblemForm(forms.ModelForm):
    class Meta:
        model = ContestProblem
        fields = ['title', 'description', 'input_format', 'output_format', 'sample_input', 'sample_output', 'hint', 'difficulty', 'time_limit', 'memory_limit', 'tags']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'form-control'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 8}),
            'input_format': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'output_format': forms.Textarea(attrs={'class': 'form-control', 'rows': 4}),
            'sample_input': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'sample_output': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'hint': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'difficulty': forms.Select(attrs={'class': 'form-select'}),
            'time_limit': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'memory_limit': forms.NumberInput(attrs={'class': 'form-control', 'min': 1}),
            'tags': forms.TextInput(attrs={'class': 'form-control'}),
        }


def parse_test_cases_from_post(post_data):
    indices = sorted(int(key.removeprefix('test_input_')) for key in post_data if key.startswith('test_input_') and key.removeprefix('test_input_').isdigit())
    return [(post_data.get(f'test_input_{index}', '').strip(), post_data.get(f'test_output_{index}', '').strip()) for index in indices if post_data.get(f'test_input_{index}', '').strip() or post_data.get(f'test_output_{index}', '').strip()]


def validate_test_cases(cases):
    if len(cases) < MIN_TEST_CASES:
        return f'至少需要 {MIN_TEST_CASES} 个测试用例，当前提供了 {len(cases)} 个。'
    for index, (_input_data, expected_output) in enumerate(cases, start=1):
        if not expected_output:
            return f'测试用例 #{index} 的输出不能为空。'
    return None


def save_test_cases(contest_problem, cases):
    ContestTestCase.objects.filter(contest_problem=contest_problem).delete()
    ContestTestCase.objects.bulk_create([
        ContestTestCase(contest_problem=contest_problem, input_data=input_data, expected_output=expected_output, order=index, is_sample=index == 0)
        for index, (input_data, expected_output) in enumerate(cases)
    ])
