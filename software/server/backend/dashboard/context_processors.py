from monitoring.models import Alert, Violation


def nav_counts(request):
    """Badges in the sidebar: work waiting for the team."""
    if not request.user.is_authenticated or request.resolver_match is None \
            or request.resolver_match.app_name != 'dashboard':
        return {}
    return {
        'nav_awaiting_review': Violation.objects.filter(status=Violation.Status.DETECTED).count(),
        'nav_alerts_open': Alert.objects.filter(response=Alert.Response.NONE).count(),
    }
