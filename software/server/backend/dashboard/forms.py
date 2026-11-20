import json

from django import forms
from django.utils.translation import gettext_lazy as _

from monitoring.models import Alert, Node, Review, SidewalkZone, Violation


class LatinDigitsMixin:
    """Chrome draws <input type="number"> in the browser's own locale, so an Arabic
    browser shows Arabic-Indic digits whatever the page says. Render number fields
    as text inputs with a numeric keyboard instead; Django still validates them as numbers."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            if isinstance(field.widget, forms.NumberInput):
                attrs = {k: v for k, v in field.widget.attrs.items() if k not in ('step', 'min', 'max')}
                attrs.update(inputmode='decimal', dir='ltr', autocomplete='off')
                field.widget = forms.TextInput(attrs=attrs)


class NodeForm(LatinDigitsMixin, forms.ModelForm):
    class Meta:
        model = Node
        fields = ['code', 'name', 'mac_address', 'latitude', 'longitude', 'mount_height_m',
                  'status', 'installed_at', 'notes']
        labels = {
            'code': _('Node code'),
            'name': _('Name or location'),
            'mac_address': _('MAC address'),
            'latitude': _('Latitude'),
            'longitude': _('Longitude'),
            'mount_height_m': _('Mounting height (m)'),
            'status': _('Status'),
            'installed_at': _('Installed on'),
            'notes': _('Notes'),
        }
        help_texts = {
            'code': _('The ID the firmware sends, e.g. N01.'),
            'mac_address': _('e.g. 24:6F:28:AA:BB:CC'),
        }
        widgets = {
            'installed_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
            'notes': forms.Textarea(attrs={'rows': 3}),
            'latitude': forms.NumberInput(attrs={'step': '0.000001', 'placeholder': '33.5138'}),
            'longitude': forms.NumberInput(attrs={'step': '0.000001', 'placeholder': '36.2765'}),
        }


class ZoneForm(LatinDigitsMixin, forms.ModelForm):
    polygon = forms.CharField(widget=forms.HiddenInput, error_messages={
        'required': _('Draw the polygon on the image first (at least 3 points).'),
    })

    class Meta:
        model = SidewalkZone
        fields = ['name', 'sidewalk_width_m', 'is_active', 'polygon']
        labels = {
            'name': _('Zone name'),
            'sidewalk_width_m': _('Actual sidewalk width (m)'),
            'is_active': _('Active'),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.initial['polygon'] = json.dumps(self.instance.polygon)

    def clean_polygon(self):
        try:
            points = json.loads(self.cleaned_data['polygon'])
            points = [[round(float(x), 4), round(float(y), 4)] for x, y in points]
        except (TypeError, ValueError):
            raise forms.ValidationError(_('Invalid polygon format.'))
        if len(points) < 3:
            raise forms.ValidationError(_('The polygon needs at least 3 points.'))
        if not all(0 <= v <= 1 for point in points for v in point):
            raise forms.ValidationError(_('All points must be inside the image.'))
        return points


class ReportViolationForm(LatinDigitsMixin, forms.Form):
    category = forms.ChoiceField(label=_('Encroachment type'), choices=Violation.Category.choices)
    zone = forms.ModelChoiceField(label=_('Sidewalk zone'), queryset=SidewalkZone.objects.none(), required=False,
                                  empty_label=_('— none —'))
    occupancy_ratio = forms.FloatField(label=_('Sidewalk occupancy (0 to 1)'), min_value=0, max_value=1, required=False)
    blocked_width_m = forms.FloatField(label=_('Blocked width (m)'), min_value=0, required=False)
    notes = forms.CharField(label=_('Notes'), widget=forms.Textarea(attrs={'rows': 2}), required=False)

    def __init__(self, *args, node=None, **kwargs):
        super().__init__(*args, **kwargs)
        if node is not None:
            self.fields['zone'].queryset = node.zones.filter(is_active=True)


class ReviewForm(LatinDigitsMixin, forms.Form):
    decision = forms.ChoiceField(label=_('Decision'), choices=Review.Decision.choices, widget=forms.RadioSelect)
    impact_score = forms.FloatField(label=_('Impact score (optional, overrides the computed one)'), min_value=0, max_value=100,
                                    required=False)
    notes = forms.CharField(label=_('Notes'), widget=forms.Textarea(attrs={'rows': 2}), required=False)


class AlertForm(forms.Form):
    channel = forms.ChoiceField(label=_('Channel'), choices=Alert.Channel.choices)


class AlertResponseForm(forms.Form):
    response = forms.ChoiceField(label=_('Inspector response'), choices=Alert.Response.choices)
    notes = forms.CharField(label=_('Notes'), required=False)
