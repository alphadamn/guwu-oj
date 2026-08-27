from django import template
from django.utils.safestring import mark_safe

from problems.markdown_utils import render_markdown

register = template.Library()


@register.filter(name='markdown')
def markdown_filter(value):
    return render_markdown(value)
