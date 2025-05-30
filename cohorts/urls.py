# 
# Copyright 2015-2019, Institute for Systems Biology
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#    http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# 
from __future__ import absolute_import

from django.urls import include, re_path, path
from . import views


urlpatterns = [
    re_path(r'^$', views.cohorts_list, name='cohort_list'),
    re_path(r'^manifests/fetch/(?P<file_name>[A-Za-z\-0-9]+\/manifest_(cohort_)?[0-9_]+(aws|gcs|gcp)?\.(s5cmd|json|csv|tsv))', views.fetch_user_manifest, name='fetch_user_manifest'),
    re_path(r'^manifests/check/(?P<file_name>[A-Za-z\-0-9]+\/manifest_(cohort_)?[0-9_]+(aws|gcs|gcp)?\.(s5cmd|json|csv|tsv))', views.check_manifest_ready, name='check_user_manifest'),
    re_path(r'^manifests/fetch/$', views.fetch_user_manifest, name='fetch_user_manifest_base'),
    re_path(r'^manifests/check/$', views.check_manifest_ready, name='check_user_manifest_base'),
    re_path(r'^api/$', views.views_api_v1.cohort_list_api, name='cohort_list_api'),
    re_path(r'^api/v1/$', views.views_api_v1.cohort_list_api, name='cohort_list_api'),
    re_path(r'^api/v2/$', views.views_api_v2.cohort_list_api, name='cohort_list_api'),
    #    re_path(r'^api/preview/$', views.cohort_preview_api, name='cohort_preview_api'),
    re_path(r'^api/preview/manifest/$', views.views_api_v1.cohort_preview_manifest_api, name='cohort_preview_manifest_api'),
    re_path(r'^api/v1/preview/manifest/$', views.views_api_v1.cohort_preview_manifest_api, name='cohort_preview_manifest_api'),
    # re_path(r'^api/v2/preview/manifest/$', views.views_api_v2.cohort_preview_manifest_api, name='cohort_preview_manifest_api'),
    re_path(r'^api/preview/query/$', views.views_api_v1.cohort_preview_query_api, name='cohort_preview_query_api'),
    re_path(r'^api/v1/preview/query/$', views.views_api_v1.cohort_preview_query_api, name='cohort_preview_query_api'),
    re_path(r'^api/v2/preview/query/$', views.views_api_v2.cohort_preview_query_api, name='cohort_preview_query_api'),
    re_path(r'^(?P<cohort_id>\d+)/$', views.cohort_detail, name='cohort_details'),
#    re_path(r'^api/(?P<cohort_id>\d+)/$', views.cohort_detail_api, name='cohort_detail_api'),
    re_path(r'^(?P<cohort_id>\d+)/stats/$', views.get_cohort_stats, name='cohort_stats'),

#    re_path(r'^api/(?P<cohort_id>\d+)/$', views.cohort_detail_api, name='cohort_detail_api'),
    re_path(r'^api/(?P<cohort_id>\d+)/manifest/$', views.views_api_v1.cohort_manifest_api, name='cohort_manifest_api'),
    re_path(r'^api/v1/(?P<cohort_id>\d+)/manifest/$', views.views_api_v1.cohort_manifest_api, name='cohort_manifest_api'),
    # re_path(r'^api/v2/(?P<cohort_id>\d+)/manifest/$', views.views_api_v2.cohort_manifest_api, name='cohort_manifest_api'),
    re_path(r'^api/(?P<cohort_id>\d+)/query/$', views.views_api_v1.cohort_query_api, name='cohort_query_api'),
    re_path(r'^api/v1/(?P<cohort_id>\d+)/query/$', views.views_api_v1.cohort_query_api, name='cohort_query_api'),
    re_path(r'^api/v2/(?P<cohort_id>\d+)/query/$', views.views_api_v2.cohort_query_api, name='cohort_query_api'),
    re_path(r'^save_cohort/', views.save_cohort, name='save_cohort'),
    re_path(r'^api/save_cohort/', views.views_api_v1.save_cohort_api, name='save_cohort_api'),
    re_path(r'^api/v1/save_cohort/', views.views_api_v1.save_cohort_api, name='save_cohort_api'),
    re_path(r'^api/v2/save_cohort/', views.views_api_v2.save_cohort_api, name='save_cohort_api'),
    re_path(r'^delete_cohort/', views.delete_cohort, name='delete_cohort'),
    re_path(r'^api/delete_cohort/', views.views_api_v1.delete_cohort_api, name='delete_cohort_api'),
    re_path(r'^api/v1/delete_cohort/', views.views_api_v1.delete_cohort_api, name='delete_cohort_api'),
    re_path(r'^api/v2/delete_cohort/', views.views_api_v2.delete_cohort_api, name='delete_cohort_api'),
    #re_path(r'^clone_cohort/(?P<cohort_id>\d+)/', views.clone_cohort, name='clone_cohort'),
    #re_path(r'^share_cohort/$', views.share_cohort, name='share_cohorts'),
    #re_path(r'^share_cohort/(?P<cohort_id>\d+)/', views.share_cohort, name='share_cohort'),
    #re_path(r'^unshare_cohort/$', views.unshare_cohort, name='unshare_cohorts'),
    #re_path(r'^unshare_cohort/(?P<cohort_id>\d+)/', views.unshare_cohort, name='unshare_cohort'),
    #re_path(r'^set_operation/', views.set_operation, name='set_operation'),
    #re_path(r'^save_cohort_comment/', views.save_comment, name='save_cohort_comment'),
    re_path(r'^download_manifest/(?P<cohort_id>\d+)/', views.download_cohort_manifest, name='cohort_manifest'),
    re_path(r'bq_string/(?P<cohort_id>\d+)/', views.get_query_str_response, name='bq_string'),
    re_path(r'^download_manifest/', views.download_cohort_manifest, name='cohort_manifest_base'),
    re_path(r'^get_metadata_ajax/$', views.get_metadata, name='metadata_count_ajax')
]
