from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('accounts.urls', namespace='accounts')),
    path('ai-receptionist/email/', include('email_receptionist.urls', namespace='email_receptionist')),
    path('', RedirectView.as_view(url='/ai-receptionist/email/', permanent=False)),
]
