#
# Copyright 2015-2022, Institute for Systems Biology
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

import sys

from time import sleep
from builtins import map
from builtins import next
from builtins import str
from past.builtins import basestring
from builtins import object
import collections
import csv
import json
import os
import io
import traceback
import re
import datetime
import time
import logging
import math

import django
from request_logging.decorators import no_logging
from google_helpers.bigquery.cohort_support import BigQuerySupport
from google_helpers.bigquery.cohort_support import BigQueryCohortSupport
from google_helpers.bigquery.export_support import BigQueryExportFileList, FILE_LIST_EXPORT_SCHEMA
from google_helpers.stackdriver import StackDriverLogger
from google.cloud import storage
from google.auth import jwt
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.models import User as Django_User
from django.conf import settings
from django.core import serializers
from django.core.exceptions import ObjectDoesNotExist, MultipleObjectsReturned
from django.urls import reverse
from django.db.models import Count
from django.http import HttpResponse, JsonResponse
from django.http import StreamingHttpResponse
from django.shortcuts import render, redirect
from django.template.loader import get_template
from django.utils import formats
from django.views.decorators.csrf import csrf_protect, csrf_exempt
from django.utils.html import escape

from cohorts.models import Cohort, Cohort_Perms, Source, Filter, Cohort_Comments
from cohorts.utils import _save_cohort, _delete_cohort, get_cohort_uuids, _get_cohort_stats
from idc_collections.models import Program, Collection, DataSource, DataVersion, ImagingDataCommonsVersion, Attribute
from idc_collections.collex_metadata_utils import build_explorer_context, get_bq_metadata, get_bq_string, \
    create_file_manifest, build_static_map, STATIC_EXPORT_FIELDS

MAX_FILE_LIST_ENTRIES = settings.MAX_FILE_LIST_REQUEST
COHORT_CREATION_LOG_NAME = settings.COHORT_CREATION_LOG_NAME

BQ_ATTEMPT_MAX = 10

BMI_MAPPING = {
    'underweight': [0, 18.5],
    'normal weight': [18.5, 25],
    'overweight': [25, 30],
    'obese': 30
}


debug = settings.DEBUG # RO global for this file

DENYLIST_RE = settings.DENYLIST_RE
BQ_SERVICE = None

logger = logging.getLogger('main_logger')


def convert(data):
    if isinstance(data, basestring):
        return str(data)
    elif isinstance(data, collections.Mapping):
        return dict(list(map(convert, iter(list(data.items())))))
    elif isinstance(data, collections.Iterable):
        return type(data)(list(map(convert, data)))
    else:
        return data


def check_manifest_ready(request, file_name):
    client = storage.Client.from_service_account_json(settings.GOOGLE_APPLICATION_CREDENTIALS)
    bucket = client.get_bucket(settings.RESULT_BUCKET)
    blob = bucket.blob("{}/{}".format(settings.USER_MANIFESTS_FOLDER, file_name))

    return JsonResponse({"manifest_ready": blob.exists()}, status=200)


def fetch_user_manifest(request, file_name):
    client = storage.Client.from_service_account_json(settings.GOOGLE_APPLICATION_CREDENTIALS)
    bucket = client.get_bucket(settings.RESULT_BUCKET)
    blob = bucket.blob("{}/{}".format(settings.USER_MANIFESTS_FOLDER, file_name))

    response = StreamingHttpResponse(io.BytesIO(blob.download_as_bytes()))

    response['Content-Disposition'] = 'attachment; filename="{}"'.format(file_name.split("/")[-1])
    response['Content-Type'] = 'application/octet-stream'

    return response


# Given a cohort ID, fetch out the unique set of case IDs associated with those samples
def get_cases_by_cohort(cohort_id):

    cases = []

    try:
        print("TODO: get_cases_by_cohort")

        return set(cases)
    except (Exception) as e:
        logger.exception(e)


def get_cohort_stats(request, cohort_id, as_json=True):
    status = 200
    cohort_stats = {
        'PatientID': 0,
        'StudyInstanceUID': 0,
        'SeriesInstanceUID': 0,
        'filters_found': True
    }
    try:
        req = request.GET if request.GET else request.POST
        update = bool(req.get('update', "False").lower() == "true")
        old_cohort = Cohort.objects.get(id=cohort_id, active=True)
        old_cohort.perm = old_cohort.get_perm(request)

        if old_cohort.perm:
            if update:
                if len(old_cohort.inactive_attrs()) > 0:
                    cohort_stats['inactive_attr'] = list(old_cohort.inactive_attrs().values_list('name', flat=True))
                cohort_filters = {}
                cohort_filters_list = old_cohort.get_filters_as_dict(active_only=update)[0]['filters']
                cohort_attrs = old_cohort.get_attrs().values_list('name', flat=True)
                if len(cohort_filters_list) <= 0:
                    # If all of the filters from the prior version were made inactive, there will be no
                    # filters for this cohort.
                    cohort_stats['filters_found'] = False
                    if not as_json:
                        return cohort_stats
                    return JsonResponse(cohort_stats, status=status)
                for cohort in cohort_filters_list:
                    cohort_filters[cohort['name']] = cohort['values']
                # For now we always require at least one filter coming from the 'ImageData' table type,
                # so it's safe to case the sources only on the filters for purposes of stat counting
                sources = Attribute.objects.filter(name__in=list(cohort_attrs)).get_data_sources(
                    ImagingDataCommonsVersion.objects.filter(active=True),
                    source_type=DataSource.SOLR,
                    active=True,
                    aggregate_level=["case_barcode", "StudyInstanceUID", "sample_barcode"]
                )
                cohort_stats.update(_get_cohort_stats(
                    0,
                    cohort_filters,
                    sources
                ))
            else:
                cohort_stats.update({'PatientID': old_cohort.case_count, 'StudyInstanceUID': old_cohort.study_count,
                                'SeriesInstanceUID': old_cohort.series_count})

    except ObjectDoesNotExist as e:
        logger.exception(e)
        messages.error(request, 'The cohort you were looking for does not exist.')
        return redirect('cohort_list')
    except Exception as e:
        logger.error("[ERROR] Exception while trying to get cohort stats for cohort {}:".format(cohort_id))
        logger.exception(e)
        messages.error(request, "There was an error while trying to load that cohort's stats.")
        return redirect('cohort_list')

    if not as_json:
        return cohort_stats

    return JsonResponse(cohort_stats, status=status)


@login_required
def cohorts_list(request, is_public=False):
    if debug: logger.debug('Called '+sys._getframe().f_code.co_name)

    current_version = str(ImagingDataCommonsVersion.objects.get(active=True))

    cohort_perms = Cohort_Perms.objects.filter(user=request.user).values_list('cohort', flat=True)
    cohorts = Cohort.objects.filter(id__in=cohort_perms, active=True).order_by('-name')

    cohorts.has_private_cohorts = True if len(cohorts) else False
    #shared_users = {}

    for item in cohorts:
        file_parts_count = math.ceil(item.series_count / (MAX_FILE_LIST_ENTRIES if MAX_FILE_LIST_ENTRIES > 0 else 1))
        item.file_parts_count = file_parts_count
        item.display_file_parts_count = min(file_parts_count, 10)
        item.has_inactive_attr = bool(len(item.get_attrs().filter(active=False)) > 0)

    #     item.perm = item.get_perm(request).get_perm_display()
    #     item.owner = item.get_owner()
    #     shared_with_ids = Cohort_Perms.objects.filter(cohort=item, perm=Cohort_Perms.READER)
    #     .values_list('user', flat=True)
    #     item.shared_with_users = User.objects.filter(id__in=shared_with_ids)
    #     if not item.owner.is_superuser:
    #         cohorts.has_private_cohorts = True
    #         # if it is not a public cohort and it has been shared with other users
    #         # append the list of shared users to the shared_users array
    #         if item.shared_with_users and item.owner.id == request.user.id:
    #             shared_users[int(item.id)] = serializers.serialize('json', item.shared_with_users, f
    #             ields=('last_name', 'first_name', 'email'))

    previously_selected_cohort_ids = []

    return render(request, 'cohorts/cohort_list.html',
                  {'request': request,
                    'cohorts': cohorts,
                    'current_version': current_version,
                    #'shared_users':  json.dumps(shared_users),
                    'base_url': settings.BASE_URL,
                    'base_api_url': settings.BASE_API_URL,
                    'is_public': is_public,
                    'is_social': bool(len(request.user.socialaccount_set.all()) > 0),
                    'is_cohort': True,
                    'previously_selected_cohort_ids' : previously_selected_cohort_ids
                    }
                  )


@login_required
def cohort_detail(request, cohort_id):
    if debug: logger.debug('Called {}'.format(sys._getframe().f_code.co_name))

    try:
        req = request.GET if request.GET else request.POST
        is_dicofdic = (req.get('is_dicofdic', "False").lower() == "true")
        source = req.get('data_source_type', DataSource.SOLR)
        fields = json.loads(req.get('fields', '[]'))
        order_docs = json.loads(req.get('order_docs', '[]'))
        counts_only = (req.get('counts_only', "False").lower() == "true")
        with_related = (req.get('with_clinical', "True").lower() == "true")
        with_derived = (req.get('with_derived', "True").lower() == "true")
        collapse_on = req.get('collapse_on', 'SeriesInstanceUID')

        cohort = Cohort.objects.get(id=cohort_id, active=True)
        cohort.perm = cohort.get_perm(request)
        cohort.owner = cohort.get_owner()

        if not cohort.perm:
            messages.error(request, 'You do not have permission to view that cohort.')
            return redirect('cohort_list')

        shared_with_ids = Cohort_Perms.objects.filter(cohort=cohort, perm=Cohort_Perms.READER).values_list('user', flat=True)
        shared_with_users = User.objects.filter(id__in=shared_with_ids)

        cohort_filters = cohort.get_filters_as_dict()
        cohort_versions = cohort.get_data_versions()
        initial_filters = {}

        template_values = build_explorer_context(
            is_dicofdic, source, cohort_versions, initial_filters, fields, order_docs, counts_only, with_related,
            with_derived, collapse_on, False
        )

        file_parts_count = math.ceil(cohort.series_count / (MAX_FILE_LIST_ENTRIES if MAX_FILE_LIST_ENTRIES > 0 else 1))
        bq_string = get_query_string(request, cohort_id)

        template_values.update({
            'request': request,
            'base_url': settings.BASE_URL,
            'cohort': cohort,
            'shared_with_users': shared_with_users,
            'cohort_filters': cohort_filters,
            'cohort_version': "; ".join(cohort_versions.get_displays()),
            'cohort_id': cohort_id,
            'is_social': bool(len(request.user.socialaccount_set.all()) > 0),
            'cohort_file_parts_count': file_parts_count,
            'cohort_display_file_parts_count': min(file_parts_count, 10),
            'bq_string': bq_string
        })

        template = 'cohorts/cohort_details.html'
    except ObjectDoesNotExist as e:
        logger.exception(e)
        messages.error(request, 'The cohort you were looking for does not exist.')
        return redirect('cohort_list')
    except Exception as e:
        logger.error("[ERROR] Exception while trying to view a cohort:")
        logger.exception(e)
        messages.error(request, "There was an error while trying to load that cohort's details page.")
        return redirect('cohort_list')

    return render(request, template, template_values)


@login_required
@csrf_protect
def save_cohort(request):
    if debug: logger.debug('Called '+sys._getframe().f_code.co_name)

    redirect_url = reverse('cohort_list')

    try:
        if request.POST:
            name = request.POST.get('name')
            desc = request.POST.get('desc', None)
            filters = json.loads(request.POST.get('selected-filters','{}'))
            cohort_id = request.POST.get('cohort_id', None)
            req_version = request.POST.get('version', None)

            version = DataVersion.objects.get(version_number=req_version) if req_version else ImagingDataCommonsVersion.objects.get(active=True)

            result = _save_cohort(request.user, filters, name, cohort_id, version, desc=desc)

            if 'message' not in result:
                st_logger = StackDriverLogger.build_from_django_settings()
                log_name = COHORT_CREATION_LOG_NAME
                user = User.objects.get(id=request.user.id)
                st_logger.write_text_log_entry(
                    log_name,
                    "[COHORT CREATION] User {} created a new cohort at {}".format(user.email, datetime.datetime.utcnow())
                )
                redirect_url = reverse('cohort_details', args=[result['cohort_id']])
                messages.info(request, 'Cohort {} {} successfully.'.format(name, 'created' if not cohort_id else 'updated'))
            else:
                messages.error(request, result['message'])

    except Exception as e:
        messages.error(request, "There was an error saving your cohort; it may not have been saved correctly.")
        logger.error('[ERROR] Exception while saving a cohort:')
        logger.exception(e)

    return redirect(redirect_url)


@login_required
@csrf_protect
def delete_cohort(request):
    if debug: logger.debug('Called ' + sys._getframe().f_code.co_name)
    try:
        redirect_url = 'cohort_list'
        cohort_ids = request.POST.getlist('id')
        for cohort_id in cohort_ids:
            result = _delete_cohort(request.user, cohort_id)
            if 'message' in result:
                logger.error("[ERROR] {}".format(result['message']))
                messages.error(request, result['message'])
            else:
                messages.info(request, result['notes'])
    except Exception as e:
        logger.error("[ERROR] While deleting cohort: ")
        logger.exception(e)
            
    return redirect(reverse(redirect_url))


@login_required
@csrf_protect
def cohort_filelist(request, cohort_id=0, panel_type=None):
    if debug: logger.debug('Called '+sys._getframe().f_code.co_name)

    template = 'cohorts/cohort_filelist{}.html'.format("_{}".format(panel_type) if panel_type else "")

    if cohort_id == 0:
        messages.error(request, 'Cohort requested does not exist.')
        return redirect('/user_landing')

    try:
        metadata_data_attr_builds = {
            'HG19': fetch_build_data_attr('HG19', panel_type),
            'HG38': fetch_build_data_attr('HG38', panel_type)
        }

        build = request.GET.get('build', 'HG19')

        metadata_data_attr = metadata_data_attr_builds[build]

        has_access = auth_dataset_whitelists_for_user(request.user.id)

        items = None

        if panel_type:
            inc_filters = json.loads(request.GET.get('filters', '{}')) if request.GET else json.loads(
                request.POST.get('filters', '{}'))
            if request.GET.get('case_barcode', None):
                inc_filters['case_barcode'] = ["%{}%".format(request.GET.get('case_barcode')),]
            items = cohort_files(cohort_id, inc_filters=inc_filters, user=request.user, build=build, access=has_access, type=panel_type)

            for attr in items['metadata_data_counts']:
                for val in items['metadata_data_counts'][attr]:
                    metadata_data_attr[attr]['values'][val]['count'] = items['metadata_data_counts'][attr][val]
                metadata_data_attr[attr]['values'] = [metadata_data_attr[attr]['values'][x] for x in metadata_data_attr[attr]['values']]

        for attr_build in metadata_data_attr_builds:
            if attr_build != build:
                for attr in metadata_data_attr_builds[attr_build]:
                    for val in metadata_data_attr_builds[attr_build][attr]['values']:
                        metadata_data_attr_builds[attr_build][attr]['values'][val]['count'] = 0
                    metadata_data_attr_builds[attr_build][attr]['values'] = [metadata_data_attr_builds[attr_build][attr]['values'][x] for x in
                                                                             metadata_data_attr_builds[attr_build][attr]['values']]
            metadata_data_attr_builds[attr_build] = [metadata_data_attr_builds[attr_build][x] for x in metadata_data_attr_builds[attr_build]]

        cohort = Cohort.objects.get(id=cohort_id, active=True)
        cohort.perm = cohort.get_perm(request)

        # Check if cohort contains user data samples - return info message if it does.
        # Get user accessed projects
        user_projects = Project.get_user_projects(request.user)
        cohort_sample_list = Samples.objects.filter(cohort=cohort, project__in=user_projects)
        if cohort_sample_list.count():
            messages.info(
                request,
                "File listing is not available for cohort samples that come from a user uploaded project. " +
                "This functionality is currently being worked on and will become available in a future release."
            )

        logger.debug("[STATUS] Returning response from cohort_filelist")

        return render(request, template, {'request': request,
                                            'cohort': cohort,
                                            'total_file_count': (items['total_file_count'] if items else 0),
                                            'download_url': reverse('download_filelist', kwargs={'cohort_id': cohort_id}),
                                            'export_url': reverse('export_data', kwargs={'cohort_id': cohort_id, 'export_type': 'file_manifest'}),
                                            'metadata_data_attr': metadata_data_attr_builds,
                                            'file_list': (items['file_list'] if items else []),
                                            'file_list_max': MAX_FILE_LIST_ENTRIES,
                                            'sel_file_max': MAX_SEL_FILES,
                                            'img_thumbs_url': settings.IMG_THUMBS_URL,
                                            'has_user_data': bool(cohort_sample_list.count() > 0),
                                            'build': build,
                                            'programs_this_cohort': cohort.get_program_names()})

        logger.debug("[STATUS] Returning response from cohort_filelist, with exception")

    except Exception as e:
        logger.error("[ERROR] While trying to view the cohort file list: ")
        logger.exception(e)
        messages.error(request, "There was an error while trying to view the file list. Please contact the administrator for help.")
        return redirect(reverse('cohort_details', args=[cohort_id]))


@login_required
@csrf_protect
def cohort_uuids(request, cohort_id=0):
    if cohort_id == 0:
        messages.error(request, 'Cohort provided is invalid.')
        return redirect('cohort_list')

    try:
        cohort_name = Cohort.objects.get(id=cohort_id).name

        rows = ["UUIDs for Cohort {} ({})".format(str(cohort_id, cohort_name))]

        rows.append(get_cohort_uuids(cohort_id))

        pseudo_buffer = Echo()
        writer = csv.writer(pseudo_buffer)
        response = StreamingHttpResponse((writer.writerow(row) for row in rows),
                                         content_type="text/csv")
        response['Content-Disposition'] = 'attachment; filename="uuids_in_cohort_{}.csv"'.format(str(cohort_id))

    except ObjectDoesNotExist:
        messages.error(request, "A cohort with the ID {} was not found.".format(str(cohort_id)))
        response = redirect('cohort_list')
    except Exception as e:
        logger.error("[ERROR] While trying to download a list of samples and cases for cohort {}:".format(str(cohort_id)))
        logger.exception(e)
        messages.error(request, "There was an error while attempting to obtain the list of samples and cases for cohort ID {}. Please contact the administrator.".format(str(cohort_id)))
        response = redirect('cohort_list')

    return response


@login_required
def create_manifest_bq_table(request, cohorts):
    response = None
    tables = None
    req = request.GET or request.POST
    try:
        timestamp = datetime.datetime.fromtimestamp(time.time()).strftime('%Y%m%d_%H%M%S')

        order_by = ["PatientID", "collection_id", "source_DOI", "StudyInstanceUID", "SeriesInstanceUID",
                    "SOPInstanceUID", "crdc_study_uuid", "crdc_series_uuid", "crdc_instance_uuid", "gcs_url"]
        field_list = json.loads(req.get(
            'columns',
            '["PatientID", "collection_id", "source_DOI", "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID", ' +
            '"crdc_study_uuid", "crdc_series_uuid", "crdc_instance_uuid", "gcs_url", "idc_version"]'
        ))

        # We can only ORDER BY columns which we've actually requested
        order_by = list(set.intersection(set(order_by), set(field_list)))

        all_results = {}
        export_jobs = {}
        static_fields = None

        table_schema = {'fields': [x for x in FILE_LIST_EXPORT_SCHEMA['fields'] if x['name'] in field_list]} \
            if len(field_list) < len(FILE_LIST_EXPORT_SCHEMA['fields']) else None

        for cohort in cohorts:
            static_map = build_static_map(cohort)
            cohort_version = "; ".join([str(x) for x in cohort.get_idc_data_version()])
            desc = None
            headers = []
            if req.get('header_fields'):
                selected_header_fields = json.loads(req.get('header_fields'))
                'cohort_name' in selected_header_fields and headers.append("Manifest for cohort '{}' ID#{}".format(cohort.name, cohort.id))
                'user_email' in selected_header_fields and headers.append("User: {}".format(request.user.email))
                'cohort_filters' in selected_header_fields and headers.append("Filters: {}".format(cohort.get_filter_display_string()))
            headers.append("IDC Data Version(s): {}".format(cohort_version))
            desc = "\n".join(headers)

            base_filters = cohort.get_filters_as_dict_simple()[0]
            if 'bmi' in base_filters:
                vals = base_filters['bmi']
                del base_filters['bmi']
                for val in vals:
                    if val not in ('None','obese'):
                        if 'bmi_btw' not in base_filters:
                            base_filters['bmi_btw'] = []
                        base_filters['bmi_btw'].append(BMI_MAPPING[val])
                    elif val == 'obese':
                        base_filters['bmi_gt'] = BMI_MAPPING[val]
                    else:
                        base_filters['bmi'] = 'None'

            table_name = "manifest_cohort_{}_{}".format(str(cohort.id), timestamp)
            export_jobs[cohort.id] = {
                'table_id': '{}.{}.{}'.format(settings.BIGQUERY_USER_DATA_PROJECT_ID,
                                              settings.BIGQUERY_USER_MANIFEST_DATASET,
                                              table_name)
            }
            for x in STATIC_EXPORT_FIELDS:
                if x in field_list:
                    static_fields = static_fields or {}
                    static_fields[x] = static_map[x]
                    field_list.remove(x)

            query = get_bq_metadata(
                base_filters, field_list, cohort.get_data_versions(),
                order_by=order_by, no_submit=True,
                search_child_records_by="StudyInstanceUID", static_fields=static_fields
            )
            export_jobs[cohort.id]['bqs'] = BigQueryExportFileList(**{
                'project_id': settings.BIGQUERY_USER_DATA_PROJECT_ID,
                'dataset_id': settings.BIGQUERY_USER_MANIFEST_DATASET,
                'table_id': table_name,
                'schema': table_schema
            })
            export_jobs[cohort.id]['job_id'] = export_jobs[cohort.id]['bqs'].export_file_list_query_to_bq(
                query['sql_string'], query['params'],
                cohort.id,
                user_email=request.user.email,
                desc=desc or None,
                for_batch=True
            )

        not_done = True
        still_checking = True
        num_retries = 0

        while still_checking and not_done:
            not_done = False
            for cohort in export_jobs:
                if not BigQuerySupport.check_job_is_done({'jobReference': {'jobId': export_jobs[cohort]['job_id']}}):
                    not_done = True
                else:
                    if cohort not in all_results:
                        all_results[cohort] = export_jobs[cohort]['bqs'].check_query_to_table_done(
                            export_jobs[cohort]['job_id'],"cohort file manifest",False
                        )
                        all_results[cohort]['table_id'] = export_jobs[cohort]['table_id'] \
                            if all_results[cohort]['status'] == 'error' \
                            else all_results[cohort]['full_table_id']
            if not_done:
                sleep(1)
                num_retries += 1
                still_checking = (num_retries < settings.BQ_MAX_ATTEMPTS)

        if not_done:
            logger.warning("[WARNING] Not all of the queries completed!")

        for cohort in export_jobs:
            if not all_results.get(cohort,None):
                all_results[cohort] = {
                    'status': 'long_running',
                    'table_id': export_jobs[cohort]['table_id']
                }

        errors = {x: all_results[x]['message'] for x in all_results if all_results[x]['status'] == 'error'}

        if bool(len(errors) > 0):
            response = JsonResponse({'status': 400, 'message': "<br />".join(["Cohort ID {}: {}".format(x,errors[x]) for x in errors])})
        else:
            msg_template = get_template('cohorts/bq-manifest-export-msg.html')
            msg = msg_template.render(context={
                'tables': [{
                    'full_id':  all_results[x]['table_id'],
                    'uri': "https://console.cloud.google.com/bigquery?p={}&d={}&t={}&page=table".format(
                        settings.BIGQUERY_USER_DATA_PROJECT_ID,
                        settings.BIGQUERY_USER_MANIFEST_DATASET,
                        all_results[x]['table_id'].split('.')[-1]
                    ),
                    'error': all_results[x]['status'] == 'error'} for x in all_results],
                'long_running': bool(len([x for x in all_results if all_results[x]['status'] == 'long_running']) > 0),
                'errors': bool(len([x for x in all_results if all_results[x]['status'] == 'error']) > 0),
                'email': request.user.email
            })
            response = JsonResponse({
                'status': 200,
                'message': msg
            })
            tables = { x: all_results[x]['table_id'] for x in all_results }
    except Exception as e:
        logger.error("[ERROR] While exporting cohort to BQ:")
        logger.exception(e)
        response = JsonResponse({
            'status': 500,
            'message': "There was an error exporting your cohort to BigQuery. Please contact the administrator."
        })

    return response, tables


@login_required
def download_cohort_manifest(request, cohort_id=0):
    try:
        cohort_ids = []
        req = request.GET or request.POST
        if cohort_id:
            cohort_ids = [cohort_id]
        else:
            cohort_ids = [int(x) for x in req.get("ids", "").split(",")]

        if not len(cohort_ids):
            messages.error(request, "A cohort ID was not provided.")
            return redirect('cohort_list')

        try:
            cohorts = Cohort.objects.filter(id__in=cohort_ids)
            tables = None
            for cohort in cohorts:
                Cohort_Perms.objects.get(cohort=cohort, user=request.user)

            if req.get('manifest-type', 'file-manifest') == 'bq-manifest':
                response, tables = create_manifest_bq_table(request, cohorts)
            else:
                response = create_file_manifest(request, cohorts.first())
            if not response:
                raise Exception("Response from manifest creation was None!")

            if req.get('manifest-type','file-manifest') == 'bq-manifest':
                for cohort in cohorts:
                    cohort.last_exported_date = datetime.datetime.utcnow()
                    cohort.last_exported_table = tables[cohort.id]
                    cohort.save()
            return response
        except ObjectDoesNotExist:
            logger.error("[ERROR] User ID {} attempted to access one or more of these cohorts, " +
                         "which they do not have permission to view: {}".format(request.user.id, cohort_ids.join("; ")))
            messages.error(request,"You don't have permission to view one or more of these cohorts.")

    except Exception as e:
        logger.error("[ERROR] While creating the cohort manifest(s) for user {}:".format(str(request.user.email)))
        logger.exception(e)
        messages.error(request,"There was an error while attempting to obtain your cohort manifest(s)--please contact the administrator.")

    if cohort_id:
        return redirect(reverse('cohort_details', kwargs={'cohort_id': cohort_id}))

    return redirect('cohort_list')


def get_query_str_response(request, cohort_id=0):
    response = {
        'status': 200,
        'msg': ''
    }
    status = 200

    req = request.GET or request.POST

    try:
        query = get_query_string(request, cohort_id)

        response['data'] = {'query_string': query, 'cohort': cohort_id}
        response['msg'] = "{} BigQuery string enclosed.".format("Cohort" if cohort_id else "Filter")

        if bool(req.get('update', "False").lower() == "true"):
            stats = get_cohort_stats(request, cohort_id, False)
            response['filters_found'] = stats.get('filters_found', None)
            response['inactive_attr'] = stats.get('inactive_attr', None)
            response['PatientID'] = stats.get('PatientID', 0)

    except Exception as e:
        logger.error("[ERROR] While fetching BQ string for {}:".format(cohort_id if cohort_id else filters))
        logger.exception(e)
        messages.error(request, "There was an error obtaining this BQ string. Please contact the administrator.")
        response = {
            'status': 500,
            'msg': "There was an error obtaining this BQ string. Please contact the administrator."
        }
        status = 500

    return JsonResponse(response, status=status)


def get_query_string(request, cohort_id=0):
    try:
        req = request.POST or request.GET
        filters = json.loads(req.get('filters', None) or '{}')
        version = req.get('version', None)

        if not cohort_id and not filters:
            raise Exception("Cannot provide query string without a cohort ID or filters!"
                            + "Please provide a valid cohort ID or filter set.")

        if cohort_id:
            cohort = Cohort.objects.get(id=cohort_id, active=True)
            cohort.perm = cohort.get_perm(request)
            cohort.owner = cohort.get_owner()

            if not cohort.perm:
                messages.error(request, "You do not have permission to view that cohort's string.")
                return JsonResponse({
                    'status': 400,
                    'message': "You do not have permission to view that cohort's string."
                }, status=400)

            filters = cohort.get_filters_as_dict_simple()[0]
            version = cohort.get_data_versions()

        field_list = ["PatientID", "collection_id", "source_DOI", "StudyInstanceUID",
                      "SeriesInstanceUID", "SOPInstanceUID", "gcs_url"]

        if 'bmi' in filters:
            vals = filters['bmi']
            del filters['bmi']
            for val in vals:
                if val not in ('None','obese'):
                    if 'bmi_btw' not in filters:
                        filters['bmi_btw'] = []
                    filters['bmi_btw'].append(BMI_MAPPING[val])
                elif val == 'obese':
                    filters['bmi_gt'] = BMI_MAPPING[val]
                else:
                    filters['bmi'] = 'None'

        query = get_bq_string(
            filters, field_list, version, order_by=field_list, search_child_records_by=True
        )

    except Exception as e:
        logger.error("[ERROR] While fetching BQ string for {}:".format(cohort_id if cohort_id else filters))
        logger.exception(e)

    return query


@login_required
def get_metadata(request):
    filters = json.loads(request.GET.get('filters', '{}'))
    comb_mut_filters = request.GET.get('mut_filter_combine', 'OR')
    cohort = request.GET.get('cohort_id', None)
    limit = request.GET.get('limit', None)
    program_id = request.GET.get('program_id', None)

    program_id = int(program_id) if program_id is not None else None

    user = Django_User.objects.get(id=request.user.id)

    if program_id is not None and program_id > 0:
        results = public_metadata_counts(filters[str(program_id)], cohort, user, program_id, limit, comb_mut_filters=comb_mut_filters)

        # If there is an extent cohort, to get the cohort's new totals per applied filters
        # we have to check the unfiltered programs for their numbers and tally them in
        # This includes user data!
        if cohort:
            results['cohort-total'] = results['total']
            results['cohort-cases'] = results['cases']
            cohort_pub_progs = Program.objects.filter(id__in=Collection.objects.filter(id__in=Samples.objects.filter(cohort_id=cohort).values_list('project_id',flat=True).distinct()).values_list('program_id',flat=True).distinct(), is_public=True)
            for prog in cohort_pub_progs:
                if prog.id != program_id:
                    prog_res = public_metadata_counts(filters[str(prog.id)], cohort, user, prog.id, limit)
                    results['cohort-total'] += prog_res['total']
                    results['cohort-cases'] += prog_res['cases']

            cohort_user_progs = Program.objects.filter(id__in=Collection.objects.filter(id__in=Samples.objects.filter(cohort_id=cohort).values_list('project_id',flat=True).distinct()).values_list('program_id', flat=True).distinct(), is_public=False)
            for prog in cohort_user_progs:
                user_prog_res = user_metadata_counts(user, {'0': {'user_program', [prog.id]}}, cohort)
                results['cohort-total'] += user_prog_res['total']
                results['cohort-cases'] += user_prog_res['cases']
    else:
        results = user_metadata_counts(user, filters, cohort)

    if not results:
        results = {}

    return JsonResponse(results)

