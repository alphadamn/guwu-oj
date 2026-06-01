from django import template
from django.utils.safestring import mark_safe

from problems.markdown_utils import render_markdown

register = template.Library()


@register.filter(name='markdown')
def markdown_filter(value):
    return mark_safe(render_markdown(value))

@register.filter(name='percentage')
def percentage(a,b):
    return a / b * 100