from django.contrib import admin
from accounts.models import Organization, UserProfile


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ('name', 'customer_id', 'owner', 'is_active', 'created_at')
    search_fields = ('name', 'customer_id', 'owner__username', 'owner__email')
    list_filter = ('is_active', 'created_at')


@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'organization', 'role', 'created_at')
    search_fields = ('user__username', 'user__email', 'organization__name')
    list_filter = ('role',)
