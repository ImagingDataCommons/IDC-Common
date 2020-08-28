from __future__ import absolute_import
from django.conf.urls import url

from . import views

urlpatterns = [
    url(r'^$', views.collection_list, name='collections'),
    #url(r'^(?P<collection_id>\d+)/$', views.collection_detail, name='collection_detail'),
    url(r'^public/$', views.public_program_list, name='public_programs'),
    url(r'^api/public/$', views.public_program_list_api, name='public_programs_api'),
    url(r'^api/versions/$', views.versions_list_api, name='versions_list_api'),
    url(r'^api/attributes/$', views.attributes_list_api, name='attributes_list_api'),
    url(r'^api/(?P<program_name>[A-Za-z0-9_-]+)/$', views.program_detail_api, name='program_detail_api'),
    url(r'^api/(?P<program_name>[A-Za-z0-9_-]+)/(?P<collection_name>[A-Za-z0-9_-]+)/$', views.collection_detail_api, name='collection_detail_api'),
]