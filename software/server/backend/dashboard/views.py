from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.http import JsonResponse
from django.db.models import Count, Max, Prefetch, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.utils.translation import gettext as _, gettext_lazy
from django.views.decorators.http import require_GET, require_POST
from urllib.parse import quote

from monitoring import services
from monitoring.models import Alert, Capture, Detection, Heartbeat, InferenceRun, Node, SidewalkZone, Violation

from .forms import AlertForm, AlertResponseForm, NodeForm, ReportViolationForm, ReviewForm, ZoneForm

OPEN_STATUSES = [Violation.Status.DETECTED, Violation.Status.CONFIRMED]


# Helpers

def _redirect_back(request, fallback):
    target = request.POST.get('next') or request.GET.get('next')
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}):
        return redirect(target)
    return redirect(fallback)


def _display_image(capture):
    """The privacy-redacted copy when it exists; otherwise the raw image, flagged in the UI."""
    if capture.redacted_image:
        return capture.redacted_image.url, True
    return capture.image.url, False


def _box(detection):
    return {
        'detection': detection,
        'left': detection.x1 * 100,
        'top': detection.y1 * 100,
        'width': (detection.x2 - detection.x1) * 100,
        'height': (detection.y2 - detection.y1) * 100,
        'on_sidewalk': detection.is_on_sidewalk,
    }


def _zone_points(zone):
    return ' '.join(f'{x},{y}' for x, y in zone.polygon)


def _auto_sidewalk(run):
    """SVG point lists for the sidewalk the YOLO manager segmented in this run."""
    if run is None or not run.sidewalk_polygons:
        return []
    return [' '.join(f'{x},{y}' for x, y in polygon) for polygon in run.sidewalk_polygons]


def _latest_run(capture):
    runs = list(capture.runs.all())
    return runs[0] if runs else None


def _paginate(request, queryset, per_page):
    return Paginator(queryset, per_page).get_page(request.GET.get('page'))


def _battery_chart(heartbeats, start, end, width=600, height=140, v_min=3.0, v_max=4.3):
    """SVG polyline points for battery voltage over a time window."""
    span = (end - start).total_seconds() or 1
    points = []
    for beat in heartbeats:
        if beat.battery_voltage is None:
            continue
        x = (beat.received_at - start).total_seconds() / span * width
        v = min(max(beat.battery_voltage, v_min), v_max)
        y = height - (v - v_min) / (v_max - v_min) * height
        points.append(f'{x:.1f},{y:.1f}')
    return ' '.join(points)


# Overview

@login_required
def overview(request):
    now = timezone.now()
    today = timezone.localdate()
    nodes = list(Node.objects.exclude(status=Node.Status.RETIRED))
    offline_nodes = [n for n in nodes if not n.is_online]

    week_start = today - timedelta(days=6)
    daily = {
        row['captured_at__date']: row['count']
        for row in Capture.objects.filter(captured_at__date__gte=week_start)
        .values('captured_at__date').annotate(count=Count('id'))
    }
    days = [week_start + timedelta(days=i) for i in range(7)]
    peak = max(daily.values(), default=0) or 1
    daily_bars = [{'day': d, 'count': daily.get(d, 0), 'height': daily.get(d, 0) / peak * 100} for d in days]

    categories = (
        Violation.objects.filter(last_seen_at__gte=now - timedelta(days=7))
        .exclude(status=Violation.Status.REJECTED)
        .values('category').annotate(count=Count('id')).order_by('-count')
    )
    category_labels = dict(Violation.Category.choices)
    top_count = max((c['count'] for c in categories), default=0) or 1
    category_bars = [
        {'label': category_labels[c['category']], 'count': c['count'], 'width': c['count'] / top_count * 100}
        for c in categories
    ]

    context = {
        'nodes_total': len(nodes),
        'nodes_online': len(nodes) - len(offline_nodes),
        'offline_nodes': offline_nodes,
        'captures_today': Capture.objects.filter(captured_at__date=today).count(),
        'queue_pending': Capture.objects.filter(status__in=[Capture.Status.PENDING, Capture.Status.PROCESSING]).count(),
        'queue_failed': Capture.objects.filter(status=Capture.Status.FAILED).count(),
        'awaiting_review': Violation.objects.filter(status=Violation.Status.DETECTED).count(),
        'confirmed_not_alerted': Violation.objects.filter(status=Violation.Status.CONFIRMED, alerts__isnull=True).count(),
        'metrics': services.pilot_metrics(since=now - timedelta(days=14)),
        'top_violations': (Violation.objects.filter(status__in=OPEN_STATUSES)
                           .select_related('node').prefetch_related('captures')[:6]),
        'recent_captures': Capture.objects.select_related('node')[:8],
        'daily_bars': daily_bars,
        'category_bars': category_bars,
    }
    return render(request, 'dashboard/overview.html', context)


# Violations

VIOLATION_TABS = [
    ('open', gettext_lazy('Open')),
    (Violation.Status.DETECTED, gettext_lazy('Awaiting review')),
    (Violation.Status.CONFIRMED, gettext_lazy('Confirmed')),
    (Violation.Status.RESOLVED, gettext_lazy('Removed')),
    (Violation.Status.REJECTED, gettext_lazy('Rejected')),
    ('all', gettext_lazy('All')),
]


@login_required
def violation_list(request):
    tab = request.GET.get('status', 'open')
    violations = Violation.objects.select_related('node').prefetch_related('captures').annotate(
        alert_count=Count('alerts', distinct=True))
    if tab == 'open':
        violations = violations.filter(status__in=OPEN_STATUSES)
    elif tab != 'all':
        violations = violations.filter(status=tab)

    category = request.GET.get('category', '')
    if category:
        violations = violations.filter(category=category)
    node = request.GET.get('node', '')
    if node:
        violations = violations.filter(node__code=node)

    counts = dict(Violation.objects.values_list('status').annotate(Count('id')))
    tabs = []
    for key, label in VIOLATION_TABS:
        if key == 'open':
            count = sum(counts.get(s, 0) for s in OPEN_STATUSES)
        elif key == 'all':
            count = sum(counts.values())
        else:
            count = counts.get(key, 0)
        tabs.append({'key': key, 'label': label, 'count': count})

    return render(request, 'dashboard/violation_list.html', {
        'page': _paginate(request, violations, 20),
        'tabs': tabs,
        'tab': tab,
        'category': category,
        'node': node,
        'categories': Violation.Category.choices,
        'nodes': Node.objects.all(),
    })


@login_required
def violation_detail(request, pk):
    violation = get_object_or_404(Violation.objects.select_related('node', 'zone', 'created_by'), pk=pk)
    captures = list(violation.captures.order_by('-captured_at').prefetch_related(
        Prefetch('runs', queryset=InferenceRun.objects.prefetch_related('detections'))))

    selected = captures[0] if captures else None
    if request.GET.get('capture'):
        selected = next((c for c in captures if str(c.pk) == request.GET['capture']), selected)

    image_url, redacted, boxes, auto_sidewalk = None, False, [], []
    if selected:
        image_url, redacted = _display_image(selected)
        run = _latest_run(selected)
        boxes = [_box(d) for d in run.detections.all()] if run else []
        auto_sidewalk = _auto_sidewalk(run)

    detail_link = request.build_absolute_uri(reverse('dashboard:violation_detail', args=[violation.pk]))
    message = services.alert_message(violation, link=detail_link)

    return render(request, 'dashboard/violation_detail.html', {
        'violation': violation,
        'captures': captures,
        'selected': selected,
        'image_url': image_url,
        'redacted': redacted,
        'boxes': boxes,
        'auto_sidewalk': auto_sidewalk,
        'zones': [{'zone': z, 'points': _zone_points(z)} for z in violation.node.zones.filter(is_active=True)],
        'reviews': violation.reviews.select_related('reviewer'),
        'alerts': violation.alerts.select_related('sent_by'),
        'review_form': ReviewForm(initial={'impact_score': None}),
        'alert_form': AlertForm(),
        'response_choices': Alert.Response.choices,
        'alert_text': message,
        'whatsapp_url': 'https://wa.me/?text=' + quote(message),
    })


@login_required
@require_POST
def violation_review(request, pk):
    violation = get_object_or_404(Violation, pk=pk)
    form = ReviewForm(request.POST)
    if form.is_valid():
        services.review_violation(violation, request.user, form.cleaned_data['decision'],
                                  impact_score=form.cleaned_data['impact_score'], notes=form.cleaned_data['notes'])
        messages.success(request, _('Review saved.'))
    else:
        messages.error(request, _('Choose a valid review decision.'))
    return redirect('dashboard:violation_detail', pk=pk)


@login_required
@require_POST
def violation_alert(request, pk):
    violation = get_object_or_404(Violation, pk=pk)
    form = AlertForm(request.POST)
    if form.is_valid():
        try:
            services.send_alert(violation, request.user, channel=form.cleaned_data['channel'])
            messages.success(request, _("Alert recorded. Track the inspectors' response on the Alerts page."))
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
    return redirect('dashboard:violation_detail', pk=pk)


@login_required
@require_POST
def violation_resolve(request, pk):
    violation = get_object_or_404(Violation, pk=pk)
    violation.status = Violation.Status.RESOLVED
    violation.save(update_fields=['status'])
    messages.success(request, _('Violation marked as removed.'))
    return redirect('dashboard:violation_detail', pk=pk)


# Captures

CAPTURE_FILTERS = ('status', 'node', 'date')


def _filtered_captures(request):
    """Captures matching the list filters (?status=&node=&date=), newest first."""
    captures = Capture.objects.select_related('node').prefetch_related(
        Prefetch('runs', queryset=InferenceRun.objects.prefetch_related('detections')))
    status, node, day = (request.GET.get(name, '') for name in CAPTURE_FILTERS)
    if status:
        captures = captures.filter(status=status)
    if node:
        captures = captures.filter(node__code=node)
    if day:
        captures = captures.filter(captured_at__date=day)
    return captures


def _capture_cards(captures):
    cards = []
    for capture in captures:
        run = _latest_run(capture)
        detections = list(run.detections.all()) if run else []
        cards.append({
            'capture': capture,
            'image_url': _display_image(capture)[0],
            'total': len(detections),
            'on_sidewalk': sum(1 for d in detections if d.is_on_sidewalk),
        })
    return cards


@login_required
def capture_list(request):
    page = _paginate(request, _filtered_captures(request), 24)
    filters = {name: request.GET.get(name, '') for name in CAPTURE_FILTERS}
    feed_query = urlencode({k: v for k, v in filters.items() if v})
    return render(request, 'dashboard/capture_list.html', {
        'page': page,
        'cards': _capture_cards(page),
        **filters,
        'statuses': Capture.Status.choices,
        'nodes': Node.objects.all(),
        # Live updates only make sense on the first page, where new images appear.
        'live': page.number == 1,
        'feed_url': reverse('dashboard:capture_feed') + (f'?{feed_query}' if feed_query else ''),
        'latest_id': max((c.pk for c in page), default=0),
    })


@login_required
@require_GET
def capture_feed(request):
    """AJAX feed for the capture list.

    ?after=<id>        captures newer than the newest card on screen (same filters as the list)
    &watch=<id,id,...> cards still waiting for detection; returned again once their status changed
    Cards come back as rendered HTML, so they match the page exactly (language, badges, counts).
    """
    try:
        after = int(request.GET.get('after', 0))
        watch = [int(i) for i in request.GET.get('watch', '').split(',') if i.strip()][:100]
    except ValueError:
        return JsonResponse({'error': 'bad parameters'}, status=400)

    def render_cards(captures):
        return [{'id': card['capture'].pk, 'status': card['capture'].status,
                 'html': render_to_string('dashboard/partials/capture_card.html', {'card': card}, request)}
                for card in _capture_cards(captures)]

    new = list(_filtered_captures(request).filter(pk__gt=after).order_by('-pk')[:24])
    changed = []
    if watch:
        changed = list(Capture.objects.select_related('node').prefetch_related(
            Prefetch('runs', queryset=InferenceRun.objects.prefetch_related('detections'))
        ).filter(pk__in=watch).exclude(status__in=[Capture.Status.PENDING, Capture.Status.PROCESSING]))

    return JsonResponse({
        'new': render_cards(new),          # newest first
        'changed': render_cards(changed),
        'latest_id': max([after] + [c.pk for c in new]),
    })


@login_required
def capture_detail(request, pk):
    capture = get_object_or_404(Capture.objects.select_related('node'), pk=pk)
    runs = list(capture.runs.prefetch_related(Prefetch('detections', queryset=Detection.objects.select_related('zone'))))
    run = runs[0] if runs else None
    if request.GET.get('run'):
        run = next((r for r in runs if str(r.pk) == request.GET['run']), run)
    detections = list(run.detections.all()) if run else []

    zones = list(capture.node.zones.filter(is_active=True))
    zone = zones[0] if zones else None
    on_sidewalk = [d for d in detections if d.is_on_sidewalk]

    if request.method == 'POST':
        form = ReportViolationForm(request.POST, node=capture.node)
        if form.is_valid():
            data = form.cleaned_data
            violation, created = services.report_violation(
                capture, data['category'], user=request.user, zone=data['zone'],
                occupancy_ratio=data['occupancy_ratio'], blocked_width_m=data['blocked_width_m'], notes=data['notes'],
            )
            messages.success(request, _('New violation created.') if created
                             else _('Image added to the open violation for the same node and type.'))
            return redirect('dashboard:violation_detail', pk=violation.pk)
    else:
        form = ReportViolationForm(node=capture.node, initial={
            'category': services.suggest_category(on_sidewalk),
            'zone': zone,
            'occupancy_ratio': services.estimate_occupancy(zone, detections, run),
        })

    siblings = Capture.objects.filter(node=capture.node)
    image_url, redacted = _display_image(capture)
    return render(request, 'dashboard/capture_detail.html', {
        'capture': capture,
        'image_url': image_url,
        'redacted': redacted,
        'runs': runs,
        'run': run,
        'boxes': [_box(d) for d in detections],
        'auto_sidewalk': _auto_sidewalk(run),
        'on_sidewalk_count': len(on_sidewalk),
        'zones': [{'zone': z, 'points': _zone_points(z)} for z in zones],
        'violations': capture.violations.all(),
        'form': form,
        'previous': siblings.filter(captured_at__lt=capture.captured_at).order_by('-captured_at').first(),
        'next': siblings.filter(captured_at__gt=capture.captured_at).order_by('captured_at').first(),
    })


@login_required
@require_POST
def capture_requeue(request, pk):
    Capture.objects.filter(pk=pk).update(status=Capture.Status.PENDING, claimed_at=None, error='')
    messages.success(request, _('Image sent back to the detection queue.'))
    return redirect('dashboard:capture_detail', pk=pk)


# Nodes

@login_required
def node_list(request):
    nodes = list(Node.objects.annotate(
        capture_count=Count('captures', distinct=True),
        open_violations=Count('violations', filter=Q(violations__status__in=OPEN_STATUSES), distinct=True),
    ))
    map_nodes = [
        {'code': n.code, 'name': n.name, 'lat': float(n.latitude), 'lng': float(n.longitude),
         'online': n.is_online, 'url': reverse('dashboard:node_detail', args=[n.pk])}
        for n in nodes if n.latitude is not None and n.longitude is not None
    ]
    return render(request, 'dashboard/node_list.html', {'nodes': nodes, 'map_nodes': map_nodes})


@login_required
def node_detail(request, pk):
    node = get_object_or_404(Node, pk=pk)
    end = timezone.now()
    start = end - timedelta(hours=48)
    heartbeats = list(Heartbeat.objects.filter(node=node, received_at__gte=start).order_by('received_at'))
    latest = node.captures.first()
    zones = list(node.zones.all())
    return render(request, 'dashboard/node_detail.html', {
        'node': node,
        'battery_points': _battery_chart(heartbeats, start, end),
        'heartbeat_count': len(heartbeats),
        'last_heartbeat': heartbeats[-1] if heartbeats else None,
        'latest': latest,
        'latest_url': _display_image(latest)[0] if latest else None,
        'latest_sidewalk': _auto_sidewalk(latest.runs.first()) if latest else [],
        'zones': [{'zone': z, 'points': _zone_points(z)} for z in zones],
        'recent_captures': node.captures.all()[:12],
        'violations': node.violations.filter(status__in=OPEN_STATUSES)[:10],
        'stats': node.captures.aggregate(
            total=Count('id'),
            done=Count('id', filter=Q(status=Capture.Status.DONE)),
            failed=Count('id', filter=Q(status=Capture.Status.FAILED)),
            last=Max('captured_at'),
        ),
    })


@login_required
def node_form(request, pk=None):
    node = get_object_or_404(Node, pk=pk) if pk else None
    form = NodeForm(request.POST or None, instance=node)
    if request.method == 'POST' and form.is_valid():
        node = form.save()
        messages.success(request, _('Node %(code)s saved.') % {'code': node.code})
        return redirect('dashboard:node_detail', pk=node.pk)
    return render(request, 'dashboard/node_form.html', {'form': form, 'node': node})


@login_required
def zone_form(request, node_pk, pk=None):
    node = get_object_or_404(Node, pk=node_pk)
    zone = get_object_or_404(SidewalkZone, pk=pk, node=node) if pk else SidewalkZone(node=node)
    form = ZoneForm(request.POST or None, instance=zone)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, _('Sidewalk zone saved. New detections will be matched against the updated polygon.'))
        return redirect('dashboard:node_detail', pk=node.pk)
    latest = node.captures.first()
    return render(request, 'dashboard/zone_form.html', {
        'node': node,
        'zone': zone if pk else None,
        'form': form,
        'background_url': _display_image(latest)[0] if latest else None,
    })


@login_required
@require_POST
def zone_delete(request, node_pk, pk):
    zone = get_object_or_404(SidewalkZone, pk=pk, node_id=node_pk)
    zone.delete()
    messages.success(request, _('Sidewalk zone deleted.'))
    return redirect('dashboard:node_detail', pk=node_pk)


# Alerts

@login_required
def alert_list(request):
    alerts = Alert.objects.select_related('violation__node', 'sent_by')
    response = request.GET.get('response', '')
    if response:
        alerts = alerts.filter(response=response)
    return render(request, 'dashboard/alert_list.html', {
        'page': _paginate(request, alerts, 25),
        'response': response,
        'responses': Alert.Response.choices,
        'metrics': services.pilot_metrics(since=timezone.now() - timedelta(days=14)),
        'metrics_all': services.pilot_metrics(),
    })


@login_required
@require_POST
def alert_response(request, pk):
    alert = get_object_or_404(Alert, pk=pk)
    form = AlertResponseForm(request.POST)
    if form.is_valid():
        services.record_alert_response(alert, form.cleaned_data['response'], form.cleaned_data['notes'])
        messages.success(request, _('Inspector response updated.'))
    return _redirect_back(request, 'dashboard:alert_list')


# Weekly hotspots

@login_required
def hotspots(request):
    try:
        weeks_back = max(0, int(request.GET.get('week', 0)))
    except ValueError:
        weeks_back = 0
    today = timezone.localdate()
    # Weeks start on Saturday, the start of the Syrian work week.
    week_start = today - timedelta(days=(today.weekday() - 5) % 7) - timedelta(weeks=weeks_back)
    week_end = week_start + timedelta(days=7)

    violations = (
        Violation.objects
        .filter(last_seen_at__date__gte=week_start, first_seen_at__date__lt=week_end)
        .exclude(status=Violation.Status.REJECTED)
        .select_related('node')
    )
    rows = (
        violations.values('node__id', 'node__code', 'node__name')
        .annotate(count=Count('id'), total_impact=Sum('impact_score'), max_impact=Max('impact_score'),
                  repeats=Sum('repeat_count'))
        .order_by('-total_impact')
    )
    category_labels = dict(Violation.Category.choices)
    by_node = {}
    for v in violations:
        by_node.setdefault(v.node_id, set()).add(category_labels[v.category])
    hotspot_rows = [{**row, 'categories': sorted(by_node.get(row['node__id'], []))} for row in rows]

    return render(request, 'dashboard/hotspots.html', {
        'rows': hotspot_rows,
        'top_violations': violations.order_by('-impact_score')[:10],
        'week_start': week_start,
        'week_end': week_end - timedelta(days=1),
        'weeks_back': weeks_back,
    })
