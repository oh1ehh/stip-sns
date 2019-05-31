# -*- coding: utf-8 -*-
import urlparse
from django import template
register = template.Library()

#pk に含まれる . を -- に変更
@register.filter(name='get_accordion_pk')
def get_accordion_pk(v):
    return v.replace('.','--')

@register.filter(name='get_referred_url_tag')
def get_referred_url_tag(referred_url):
    url_parse = urlparse.urlparse(referred_url)
    if len(url_parse.scheme) == 0:
        url = referred_url
    else:
        url = '<a href="%s" target="_blank">%s</a>' % (referred_url,referred_url)
    return url