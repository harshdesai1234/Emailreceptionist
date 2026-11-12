import string
import secrets
from django.db import models
from django.conf import settings
from django.contrib.auth import get_user_model


def generate_customer_id():
    """Generates a human-readable customer ID like AGT-K3M9PQ2R."""
    chars = string.ascii_uppercase + string.digits
    suffix = ''.join(secrets.choice(chars) for _ in range(8))
    return f'AGT-{suffix}'


class Organization(models.Model):
    """
    Represents a company/tenant workspace.
    """
    customer_id = models.CharField(
        max_length=20,
        unique=True,
        default=generate_customer_id,
        help_text="Unique customer identifier (e.g. AGT-K3M9PQ2R)"
    )
    name = models.CharField(max_length=255, help_text="Company name")
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='owned_organizations'
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Organization'
        verbose_name_plural = 'Organizations'

    def get_users(self):
        User = get_user_model()
        return User.objects.filter(profile__organization=self)

    def get_user_ids(self):
        return list(self.get_users().values_list('id', flat=True))

    def __str__(self):
        return f"{self.name} ({self.customer_id})"


class UserProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='profile'
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='members',
        help_text="The organization/company this user belongs to"
    )
    role = models.CharField(max_length=20, default='admin')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = 'User Profile'
        verbose_name_plural = 'User Profiles'

    def __str__(self):
        return f"Profile for {self.user.username}"
