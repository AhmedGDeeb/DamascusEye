from django import template
from django.utils.http import urlencode
from django.utils.translation import gettext as _

register = template.Library()

STATUS_TONES = {
    # Violation
    'detected': 'warn', 'confirmed': 'danger', 'resolved': 'ok', 'rejected': 'muted',
    # Capture
    'pending': 'muted', 'processing': 'info', 'done': 'ok', 'failed': 'danger',
    # Node
    'active': 'ok', 'maintenance': 'warn', 'retired': 'muted',
    # Alert response
    'none': 'warn', 'acknowledged': 'info', 'dispatched': 'ok', 'ignored': 'danger',
}


@register.filter
def tone(status):
    return STATUS_TONES.get(status, 'muted')


@register.filter
def pct(value, digits=0):
    if value is None:
        return '—'
    return f'{value * 100:.{int(digits)}f}%'


@register.filter
def score_tone(score):
    if score is None:
        return 'muted'
    if score >= 60:
        return 'danger'
    if score >= 30:
        return 'warn'
    return 'ok'


@register.filter
def duration(delta):
    """timedelta -> short text, e.g. "3h 20m" / "3 س 20 د"."""
    if delta is None:
        return '—'
    minutes = int(delta.total_seconds() // 60)
    days, minutes = divmod(minutes, 60 * 24)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if days:
        parts.append(_('%(n)sd') % {'n': days})
    if hours and len(parts) < 2:
        parts.append(_('%(n)sh') % {'n': hours})
    if (minutes or not parts) and len(parts) < 2 and not days:
        parts.append(_('%(n)sm') % {'n': minutes})
    return ' '.join(parts)


@register.simple_tag(takes_context=True)
def query(context, **changes):
    """Current query string with some keys replaced; used by filters and pagination."""
    params = context['request'].GET.copy()
    for key, value in changes.items():
        if value in (None, ''):
            params.pop(key, None)
        else:
            params[key] = value
    if 'page' not in changes:
        params.pop('page', None)
    encoded = urlencode(sorted(params.items()))
    return f'?{encoded}' if encoded else '?'
