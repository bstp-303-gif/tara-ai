from django import template

register = template.Library()


@register.filter
def trim(value):
    return str(value).strip()


@register.filter
def split_certs(value):
    return [c.strip() for c in str(value).split(',') if c.strip()]
