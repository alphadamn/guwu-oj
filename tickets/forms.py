from django import forms

from .models import Ticket


class TicketForm(forms.ModelForm):
    """工单提交表单。

    ``website`` 是蜜罐字段：正常用户不可见也不会填，机器人填了即静默拒绝。
    """

    website = forms.CharField(required=False, widget=forms.HiddenInput)

    class Meta:
        model = Ticket
        fields = ['subject', 'category', 'body', 'contact_email']
        widgets = {
            'body': forms.Textarea(attrs={'rows': 8, 'maxlength': 5000}),
        }

    def clean_website(self):
        value = (self.cleaned_data.get('website') or '').strip()
        if value:
            raise forms.ValidationError('提交被拒绝。', code='honeypot')
        return value

    def clean_contact_email(self):
        return (self.cleaned_data.get('contact_email') or '').strip()
