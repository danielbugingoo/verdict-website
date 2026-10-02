from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.views.static import serve

urlpatterns = [
    path('admin/', admin.site.urls),
    # Newsletter app URLs
    path('', include('newsletter.urls')),
]

# Serve uploaded media files through Django in every environment; production
# has no separate web-server mapping for /media/.
urlpatterns += [
    re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
]