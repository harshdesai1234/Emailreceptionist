import logging
from django.contrib.auth import get_user_model

logger = logging.getLogger(__name__)


class OrganizationMiddleware:
    """
    Attaches `request.organization` and `request.org_user_ids` to every authenticated request.
    If an authenticated user has no organization yet, automatically provisions a default one.
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        self._attach_org(request)
        return self.get_response(request)

    def _attach_org(self, request):
        request.organization = None
        request.org_user_ids = []

        if not request.user.is_authenticated:
            return

        from accounts.models import Organization, UserProfile

        try:
            profile, _ = UserProfile.objects.get_or_create(user=request.user)
            org = profile.organization

            if not org or not org.is_active:
                # Find an owned organization or create a default one
                org = Organization.objects.filter(owner=request.user, is_active=True).first()
                if not org:
                    org_name = f"{request.user.username.capitalize()}'s Workspace"
                    org = Organization.objects.create(name=org_name, owner=request.user)
                profile.organization = org
                profile.save(update_fields=['organization'])

            request.organization = org
            request.org_user_ids = org.get_user_ids()
        except Exception as exc:
            logger.warning(f"OrganizationMiddleware fallback: {exc}")
            request.org_user_ids = [request.user.id]
