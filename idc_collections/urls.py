from __future__ import absolute_import
from django.urls import include, re_path, path

from . import views

urlpatterns = [
    re_path(r'^$', views.collection_list, name='collections'),
    #re_path(r'^(?P<collection_id>\d+)/$', views.collection_detail, name='collection_detail'),
    re_path(r'^api/versions/$', views.views_api_v1.versions_list_api, name='versions_list_api'),
    re_path(r'^api/v1/versions/$', views.views_api_v1.versions_list_api, name='versions_list_api'),
    re_path(r'^api/v2/versions/$', views.views_api_v2.versions_list_api, name='versions_list_api'),
    re_path(r'^api/$', views.views_api_v1.collections_list_api, name='collections_list_api'),
    re_path(r'^api/v1/$', views.views_api_v1.collections_list_api, name='collections_list_api'),
    re_path(r'^api/v2/$', views.views_api_v2.collections_list_api, name='collections_list_api'),
    # re_path(r'^api/data_sources/$', views.data_sources_list_api, name='data_sources_list_api'),
    re_path(r'^api/attributes/$', views.views_api_v1.attributes_list_api, name='attributes_list_api'),
    re_path(r'^api/v1/attributes/$', views.views_api_v1.attributes_list_api, name='attributes_list_api'),
    re_path(r'^api/v2/attributes/$', views.views_api_v2.attributes_list_api, name='attributes_list_api'),
    re_path(r'^api/v2/fields/(?P<version>[a-zA-Z0-9. ]+)$', views.views_api_v2.queryfields_list_api, name='queryfields_list_api'),
    re_path(r'^api/analysis_results/$', views.views_api_v1.analysis_results_list_api, name='analysis_results_list_api'),
    re_path(r'^api/v1/analysis_results/$', views.views_api_v1.analysis_results_list_api, name='analysis_results_list_api'),
    re_path(r'^api/v2/analysis_results/$', views.views_api_v2.analysis_results_list_api, name='analysis_results_list_api'),
    # re_path(r'^api/programs/$', views.public_program_list_api, name='public_programs_api'),
    # re_path(r'^api/programs/(?P<program_name>[A-Za-z0-9_-]+)/$', views.program_detail_api, name='program_detail_api'),
    # re_path(r'^api/(?P<idc_version>[A-Za-z0-9_-]+)/$', views.collections_list_api, name='collections_api'),
]