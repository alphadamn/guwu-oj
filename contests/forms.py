from django import forms
from django.utils import timezone


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
