"""

Copyright 2015, Institute for Systems Biology

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

"""

import json
import collections
import csv
import sys
import random
import string
import time
import logging
import json
import traceback
import copy
import urllib
import re
import MySQLdb

from django.utils import formats
from django.shortcuts import render, redirect
from django.http import HttpResponse, JsonResponse
from django.core.urlresolvers import reverse
from django.core.exceptions import ObjectDoesNotExist
from django.views.decorators.csrf import csrf_protect
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.conf import settings
from django.db.models import Count, Sum
import django

from django.http import StreamingHttpResponse
from django.core import serializers
from google.appengine.api import urlfetch
from allauth.socialaccount.models import SocialToken, SocialAccount
from django.contrib.auth.models import User as Django_User

from models import Cohort, Patients, Samples, Cohort_Perms, Source, Filters, Cohort_Comments
from workbooks.models import Workbook, Worksheet, Worksheet_plot
from projects.models import Project, Study, User_Feature_Counts, User_Feature_Definitions, User_Data_Tables
from visualizations.models import Plot_Cohorts, Plot
from bq_data_access.cohort_bigquery import BigQueryCohortSupport
from accounts.models import NIH_User

from api.api_helpers import *
from api.metadata import METADATA_SHORTLIST
from api.metadata import MUTATION_SHORTLIST

# For database values which have display names which are needed by templates but not stored directly in the dsatabase
DISPLAY_NAME_DD = {
    'SampleTypeCode': {
        '01': 'Primary Solid Tumor',
        '02': 'Recurrent Solid Tumor',
        '03': 'Primary Blood Derived Cancer - Peripheral Blood',
        '04': 'Recurrent Blood Derived Cancer - Bone Marrow',
        '05': 'Additional - New Primary',
        '06': 'Metastatic',
        '07': 'Additional Metastatic',
        '08': 'Human Tumor Original Cells',
        '09': 'Primary Blood Derived Cancer - Bone Marrow',
        '10': 'Blood Derived Normal',
        '11': 'Solid Tissue Normal',
        '12': 'Buccal Cell Normal',
        '13': 'EBV Immortalized Normal',
        '14': 'Bone Marrow Normal',
        '20': 'Control Analyte',
        '40': 'Recurrent Blood Derived Cancer - Peripheral Blood',
        '50': 'Cell Lines',
        '60': 'Primary Xenograft Tissue',
        '61': 'Cell Line Derived Xenograft Tissue',
        'None': 'N/A'
    },
    'Somatic_Mutations': {
        'Missense_Mutation': 'Missense Mutation',
        'Frame_Shift_Ins': 'Frame Shift: Insertion',
        'Frame_Shift_Del': 'Frame Shift: Deletion',
        'Nonsense_Mutation': 'Nonsense Mutation',
        'Splice_Site': 'Splice Site',
        'Silent': 'Silent',
        'RNA': 'RNA',
        'Intron': 'Intron',
    }
}

debug = settings.DEBUG # RO global for this file
urlfetch.set_default_fetch_deadline(60)

MAX_FILE_LIST_ENTRIES = settings.MAX_FILE_LIST_REQUEST
MAX_SEL_FILES = settings.MAX_FILES_IGV

logger = logging.getLogger(__name__)

# Get a set of random characters of 'length'
def make_id(length):
    return ''.join(random.sample(string.ascii_lowercase, length))

# Database connection - does not check for AppEngine
def get_sql_connection():
    database = settings.DATABASES['default']
    try:
        connect_options = {
            'host': database['HOST'],
            'db': database['NAME'],
            'user': database['USER'],
            'passwd': database['PASSWORD']
        }

        if 'OPTIONS' in database and 'ssl' in database['OPTIONS']:
            connect_options['ssl'] = database['OPTIONS']['ssl']

        db = MySQLdb.connect(**connect_options)

        return db

    except:
        print >> sys.stderr, "[ERROR] Exception in get_sql_connection(): ", sys.exc_info()[0]


def convert(data):
    if isinstance(data, basestring):
        return str(data)
    elif isinstance(data, collections.Mapping):
        return dict(map(convert, data.iteritems()))
    elif isinstance(data, collections.Iterable):
        return type(data)(map(convert, data))
    else:
        return data

USER_DATA_ON = settings.USER_DATA_ON
BIG_QUERY_API_URL = settings.BASE_API_URL + '/_ah/api/bq_api/v1'
COHORT_API = settings.BASE_API_URL + '/_ah/api/cohort_api/v1'
METADATA_API = settings.BASE_API_URL + '/_ah/api/meta_api/'
# This URL is not used : META_DISCOVERY_URL = settings.BASE_API_URL + '/_ah/api/discovery/v1/apis/meta_api/v1/rest'


def get_filter_values():
    db = get_sql_connection()
    cursor = None

    filter_values = {}

    get_filter_vals = 'SELECT DISTINCT %s FROM metadata_samples;'

    try:
        cursor = db.cursor()

        for attr in METADATA_SHORTLIST:
            # We only want the values of columns which are not numeric ranges and not true/false
            if not attr.startswith('has_') and not attr == 'bmi' and not attr.startswith('age_'):
                cursor.execute(get_filter_vals % attr)
                filter_values[attr] = ()
                for row in cursor.fetchall():
                    filter_values[attr] += (row[0] if row[0] is not None else 'None',)

        return filter_values

    except Exception as e:
        print traceback.format_exc()
    finally:
        if cursor: cursor.close()
        if db and db.open: db.close()

''' Begin metadata counting methods '''


# TODO: needs to be refactored to use other samples tables
def get_participant_and_sample_count(filter="", cohort_id=None):

    db = get_sql_connection()
    cursor = None
    counts = {}

    try:
        cursor = db.cursor(MySQLdb.cursors.DictCursor)

        param_tuple = ()

        query_str_lead = "SELECT COUNT(DISTINCT %s) AS %s "

        if cohort_id is not None:
            query_str = "FROM cohorts_samples cs JOIN metadata_samples ms ON ms.SampleBarcode = cs.sample_id "
            query_str += "WHERE cs.cohort_id = %s "
            param_tuple += (cohort_id,)
        else:
            query_str = "FROM metadata_samples ms "

        if filter.__len__() > 0:
            where_clause = build_where_clause(filter)
            query_str += "WHERE " if cohort_id is None else "AND "
            query_str += where_clause['query_str']
            param_tuple += where_clause['value_tuple']

        cursor.execute((query_str_lead % ('ParticipantBarcode', 'participant_count')) + query_str, param_tuple)

        for row in cursor.fetchall():
            counts['participant_count'] = row['participant_count']

        cursor.execute((query_str_lead % ('SampleBarcode', 'sample_count')) + query_str, param_tuple)

        for row in cursor.fetchall():
            counts['sample_count'] = row['sample_count']

        return counts

    except Exception as e:
        print traceback.format_exc()
    finally:
        if cursor: cursor.close()
        if db and db.open: db.close()


def count_mutations(user, filters=None):
    counts_and_total = {}


def count_metadata(user, cohort_id=None, sample_ids=None, filters=None):
    counts_and_total = {}
    sample_tables = {}
    valid_attrs = {}
    study_ids = ()
    table_key_map = {}

    if filters is None:
        filters = {}

    if sample_ids is None:
        sample_ids = {}

    for key in sample_ids:
        samples_by_study = sample_ids[key]
        sample_ids[key] = {
            'SampleBarcode': build_where_clause({'SampleBarcode': samples_by_study}),
            'sample_barcode': build_where_clause({'sample_barcode': samples_by_study}),
        }

    db = get_sql_connection()
    django.setup()

    cursor = None

    try:
        # Add TCGA attributes to the list of available attributes
        if 'user_studies' not in filters or 'tcga' in filters['user_studies']['values']:
            sample_tables['metadata_samples'] = {'sample_ids': None}
            if sample_ids and None in sample_ids:
                sample_tables['metadata_samples']['sample_ids'] = sample_ids[None]

            cursor = db.cursor(MySQLdb.cursors.DictCursor)
            cursor.execute('SELECT attribute, spec FROM metadata_attr')
            for row in cursor.fetchall():
                if row['attribute'] in METADATA_SHORTLIST:
                    valid_attrs[row['spec'] + ':' + row['attribute']] = {
                        'name': row['attribute'],
                        'tables': ('metadata_samples',),
                        'sample_ids': None
                    }
            cursor.close()

        # If we have a user, get a list of valid studies
        if user:
            for study in Study.get_user_studies(user):
                if 'user_studies' not in filters or study.id in filters['user_studies']['values']:
                    study_ids += (study.id,)

                    for tables in User_Data_Tables.objects.filter(study=study):
                        sample_tables[tables.metadata_samples_table] = {'sample_ids': None}
                        if sample_ids and study.id in sample_ids:
                            sample_tables[tables.metadata_samples_table]['sample_ids'] = sample_ids[study.id]

            features = User_Feature_Definitions.objects.filter(study__in=study_ids)
            for feature in features:
                if ' ' in feature.feature_name:
                    # It is not a column name and comes from molecular data, ignore it
                    continue

                name = feature.feature_name
                key = 'study:' + str(feature.study_id) + ':' + name

                if feature.shared_map_id:
                    key = feature.shared_map_id
                    name = feature.shared_map_id.split(':')[-1]

                if key not in valid_attrs:
                    valid_attrs[key] = {'name': name, 'tables': (), 'sample_ids': None}

                for tables in User_Data_Tables.objects.filter(study_id=feature.study_id):
                    valid_attrs[key]['tables'] += (tables.metadata_samples_table,)

                    if tables.metadata_samples_table not in table_key_map:
                        table_key_map[tables.metadata_samples_table] = {}
                    table_key_map[tables.metadata_samples_table][key] = feature.feature_name

                    if key in filters:
                        filters[key]['tables'] += (tables.metadata_samples_table,)

                    if sample_ids and feature.study_id in sample_ids:
                        valid_attrs[key]['sample_ids'] = sample_ids[feature.study_id]
        else:
            print "User not authenticated with Metadata Endpoint API"

        # Now that we're through the Studies filtering area, delete it so it doesn't get pulled into a query
        if 'user_studies' in filters:
            del filters['user_studies']

        # For filters with no tables at this point, assume its the TCGA metadata_samples table
        for key, obj in filters.items():
            if not obj['tables']:
                filters[key]['tables'].append('metadata_samples')

        params_tuple = ()
        counts = {}

        cursor = db.cursor()

        # We need to perform 2 sets of queries: one with each filter excluded from the others, against the full
        # metadata_samples/cohort JOIN, and one where all filters are applied to create a temporary table, and
        # attributes *outside* that set are counted

        unfiltered_attr = []
        exclusionary_filter = {}
        where_clause = None
        filtered_join = 'metadata_samples ms'

        for attr in valid_attrs:
            if attr not in filters:
                unfiltered_attr.append(attr.split(':')[-1])

        key_map = table_key_map['metadata_samples'] if 'metadata_samples' in table_key_map else False

        # construct the WHERE clauses needed
        if filters.__len__() > 0:
            if cohort_id is not None:
                filtered_join = 'cohorts_samples cs JOIN metadata_samples ms ON cs.sample_id = ms.SampleBarcode'
            filter_copy = copy.deepcopy(filters)
            where_clause = build_where_clause(filter_copy, alt_key_map=key_map)
            for filter in filters:
                filter_copy = copy.deepcopy(filters)
                del filter_copy[filter]
                if filter_copy.__len__() <= 0:
                    ex_where_clause = {'query_str': None, 'value_tuple': None}
                else:
                    ex_where_clause = build_where_clause(filter_copy, alt_key_map=key_map)
                if cohort_id is not None:
                    if ex_where_clause['query_str'] is not None:
                        ex_where_clause['query_str'] += ' AND '
                    else:
                        ex_where_clause['query_str'] = ''
                        ex_where_clause['value_tuple'] = ()
                    ex_where_clause['query_str'] += ' cs.cohort_id=%s '
                    ex_where_clause['value_tuple'] += (cohort_id,)

                exclusionary_filter[filter.split(':')[-1]] = ex_where_clause

        query_table_name = None
        tmp_table_name = None

        # Only create the temporary table if there's something to actually filter down the
        # source table - otherwise, it's just a waste of time and memory
        if unfiltered_attr.__len__() > 0 and (filters.__len__() > 0 or cohort_id is not None):
            # TODO: This should take into account variable tables; may require a UNION statement or similar
            tmp_table_name = "filtered_samples_tmp_" + user.id.__str__() + "_" + make_id(6)
            query_table_name = tmp_table_name
            make_tmp_table_str = "CREATE TEMPORARY TABLE " + tmp_table_name + " AS SELECT * "

            if cohort_id is not None:
                make_tmp_table_str += "FROM cohorts_samples cs "
                make_tmp_table_str += "JOIN metadata_samples ms ON ms.SampleBarcode = cs.sample_id "
                make_tmp_table_str += "WHERE cs.cohort_id = %s "
                params_tuple += (cohort_id,)
            else:
                make_tmp_table_str += "FROM metadata_samples ms "

            if filters.__len__() > 0:
                make_tmp_table_str += "WHERE " if cohort_id is None else "AND "
                make_tmp_table_str += where_clause['query_str']
                params_tuple += where_clause['value_tuple']

            make_tmp_table_str += ";"
            cursor.execute(make_tmp_table_str, params_tuple)
        else:
            query_table_name = 'metadata_samples'

        count_query_set = []

        for key, feature in valid_attrs.items():
            # TODO: This should be restructured to deal with features and user data
            for table in feature['tables']:
                # Check if the filters make this table 0 anyway
                # We do this to avoid SQL errors for columns that don't exist
                should_be_queried = True

                for key, filter in filters.items():
                    if table not in filter['tables']:
                        should_be_queried = False
                        break

                col_name = feature['name']
                if key_map and key in key_map:
                    col_name = key_map[key]

                if should_be_queried:
                    if col_name in unfiltered_attr:
                        count_query_set.append({'query_str':("""
                            SELECT DISTINCT IF(ms.%s IS NULL,'None',ms.%s) AS %s, IF(counts.count IS NULL,0,counts.count) AS
                            count
                            FROM %s ms
                            LEFT JOIN (SELECT DISTINCT %s, COUNT(1) as count FROM %s GROUP BY %s) AS counts
                            ON counts.%s = ms.%s OR (counts.%s IS NULL AND ms.%s IS NULL);
                          """) % (col_name, col_name, col_name, 'metadata_samples', col_name, query_table_name, col_name, col_name, col_name, col_name, col_name),
                        'params': None, })
                    else:
                        subquery = filtered_join + ((' WHERE ' + exclusionary_filter[col_name]['query_str']) if exclusionary_filter[col_name]['query_str'] else ' ')
                        print >> sys.stdout, subquery
                        count_query_set.append({'query_str':("""
                            SELECT DISTINCT IF(ms.%s IS NULL,'None',ms.%s) AS %s, IF(counts.count IS NULL,0,counts.count) AS
                            count
                            FROM %s AS ms
                            LEFT JOIN (SELECT DISTINCT %s, COUNT(1) as count FROM %s GROUP BY %s) AS counts
                            ON counts.%s = ms.%s OR (counts.%s IS NULL AND ms.%s IS NULL);
                          """) % (col_name, col_name, col_name, 'metadata_samples', col_name, subquery, col_name, col_name, col_name, col_name, col_name),
                        'params': exclusionary_filter[col_name]['value_tuple']})

        for query in count_query_set:
            if 'params' in query and query['params'] is not None:
                cursor.execute(query['query_str'], query['params'])
            else:
                cursor.execute(query['query_str'])

            colset = cursor.description
            col_headers = []
            if colset is not None:
                col_headers = [i[0] for i in cursor.description]
            if not col_headers[0] in counts:
                counts[col_headers[0]] = {}
                counts[col_headers[0]]['counts'] = {}
                counts[col_headers[0]]['total'] = 0
            for row in cursor.fetchall():
                counts[col_headers[0]]['counts'][row[0]] = int(row[1])
                counts[col_headers[0]]['total'] += int(row[1])

        # Drop the temporary table, if we made one
        if tmp_table_name is not None:
            cursor.execute(("DROP TEMPORARY TABLE IF EXISTS %s") % tmp_table_name)

        sample_and_participant_counts = get_participant_and_sample_count(filters, cohort_id);

        counts_and_total['participants'] = sample_and_participant_counts['participant_count']
        counts_and_total['total'] = sample_and_participant_counts['sample_count']
        counts_and_total['counts'] = []

        for key, feature in valid_attrs.items():
            value_list = []
            feature['values'] = counts[feature['name']]['counts']
            feature['total'] = counts[feature['name']]['total']

            # Special case for age ranges
            if key == 'CLIN:age_at_initial_pathologic_diagnosis':
                feature['values'] = normalize_ages(feature['values'])
            elif key == 'CLIN:bmi':
                feature['values'] = normalize_bmi(feature['values'])

            for value, count in feature['values'].items():
                if feature['name'].startswith('has_'):
                    value = 'True' if value else 'False'

                if feature['name'] in DISPLAY_NAME_DD:
                    value_list.append({'value': str(value), 'count': count, 'displ_name': DISPLAY_NAME_DD[feature['name']][str(value)]})
                else:
                    value_list.append({'value': str(value), 'count': count})

            counts_and_total['counts'].append({'name': feature['name'], 'values': value_list, 'id': key, 'total': feature['total']})

        return counts_and_total

    except (Exception) as e:
        print traceback.format_exc()
    finally:
        if cursor: cursor.close()
        if db and db.open: db.close()


def metadata_counts_platform_list(req_filters, cohort_id, user, limit):
    filters = {}

    if req_filters is not None:
        try:
            for this_filter in req_filters:
                key = this_filter['key']
                if key not in filters:
                    filters[key] = {'values': [], 'tables': []}
                filters[key]['values'].append(this_filter['value'])

        except Exception, e:
            print traceback.format_exc()
            raise Exception(
                'Filters must be a valid JSON formatted array with objects containing both key and value properties'
            )

    start = time.time()
    counts_and_total = count_metadata(user, cohort_id, None, filters)
    stop = time.time()
    logger.debug(
        "[BENCHMARKING] Time to query metadata_counts"
        + (" for cohort " + cohort_id if cohort_id is not None else "")
        + (" and" if cohort_id is not None and filters.__len__() > 0 else "")
        + (" filters " + filters.__str__() if filters.__len__() > 0 else "")
        + ": " + (stop - start).__str__()
    )

    data = []

    cursor = None

    try:
        db = get_sql_connection()
        cursor = db.cursor(MySQLdb.cursors.DictCursor)

        query_str = """
            SELECT IF(has_Illumina_DNASeq=1,'Yes', 'None') AS DNAseq_data,
                IF (has_SNP6=1, 'Genome_Wide_SNP_6', 'None') as cnvrPlatform,
                CASE
                    WHEN has_BCGSC_HiSeq_RNASeq=1 and has_UNC_HiSeq_RNASeq=0 THEN 'HiSeq/BCGSC'
                    WHEN has_BCGSC_HiSeq_RNASeq=1 and has_UNC_HiSeq_RNASeq=1 THEN 'HiSeq/BCGSC and UNC V2'
                    WHEN has_UNC_HiSeq_RNASeq=1 and has_BCGSC_HiSeq_RNASeq=0 and has_BCGSC_GA_RNASeq=0 and has_UNC_GA_RNASeq=0 THEN 'HiSeq/UNC V2'
                    WHEN has_UNC_HiSeq_RNASeq=1 and has_BCGSC_HiSeq_RNASeq=0 and has_BCGSC_GA_RNASeq=0 and has_UNC_GA_RNASeq=1 THEN 'GA and HiSeq/UNC V2'
                    WHEN has_UNC_HiSeq_RNASeq=1 and has_BCGSC_HiSeq_RNASeq=0 and has_BCGSC_GA_RNASeq=1 and has_UNC_GA_RNASeq=0 THEN 'HiSeq/UNC V2 and GA/BCGSC'
                    WHEN has_UNC_HiSeq_RNASeq=1 and has_BCGSC_HiSeq_RNASeq=1 and has_BCGSC_GA_RNASeq=0 and has_UNC_GA_RNASeq=0 THEN 'HiSeq/UNC V2 and BCGSC'
                    WHEN has_BCGSC_GA_RNASeq=1 and has_UNC_HiSeq_RNASeq=0 THEN 'GA/BCGSC'
                    WHEN has_UNC_GA_RNASeq=1 and has_UNC_HiSeq_RNASeq=0 THEN 'GA/UNC V2' ELSE 'None'
                END AS gexpPlatform,
                CASE
                    WHEN has_27k=1 and has_450k=0 THEN 'HumanMethylation27'
                    WHEN has_27k=0 and has_450k=1 THEN 'HumanMethylation450'
                    WHEN has_27k=1 and has_450k=1 THEN '27k and 450k' ELSE 'None'
                END AS methPlatform,
                CASE
                    WHEN has_HiSeq_miRnaSeq=1 and has_GA_miRNASeq=0 THEN 'IlluminaHiSeq_miRNASeq'
                    WHEN has_HiSeq_miRnaSeq=0 and has_GA_miRNASeq=1 THEN 'IlluminaGA_miRNASeq'
                    WHEN has_HiSeq_miRnaSeq=1 and has_GA_miRNASeq=1 THEN 'GA and HiSeq'	ELSE 'None'
                END AS mirnPlatform,
                IF (has_RPPA=1, 'MDA_RPPA_Core', 'None') AS rppaPlatform
        """

        params_tuple = ()

        # TODO: This should take into account variable tables; may require a UNION statement or similar
        if cohort_id is not None:
            query_str += """FROM cohorts_samples cs
                JOIN metadata_samples ms ON ms.SampleBarcode = cs.sample_id
                WHERE cohort_id = %s """
            params_tuple += (cohort_id,)
        else:
            query_str += "FROM metadata_samples ms "

        if filters.__len__() > 0:
            filter_copy = copy.deepcopy(filters)
            where_clause = build_where_clause(filter_copy)
            query_str += "WHERE " if cohort_id is None else "AND "
            query_str += where_clause['query_str']
            params_tuple += where_clause['value_tuple']

        if limit is not None:
            query_str += " LIMIT %s;"
            params_tuple += (limit,)
        else:
            query_str += ";"

        start = time.time()
        cursor.execute(query_str, params_tuple)
        stop = time.time()
        logger.debug("[BENCHMARKING] Time to query platforms in metadata_counts_platform_list for cohort '" +
                     (cohort_id if cohort_id is not None else 'None') + "': " + (stop - start).__str__())
        for row in cursor.fetchall():
            item = {
                'DNAseq_data': str(row['DNAseq_data']),
                'cnvrPlatform': str(row['cnvrPlatform']),
                'gexpPlatform': str(row['gexpPlatform']),
                'methPlatform': str(row['methPlatform']),
                'mirnPlatform': str(row['mirnPlatform']),
                'rppaPlatform': str(row['rppaPlatform']),
            }
            data.append(item)

        return {'items': data, 'count': counts_and_total['counts'],
                'participants': counts_and_total['participants'],
                'total': counts_and_total['total']}

    except Exception as e:
        print traceback.format_exc()
    finally:
        if cursor: cursor.close()
        if db and db.open: db.close()

''' End metadata counting methods '''


def data_availability_sort(key, value, data_attr, attr_details):
    if key == 'has_Illumina_DNASeq':
        attr_details['DNA_sequencing'] = sorted(value, key=lambda k: int(k['count']), reverse=True)
    if key == 'has_SNP6':
        attr_details['SNP_CN'] = sorted(value, key=lambda k: int(k['count']), reverse=True)
    if key == 'has_RPPA':
        attr_details['Protein'] = sorted(value, key=lambda k: int(k['count']), reverse=True)

    if key == 'has_27k':
        count = [v['count'] for v in value if v['value'] == 'True']
        attr_details['DNA_methylation'].append({
            'value': '27k',
            'count': count[0] if count.__len__() > 0 else 0
        })
    if key == 'has_450k':
        count = [v['count'] for v in value if v['value'] == 'True']
        attr_details['DNA_methylation'].append({
            'value': '450k',
            'count': count[0] if count.__len__() > 0 else 0
        })
    if key == 'has_HiSeq_miRnaSeq':
        count = [v['count'] for v in value if v['value'] == 'True']
        attr_details['miRNA_sequencing'].append({
            'value': 'Illumina HiSeq',
            'count': count[0] if count.__len__() > 0 else 0
        })
    if key == 'has_GA_miRNASeq':
        count = [v['count'] for v in value if v['value'] == 'True']
        attr_details['miRNA_sequencing'].append({
            'value': 'Illumina GA',
            'count': count[0] if count.__len__() > 0 else 0
        })
    if key == 'has_UNC_HiSeq_RNASeq':
        count = [v['count'] for v in value if v['value'] == 'True']
        attr_details['RNA_sequencing'].append({
            'value': 'UNC Illumina HiSeq',
            'count': count[0] if count.__len__() > 0 else 0
        })
    if key == 'has_UNC_GA_RNASeq':
        count = [v['count'] for v in value if v['value'] == 'True']
        attr_details['RNA_sequencing'].append({
            'value': 'UNC Illumina GA',
            'count': count[0] if count.__len__() > 0 else 0
        })
    if key == 'has_BCGSC_HiSeq_RNASeq':
        count = [v['count'] for v in value if v['value'] == 'True']
        attr_details['RNA_sequencing'].append({
            'value': 'BCGSC Illumina HiSeq',
            'count': count[0] if count.__len__() > 0 else 0
        })
    if key == 'has_BCGSC_GA_RNASeq':
        count = [v['count'] for v in value if v['value'] == 'True']
        attr_details['RNA_sequencing'].append({
            'value': 'BCGSC Illumina GA',
            'count': count[0] if count.__len__() > 0 else 0
        })

@login_required
def public_cohort_list(request):
    return cohorts_list(request, is_public=True)

@login_required
def cohorts_list(request, is_public=False, workbook_id=0, worksheet_id=0, create_workbook=False):
    if debug: print >> sys.stderr,'Called '+sys._getframe().f_code.co_name
    # check to see if user has read access to 'All TCGA Data' cohort
    isb_superuser = User.objects.get(username='isb')
    superuser_perm = Cohort_Perms.objects.get(user=isb_superuser)
    user_all_data_perm = Cohort_Perms.objects.filter(user=request.user, cohort=superuser_perm.cohort)
    if not user_all_data_perm:
        Cohort_Perms.objects.create(user=request.user, cohort=superuser_perm.cohort, perm=Cohort_Perms.READER)

    # add_data_cohort = Cohort.objects.filter(name='All TCGA Data')

    users = User.objects.filter(is_superuser=0)
    cohort_perms = Cohort_Perms.objects.filter(user=request.user).values_list('cohort', flat=True)
    cohorts = Cohort.objects.filter(id__in=cohort_perms, active=True).order_by('-last_date_saved').annotate(num_patients=Count('samples'))
    cohorts.has_private_cohorts = False
    shared_users = {}

    for item in cohorts:
        item.perm = item.get_perm(request).get_perm_display()
        item.owner = item.get_owner()
        shared_with_ids = Cohort_Perms.objects.filter(cohort=item, perm=Cohort_Perms.READER).values_list('user', flat=True)
        item.shared_with_users = User.objects.filter(id__in=shared_with_ids)
        if not item.owner.is_superuser:
            cohorts.has_private_cohorts = True
            # if it is not a public cohort and it has been shared with other users
            # append the list of shared users to the shared_users array
            if item.shared_with_users:
                shared_users[int(item.id)] = serializers.serialize('json', item.shared_with_users, fields=('last_name', 'first_name', 'email'))

        # print local_zone.localize(item.last_date_saved)

    # Used for autocomplete listing
    cohort_id_names = Cohort.objects.filter(id__in=cohort_perms, active=True).values('id', 'name')
    cohort_listing = []
    for cohort in cohort_id_names:
        cohort_listing.append({
            'value': int(cohort['id']),
            'label': cohort['name'].encode('utf8')
        })
    workbook = None
    worksheet = None
    previously_selected_cohort_ids = []
    if workbook_id != 0:
        workbook = Workbook.objects.get(owner=request.user, id=workbook_id)
        worksheet = workbook.worksheet_set.get(id=worksheet_id)
        worksheet_cohorts = worksheet.worksheet_cohort_set.all()
        for wc in worksheet_cohorts :
            previously_selected_cohort_ids.append(wc.cohort_id)

    return render(request, 'cohorts/cohort_list.html', {'request': request,
                                                        'cohorts': cohorts,
                                                        'user_list': users,
                                                        'cohorts_listing': cohort_listing,
                                                        'shared_users':  json.dumps(shared_users),
                                                        'base_url': settings.BASE_URL,
                                                        'base_api_url': settings.BASE_API_URL,
                                                        'is_public': is_public,
                                                        'workbook': workbook,
                                                        'worksheet': worksheet,
                                                        'previously_selected_cohort_ids' : previously_selected_cohort_ids,
                                                        'create_workbook': create_workbook,
                                                        'from_workbook': bool(workbook),
                                                        })

@login_required
def cohort_select_for_new_workbook(request):
    return cohorts_list(request=request, is_public=False, workbook_id=0, worksheet_id=0, create_workbook=True)

@login_required
def cohort_select_for_existing_workbook(request, workbook_id, worksheet_id):
    return cohorts_list(request=request, is_public=False, workbook_id=workbook_id, worksheet_id=worksheet_id)

@login_required
def cohort_create_for_new_workbook(request):
    return cohort_detail(request=request, cohort_id=0, workbook_id=0, worksheet_id=0, create_workbook=True)

@login_required
def cohort_create_for_existing_workbook(request, workbook_id, worksheet_id):
    return cohort_detail(request=request, cohort_id=0, workbook_id=workbook_id, worksheet_id=worksheet_id)

@login_required
def cohort_detail(request, cohort_id=0, workbook_id=0, worksheet_id=0, create_workbook=False):
    if debug: print >> sys.stderr,'Called '+sys._getframe().f_code.co_name
    users = User.objects.filter(is_superuser=0).exclude(id=request.user.id)
    cohort = None
    shared_with_users = []

    # service = build('meta', 'v1', discoveryServiceUrl=META_DISCOVERY_URL)
    clin_attr = [
        'Project',
        'Study',
        'vital_status',
        # 'survival_time',
        'gender',
        'age_at_initial_pathologic_diagnosis',
        'SampleTypeCode',
        'tumor_tissue_site',
        'histological_type',
        'prior_dx',
        'pathologic_stage',
        'person_neoplasm_cancer_status',
        'new_tumor_event_after_initial_treatment',
        'neoplasm_histologic_grade',
        'bmi',
        'hpv_status',
        'residual_tumor',
        # 'targeted_molecular_therapy', TODO: Add to metadata_samples
        'tobacco_smoking_history',
        'icd_10',
        'icd_o_3_site',
        'icd_o_3_histology'
    ]

    data_attr = [
        'DNA_sequencing',
        'RNA_sequencing',
        'miRNA_sequencing',
        'Protein',
        'SNP_CN',
        'DNA_methylation'
    ]

    mutation_attr = {
        'cat': [
            {'name': 'Silent', 'value': 'silent', 'count': 0, 'attrs': {
                'silent': 1,
                'RNA': 1,
                'intron': 1,
            }},
            {'name': 'Non-silent', 'value': 'nonsilent', 'count': 0, 'attrs': {
                'Frame_Shift_Ins': 1,
                'Frame_Shift_Del': 1,
                'Missense_Mutation': 1,
                'Nonsense_Mutation': 1,
                'Splice_Site': 1,
            }},
        ],
        'attr': {}
    }

    for mut_attr in MUTATION_SHORTLIST:
        mutation_attr[mut_attr] = {'name': DISPLAY_NAME_DD['Somatic_Mutations'][mut_attr], 'value': mut_attr, 'count': 0}

    molec_attr = [
        'somatic_mutation_status',
        'mRNA_expression',
        'miRNA_expression',
        'DNA_methylation',
        'gene_copy_number',
        'protein_quantification'
    ]

    clin_attr_dsp = []
    clin_attr_dsp += clin_attr

    user = Django_User.objects.get(id=request.user.id)

    start = time.time()
    results = metadata_counts_platform_list(None, cohort_id if cohort_id else None, user, None)

    stop = time.time()
    logger.debug("[BENCHMARKING] Time to query metadata_counts_platform_list in cohort_detail: "+(stop-start).__str__())

    totals = results['total']

    if USER_DATA_ON:
        # Add in user data
        user_attr = ['user_project','user_study']
        projects = Project.get_user_projects(request.user, True)
        studies = Study.get_user_studies(request.user, True)
        features = User_Feature_Definitions.objects.filter(study__in=studies)
        study_counts = {}
        project_counts = {}

        for count in results['count']:
            if 'id' in count and count['id'].startswith('study:'):
                split = count['id'].split(':')
                study_id = split[1]
                feature_name = split[2]
                study_counts[study_id] = count['total']

        user_studies = []
        for study in studies:
            count = study_counts[study.id] if study.id in study_counts else 0

            if not study.project_id in project_counts:
                project_counts[study.project_id] = 0
            project_counts[study.project_id] += count

            user_studies += ({
                'count': str(count),
                'value': study.name,
                'id'   : study.id
            },)

        user_projects = []
        for project in projects:
            user_projects += ({
                'count': str(project_counts[project.id]) if project.id in project_counts else 0,
                'value': project.name,
                'id'   : project.id
            },)

        results['count'].append({
            'name': 'user_projects',
            'values': user_projects
        })
        results['count'].append({
            'name': 'user_studies',
            'values': user_studies
        })

    # Get and sort counts
    attr_details = {
        'RNA_sequencing': [],
        'miRNA_sequencing': [],
        'DNA_methylation': []
    }
    keys = []
    for item in results['count']:
        key = item['name']
        values = item['values']

        if key.startswith('has_'):
            data_availability_sort(key, values, data_attr, attr_details)
        else:
            keys.append(item['name'])
            item['values'] = sorted(values, key=lambda k: int(k['count']), reverse=True)

            if item['name'].startswith('user_'):
                clin_attr_dsp += (item['name'],)

    for key, value in attr_details.items():
        results['count'].append({
            'name': key,
            'values': value,
            'id': None
         })

    template_values = {
        'request': request,
        'users': users,
        'attr_list': keys,
        'attr_list_count': results['count'],
        'total_samples': int(totals),
        'clin_attr': clin_attr_dsp,
        'data_attr': data_attr,
        'molec_attr': molec_attr,
        'base_url': settings.BASE_URL,
        'base_api_url': settings.BASE_API_URL,
        'mutation_attr': mutation_attr,
    }

    if USER_DATA_ON:
        template_values['user_attr'] = user_attr

    if workbook_id and worksheet_id :
        template_values['workbook']  = Workbook.objects.get(id=workbook_id)
        template_values['worksheet'] = Worksheet.objects.get(id=worksheet_id)
    elif create_workbook:
        template_values['create_workbook'] = True

    template = 'cohorts/new_cohort.html'

    template_values['metadata_counts'] = results

    if cohort_id != 0:
        try:
            cohort = Cohort.objects.get(id=cohort_id, active=True)
            cohort.perm = cohort.get_perm(request)
            cohort.owner = cohort.get_owner()

            if not cohort.perm:
                messages.error(request, 'You do not have permission to view that cohort.')
                return redirect('cohort_list')

            cohort.mark_viewed(request)

            shared_with_ids = Cohort_Perms.objects.filter(cohort=cohort, perm=Cohort_Perms.READER).values_list('user', flat=True)
            shared_with_users = User.objects.filter(id__in=shared_with_ids)
            template = 'cohorts/cohort_details.html'
            template_values['cohort'] = cohort
            template_values['total_samples'] = len(cohort.samples_set.all())
            template_values['total_patients'] = len(cohort.patients_set.all())
            template_values['shared_with_users'] = shared_with_users
        except ObjectDoesNotExist:
            # Cohort doesn't exist, return to user landing with error.
            messages.error(request, 'The cohort you were looking for does not exist.')
            return redirect('cohort_list')

    return render(request, template, template_values)

'''
Saves a cohort, adds the new cohort to an existing worksheet, then redirected back to the worksheet display
'''
@login_required
def save_cohort_for_existing_workbook(request):
    return save_cohort(request=request, workbook_id=request.POST.get('workbook_id'), worksheet_id=request.POST.get("worksheet_id"))

'''
Saves a cohort, adds the new cohort to a new worksheet, then redirected back to the worksheet display
'''
@login_required
def save_cohort_for_new_workbook(request):
    return save_cohort(request=request, workbook_id=None, worksheet_id=None, create_workbook=True)

@login_required
def add_cohorts_to_worksheet(request, workbook_id=0, worksheet_id=0):
    if request.method == 'POST':
        cohorts = request.POST.getlist('cohorts')
        workbook = request.user.workbook_set.get(id=workbook_id)
        worksheet = workbook.worksheet_set.get(id=worksheet_id)

        existing_w_cohorts = worksheet.worksheet_cohort_set.all()
        existing_cohort_ids = []
        for wc in existing_w_cohorts :
            existing_cohort_ids.append(str(wc.cohort_id))

        for ec in existing_cohort_ids:
            if ec not in cohorts :
                missing_cohort = Cohort.objects.get(id=ec)
                worksheet.remove_cohort(missing_cohort)

        cohort_perms = request.user.cohort_perms_set.filter(cohort__active=True)

        for cohort in cohorts:
            cohort_model = cohort_perms.get(cohort__id=cohort).cohort
            worksheet.add_cohort(cohort_model)

    redirect_url = reverse('worksheet_display', kwargs={'workbook_id':workbook_id, 'worksheet_id': worksheet_id})
    return redirect(redirect_url)

@login_required
def remove_cohort_from_worksheet(request, workbook_id=0, worksheet_id=0, cohort_id=0):
    if request.method == 'POST':
        workbook = request.user.workbook_set.get(id=workbook_id)
        worksheet = workbook.worksheet_set.get(id=worksheet_id)

        cohorts = request.user.cohort_perms_set.filter(cohort__active=True,cohort__id=cohort_id, perm=Cohort_Perms.OWNER)
        if cohorts.count() > 0:
            for cohort in cohorts:
                cohort_model = cohort.cohort
                worksheet.remove_cohort(cohort_model)

    redirect_url = reverse('worksheet_display', kwargs={'workbook_id':workbook_id, 'worksheet_id': worksheet_id})
    return redirect(redirect_url)

'''
This save view only works coming from cohort editing or creation views.
- only ever one source coming in
- filters optional
'''
# TODO: Create new view to save cohorts from visualizations
@login_required
@csrf_protect
def save_cohort(request, workbook_id=None, worksheet_id=None, create_workbook=False):
    if debug: print >> sys.stderr,'Called '+sys._getframe().f_code.co_name

    redirect_url = reverse('cohort_list')

    samples = []
    patients = []
    name = ''
    user_id = request.user.id
    parent = None

    if request.POST:
        name = request.POST.get('name')
        source = request.POST.get('source')
        deactivate_sources = request.POST.get('deactivate_sources')
        filters = request.POST.getlist('filters')
        projects = request.user.project_set.all()

        # TODO: Make this a query in the view
        token = SocialToken.objects.filter(account__user=request.user, account__provider='Google')[0].token
        data_url = METADATA_API + 'v2/metadata_sample_list'
        payload = {
            'token': token
        }
        # Given cohort_id is the only source id.
        if source:
            # Only ever one source
            # data_url += '&cohort_id=' + source
            payload['cohort_id'] = source
            parent = Cohort.objects.get(id=source)
            if deactivate_sources:
                parent.active = False
                parent.save()

        if filters:

            filter_obj = []
            for filter in filters:
                tmp = json.loads(filter)
                key = tmp['feature']['name']
                val = tmp['value']['name']

                if 'id' in tmp['feature'] and tmp['feature']['id']:
                    key = tmp['feature']['id']

                if 'id' in tmp['value'] and tmp['value']['id']:
                    val = tmp['value']['id']

                if key == 'user_projects':
                    proj = projects.get(id=val)
                    studies = proj.study_set.all()
                    for study in studies:
                        filter_obj.append({
                            'key': 'user_studies',
                            'value': str(study.id)
                        })

                else :
                    filter_obj.append({
                        'key': key,
                        'value': val
                    })

            if len(filter_obj):
                # data_url += '&filters=' + re.sub(r'\s+', '', urllib.quote( json.dumps(filter_obj) ))
                payload['filters'] = json.dumps(filter_obj)
        result = urlfetch.fetch(data_url, method=urlfetch.POST, payload=json.dumps(payload), deadline=60, headers={'Content-Type': 'application/json'})
        items = json.loads(result.content)

        #it is possible the the filters are creating a cohort with no samples
        if int(items['count']) == 0 :
            messages.error(request, 'The filters selected returned 0 samples. Please alter your filters and try again')
            redirect_url = reverse('cohort')
        else :
            items = items['items']
            for item in items:
                samples.append(item['sample_barcode'])
                #patients.append(item['ParticipantBarcode'])

            # Create new cohort
            cohort = Cohort.objects.create(name=name)
            cohort.save()

            # If there are sample ids
            sample_list = []
            for item in items:
                study = None
                if 'study_id' in item:
                    study = item['study_id']
                sample_list.append(Samples(cohort=cohort, sample_id=item['sample_barcode'], study_id=study))
            Samples.objects.bulk_create(sample_list)

            # TODO This would be a nice to have if we have a mapped ParticipantBarcode value
            # TODO Also this gets weird with mixed mapped and unmapped ParticipantBarcode columns in cohorts
            # If there are patient ids
            # If we are *not* using user data, get participant barcodes from metadata_data
            if not USER_DATA_ON:
                participant_url = METADATA_API + ('v2/metadata_participant_list?cohort_id=%s' % (str(cohort.id),))
                participant_result = urlfetch.fetch(participant_url, deadline=120)
                participant_items = json.loads(participant_result.content)
                participant_list = []
                for item in participant_items['items']:
                    participant_list.append(Patients(cohort=cohort, patient_id=item['sample_barcode']))
                Patients.objects.bulk_create(participant_list)

            # Set permission for user to be owner
            perm = Cohort_Perms(cohort=cohort, user=request.user, perm=Cohort_Perms.OWNER)
            perm.save()

            # Create the source if it was given
            if source:
                Source.objects.create(parent=parent, cohort=cohort, type=Source.FILTERS).save()

            # Create filters applied
            if filters:
                for filter in filter_obj:
                    Filters.objects.create(resulting_cohort=cohort, name=filter['key'], value=filter['value']).save()

            # Store cohort to BigQuery
            project_id = settings.BQ_PROJECT_ID
            cohort_settings = settings.GET_BQ_COHORT_SETTINGS()
            bcs = BigQueryCohortSupport(project_id, cohort_settings.dataset_id, cohort_settings.table_id)
            bcs.add_cohort_with_sample_barcodes(cohort.id, cohort.samples_set.values_list('sample_id','study_id'))

            # Check if coming from applying filters and redirect accordingly
            if 'apply-filters' in request.POST:
                redirect_url = reverse('cohort_details',args=[cohort.id])
                messages.info(request, 'Changes applied successfully.')
            else:
                redirect_url = reverse('cohort_list')
                messages.info(request, 'Cohort "%s" created successfully.' % cohort.name)

            if workbook_id and worksheet_id :
                Worksheet.objects.get(id=worksheet_id).add_cohort(cohort)
                redirect_url = reverse('worksheet_display', kwargs={'workbook_id':workbook_id, 'worksheet_id' : worksheet_id})
            elif create_workbook :
                workbook_model  = Workbook.create("default name", "This is a default workbook description", request.user)
                worksheet_model = Worksheet.create(workbook_model.id, "worksheet 1","This is a default description")
                worksheet_model.add_cohort(cohort)
                redirect_url = reverse('worksheet_display', kwargs={'workbook_id': workbook_model.id, 'worksheet_id' : worksheet_model.id})

    return redirect(redirect_url) # redirect to search/ with search parameters just saved

@login_required
@csrf_protect
def delete_cohort(request):
    if debug: print >> sys.stderr,'Called '+sys._getframe().f_code.co_name
    redirect_url = 'cohort_list'
    cohort_ids = request.POST.getlist('id')
    Cohort.objects.filter(id__in=cohort_ids).update(active=False)
    return redirect(reverse(redirect_url))

@login_required
@csrf_protect
def share_cohort(request, cohort_id=0):
    if debug: print >> sys.stderr,'Called '+sys._getframe().f_code.co_name
    user_ids = request.POST.getlist('users')
    users = User.objects.filter(id__in=user_ids)

    if cohort_id == 0:
        redirect_url = '/cohorts/'
        cohort_ids = request.POST.getlist('cohort-ids')
        cohorts = Cohort.objects.filter(id__in=cohort_ids)
    else:
        redirect_url = '/cohorts/%s' % cohort_id
        cohorts = Cohort.objects.filter(id=cohort_id)
    for user in users:

        for cohort in cohorts:
            obj = Cohort_Perms.objects.create(user=user, cohort=cohort, perm=Cohort_Perms.READER)
            obj.save()

    return redirect(redirect_url)

@login_required
@csrf_protect
def clone_cohort(request, cohort_id):
    if debug: print >> sys.stderr,'Called '+sys._getframe().f_code.co_name
    redirect_url = 'cohort_details'
    parent_cohort = Cohort.objects.get(id=cohort_id)
    new_name = 'Copy of %s' % parent_cohort.name
    cohort = Cohort.objects.create(name=new_name)
    cohort.save()

    # If there are sample ids
    samples = Samples.objects.filter(cohort=parent_cohort).values_list('sample_id', 'study_id')
    sample_list = []
    for sample in samples:
        sample_list.append(Samples(cohort=cohort, sample_id=sample[0], study_id=sample[1]))
    Samples.objects.bulk_create(sample_list)

    # TODO Some cohorts won't have them at the moment. That isn't a big deal in this function
    # If there are patient ids
    patients = Patients.objects.filter(cohort=parent_cohort).values_list('patient_id', flat=True)
    patient_list = []
    for patient_code in patients:
        patient_list.append(Patients(cohort=cohort, patient_id=patient_code))
    Patients.objects.bulk_create(patient_list)

    # Clone the filters
    filters = Filters.objects.filter(resulting_cohort=parent_cohort).values_list('name', 'value')
    # ...but only if there are any (there may not be)
    if filters.__len__() > 0:
        filters_list = []
        for filter_pair in filters:
            filters_list.append(Filters(name=filter_pair[0], value=filter_pair[1], resulting_cohort=cohort))
        Filters.objects.bulk_create(filters_list)

    # Set source
    source = Source(parent=parent_cohort, cohort=cohort, type=Source.CLONE)
    source.save()

    # Set permissions
    perm = Cohort_Perms(cohort=cohort, user=request.user, perm=Cohort_Perms.OWNER)
    perm.save()

    # Store cohort to BigQuery
    project_id = settings.BQ_PROJECT_ID
    cohort_settings = settings.GET_BQ_COHORT_SETTINGS()
    bcs = BigQueryCohortSupport(project_id, cohort_settings.dataset_id, cohort_settings.table_id)
    bcs.add_cohort_with_sample_barcodes(cohort.id, samples)

    return redirect(reverse(redirect_url,args=[cohort.id]))

@login_required
@csrf_protect
def set_operation(request):
    if debug: print >> sys.stderr,'Called '+sys._getframe().f_code.co_name
    redirect_url = '/cohorts/'

    if request.POST:
        name = request.POST.get('name').encode('utf8')
        cohorts = []
        base_cohort = None
        subtract_cohorts = []
        notes = ''
        patients = []
        samples = []

        op = request.POST.get('operation')
        if op == 'union':
            notes = 'Union of '
            cohort_ids = request.POST.getlist('selected-ids')
            cohorts = Cohort.objects.filter(id__in=cohort_ids, active=True, cohort_perms__in=request.user.cohort_perms_set.all())
            first = True
            ids = ()
            for cohort in cohorts:
                if first:
                    notes += cohort.name
                    first = False
                else:
                    notes += ', ' + cohort.name
                ids += (cohort.id,)
            patients = Patients.objects.filter(cohort_id__in=ids).distinct().values_list('patient_id', flat=True)
            samples = Samples.objects.filter(cohort_id__in=ids).distinct().values_list('sample_id', 'study_id')
        elif op == 'intersect':
            cohort_ids = request.POST.getlist('selected-ids')
            cohorts = Cohort.objects.filter(id__in=cohort_ids, active=True, cohort_perms__in=request.user.cohort_perms_set.all())
            request.user.cohort_perms_set.all()
            if len(cohorts):
                cohort_patients = set(Patients.objects.filter(cohort=cohorts[0]).values_list('patient_id', flat=True))
                cohort_samples = set(Samples.objects.filter(cohort=cohorts[0]).values_list('sample_id', 'study_id'))

                notes = 'Intersection of ' + cohorts[0].name

                # print "Start of intersection with %s has %d" % (cohorts[0].name, len(cohort_samples))
                for i in range(1, len(cohorts)):
                    cohort = cohorts[i]
                    notes += ', ' + cohort.name

                    cohort_patients = cohort_patients.intersection(Patients.objects.filter(cohort=cohort).values_list('patient_id', flat=True))
                    cohort_samples = cohort_samples.intersection(Samples.objects.filter(cohort=cohort).values_list('sample_id', 'study_id'))

                    # se1 = set(x[0] for x in s1)
                    # se2 = set(x[0] for x in s2)
                    # TODO: work this out with user data when activated
                    # cohort_samples = cohort_samples.extra(
                    #         tables=[Samples._meta.db_table+"` AS `t"+str(1)], # TODO This is ugly :(
                    #         where=[
                    #             't'+str(i)+'.sample_id = ' + Samples._meta.db_table + '.sample_id',
                    #             't'+str(i)+'.study_id = ' + Samples._meta.db_table + '.study_id',
                    #             't'+str(i)+'.cohort_id = ' + Samples._meta.db_table + '.cohort_id',
                    #         ]
                    # )
                    # cohort_patients = cohort_patients.extra(
                    #         tables=[Patients._meta.db_table+"` AS `t"+str(1)], # TODO This is ugly :(
                    #         where=[
                    #             't'+str(i)+'.patient_id = ' + Patients._meta.db_table + '.patient_id',
                    #             't'+str(i)+'.cohort_id = ' + Patients._meta.db_table + '.cohort_id',
                    #         ]
                    # )

                patients = list(cohort_patients)
                samples = list(cohort_samples)

        elif op == 'complement':
            base_id = request.POST.get('base-id')
            subtract_ids = request.POST.getlist('subtract-ids')

            base_patients = Patients.objects.filter(cohort_id=base_id)
            subtract_patients = Patients.objects.filter(cohort_id__in=subtract_ids).distinct()
            cohort_patients = base_patients.exclude(patient_id__in=subtract_patients.values_list('patient_id', flat=True))
            patients = cohort_patients.values_list('patient_id', flat=True)

            base_samples = Samples.objects.filter(cohort_id=base_id)
            subtract_samples = Samples.objects.filter(cohort_id__in=subtract_ids).distinct()
            cohort_samples = base_samples.exclude(sample_id__in=subtract_samples.values_list('sample_id', flat=True))
            samples = cohort_samples.values_list('sample_id', 'study_id')

            notes = 'Subtracted '
            base_cohort = Cohort.objects.get(id=base_id)
            subtracted_cohorts = Cohort.objects.filter(id__in=subtract_ids)
            first = True
            for item in subtracted_cohorts:
                if first:
                    notes += item.name
                    first = False
                else:
                    notes += ', ' + item.name
            notes += ' from %s.' % base_cohort.name

        if len(samples) or len(patients):
            new_cohort = Cohort.objects.create(name=name)
            perm = Cohort_Perms(cohort=new_cohort, user=request.user, perm=Cohort_Perms.OWNER)
            perm.save()

            # Store cohort to BigQuery
            project_id = settings.BQ_PROJECT_ID
            cohort_settings = settings.GET_BQ_COHORT_SETTINGS()
            bcs = BigQueryCohortSupport(project_id, cohort_settings.dataset_id, cohort_settings.table_id)
            bcs.add_cohort_with_sample_barcodes(new_cohort.id, samples)

            # Store cohort to CloudSQL
            patient_list = []
            for patient in patients:
                patient_list.append(Patients(cohort=new_cohort, patient_id=patient))
            Patients.objects.bulk_create(patient_list)

            sample_list = []
            for sample in samples:
                sample_list.append(Samples(cohort=new_cohort, sample_id=sample[0], study_id=sample[1]))
            Samples.objects.bulk_create(sample_list)

            # Create Sources
            if op == 'union' or op == 'intersect':
                for cohort in cohorts:
                    source = Source.objects.create(parent=cohort, cohort=new_cohort, type=Source.SET_OPS, notes=notes)
                    source.save()
            elif op == 'complement':
                source = Source.objects.create(parent=base_cohort, cohort=new_cohort, type=Source.SET_OPS, notes=notes)
                source.save()
                for cohort in subtracted_cohorts:
                    source = Source.objects.create(parent=cohort, cohort=new_cohort, type=Source.SET_OPS, notes=notes)
                    source.save()

        else:
            message = 'Operation resulted in empty set of samples and patients. Cohort not created.'
            messages.warning(request, message)
            return redirect('cohort_list')

    return redirect(redirect_url)


@login_required
@csrf_protect
def union_cohort(request):
    if debug: print >> sys.stderr,'Called '+sys._getframe().f_code.co_name
    redirect_url = '/cohorts/'

    return redirect(redirect_url)

@login_required
@csrf_protect
def intersect_cohort(request): 
    if debug: print >> sys.stderr,'Called '+sys._getframe().f_code.co_name
    redirect_url = '/cohorts/'
    return redirect(redirect_url)

@login_required
@csrf_protect
def set_minus_cohort(request):
    if debug: print >> sys.stderr,'Called '+sys._getframe().f_code.co_name
    redirect_url = '/cohorts/'

    return redirect(redirect_url)

@login_required
@csrf_protect
def save_comment(request):
    if debug: print >> sys.stderr,'Called '+sys._getframe().f_code.co_name
    content = request.POST.get('content').encode('utf-8')
    cohort = Cohort.objects.get(id=int(request.POST.get('cohort_id')))
    obj = Cohort_Comments.objects.create(user=request.user, cohort=cohort, content=content)
    obj.save()
    return_obj = {
        'first_name': request.user.first_name,
        'last_name': request.user.last_name,
        'date_created': formats.date_format(obj.date_created, 'DATETIME_FORMAT'),
        'content': obj.content
    }
    return HttpResponse(json.dumps(return_obj), status=200)

@login_required
@csrf_protect
def save_cohort_from_plot(request):
    if debug: print >> sys.stderr,'Called '+sys._getframe().f_code.co_name
    cohort_name = request.POST.get('cohort-name', 'Plot Selected Cohort')
    result = {}

    if cohort_name:
        # Create Cohort
        cohort = Cohort.objects.create(name=cohort_name)
        cohort.save()

        # Create Permission
        perm = Cohort_Perms.objects.create(cohort=cohort, user=request.user, perm=Cohort_Perms.OWNER)
        perm.save()

        # Create Sources, at this point only one cohort for a plot
        plot_id = request.POST.get('plot-id')
        source_plot = Worksheet_plot.objects.get(id=plot_id)
        plot_cohorts = source_plot.get_cohorts()
        source_list = []
        for c in plot_cohorts :
            source_list.append(Source(parent=c, cohort=cohort, type=Source.PLOT_SEL))
        Source.objects.bulk_create(source_list)

        # Create Samples
        samples = request.POST.get('samples', '')
        if len(samples):
            samples = samples.split(',')
        sample_list = []
        patient_id_list = []
        for sample in samples:
            patient_id = sample[:12]
            if patient_id not in patient_id_list:
                patient_id_list.append(patient_id)
            sample_list.append(Samples(cohort=cohort, sample_id=sample))
        Samples.objects.bulk_create(sample_list)

        # Create Patients
        patient_list = []
        for patient in patient_id_list:
            patient_list.append(Patients(cohort=cohort, patient_id=patient))
        Patients.objects.bulk_create(patient_list)

        # Store cohort to BigQuery
        project_id = settings.BQ_PROJECT_ID
        cohort_settings = settings.GET_BQ_COHORT_SETTINGS()
        bcs = BigQueryCohortSupport(project_id, cohort_settings.dataset_id, cohort_settings.table_id)
        bcs.add_cohort_with_sample_barcodes(cohort.id, cohort.samples_set.all().values_list('sample_id', 'study_id'))

        workbook_id  = source_plot.worksheet.workbook_id
        worksheet_id = source_plot.worksheet_id


        result['message'] = "Cohort '" + cohort.name + "' created from the selected sample"
    else :
        result['error'] = "parameters were not correct"

    return HttpResponse(json.dumps(result), status=200)


@login_required
@csrf_protect
def cohort_filelist(request, cohort_id=0):
    if debug: print >> sys.stderr,'Called '+sys._getframe().f_code.co_name
    if cohort_id == 0:
        messages.error(request, 'Cohort provided does not exist.')
        return redirect('/user_landing')

    token = SocialToken.objects.filter(account__user=request.user, account__provider='Google')[0].token
    data_url = METADATA_API + ('v1/cohort_files?platform_count_only=True&cohort_id=%s&token=%s' % (cohort_id, token))
    result = urlfetch.fetch(data_url, deadline=120)
    items = json.loads(result.content)
    file_list = []
    cohort = Cohort.objects.get(id=cohort_id, active=True)
    nih_user = NIH_User.objects.filter(user=request.user, active=True, dbGaP_authorized=True)
    has_access = False
    if len(nih_user) > 0:
        has_access = True
    return render(request, 'cohorts/cohort_filelist.html', {'request': request,
                                                            'cohort': cohort,
                                                            'base_url': settings.BASE_URL,
                                                            'base_api_url': settings.BASE_API_URL,
                                                            # 'file_count': items['total_file_count'],
                                                            # 'page': items['page'],
                                                            'download_url': reverse('download_filelist', kwargs={'cohort_id': cohort_id}),
                                                            'platform_counts': items['platform_count_list'],
                                                            'filelist': file_list,
                                                            'file_list_max': MAX_FILE_LIST_ENTRIES,
                                                            'sel_file_max': MAX_SEL_FILES,
                                                            'has_access': has_access})

@login_required
def cohort_filelist_ajax(request, cohort_id=0):
    if debug: print >> sys.stderr,'Called '+sys._getframe().f_code.co_name
    if cohort_id == 0:
        response_str = '<div class="row">' \
                    '<div class="col-lg-12">' \
                    '<div class="alert alert-danger alert-dismissible">' \
                    '<button type="button" class="close" data-dismiss="alert"><span aria-hidden="true">&times;</span><span class="sr-only">Close</span></button>' \
                    'Cohort provided does not exist.' \
                    '</div></div></div>'
        return HttpResponse(response_str, status=500)

    token = SocialToken.objects.filter(account__user=request.user, account__provider='Google')[0].token
    data_url = METADATA_API + ('v1/cohort_files?cohort_id=%s&token=%s' % (cohort_id, token))

    for key in request.GET:
        data_url += '&' + key + '=' + request.GET[key]

    result = urlfetch.fetch(data_url, deadline=120)

    return HttpResponse(result.content, status=200)


@login_required
@csrf_protect
def cohort_samples_patients(request, cohort_id=0):
    if debug: print >> sys.stderr, 'Called '+sys._getframe().f_code.co_name
    if cohort_id == 0:
        messages.error(request, 'Cohort provided does not exist.')
        return redirect('/user_landing')

    cohort_name = Cohort.objects.filter(id=cohort_id).values_list('name', flat=True)[0].__str__()

    # Sample IDs
    samples = Samples.objects.filter(cohort=cohort_id).values_list('sample_id', flat=True)

    # Patient IDs, may be empty!
    patients = Patients.objects.filter(cohort=cohort_id).values_list('patient_id', flat=True)

    rows = (["Sample and Patient List for Cohort '"+cohort_name+"'"],)
    rows += (["ID", "Type"],)

    for sample_id in samples:
        rows += ([sample_id, "Sample"],)

    for patient_id in patients:
        rows += ([patient_id, "Patient"],)

    pseudo_buffer = Echo()
    writer = csv.writer(pseudo_buffer)
    response = StreamingHttpResponse((writer.writerow(row) for row in rows),
                                     content_type="text/csv")
    response['Content-Disposition'] = 'attachment; filename="samples_patients_in_cohort.csv"'
    return response


class Echo(object):
    """An object that implements just the write method of the file-like
    interface.
    """
    def write(self, value):
        """Write the value by returning it, instead of storing in a buffer."""
        return value


def streaming_csv_view(request, cohort_id=0):
    if debug: print >> sys.stderr,'Called '+sys._getframe().f_code.co_name
    if cohort_id == 0:
        messages.error('Cohort provided does not exist.')
        return redirect('/user_landing')

    total_expected = int(request.GET.get('total'))
    limit = -1 if total_expected < MAX_FILE_LIST_ENTRIES else MAX_FILE_LIST_ENTRIES

    token = SocialToken.objects.filter(account__user=request.user, account__provider='Google')[0].token
    data_url = METADATA_API + ('v1/cohort_files?cohort_id=%s&token=%s&limit=%s' % (cohort_id, token, limit.__str__()))

    if 'params' in request.GET:
        params = request.GET.get('params').split(',')

        for param in params:
            data_url += '&' + param + '=True'

    keep_fetching = True
    file_list = []
    offset = None

    while keep_fetching:
        result = urlfetch.fetch(data_url+('&offset='+offset.__str__() if offset else ''), deadline=60)
        items = json.loads(result.content)

        if 'file_list' in items:
            file_list += items['file_list']
            # offsets are counted from row 0, so setting the offset to the current number of
            # retrieved rows will start the next request on the row we want
            offset = file_list.__len__()
        else:
            if 'error' in items:
                messages.error(request, items['error']['message'])
            return redirect(reverse('cohort_filelist', kwargs={'cohort_id': cohort_id}))

        keep_fetching = ((offset < total_expected) and ('file_list' in items))

    if file_list.__len__() < total_expected:
        messages.error(request, 'Only %d files found out of %d expected!' % (file_list.__len__(), total_expected))
        return redirect(reverse('cohort_filelist', kwargs={'cohort_id': cohort_id}))

    if file_list.__len__() > 0:
        """A view that streams a large CSV file."""
        # Generate a sequence of rows. The range is based on the maximum number of
        # rows that can be handled by a single sheet in most spreadsheet
        # applications.
        rows = (["Sample", "Platform", "Pipeline", "Data Level", "Data Type", "Cloud Storage Location", "Access Type"],)
        for file in file_list:
            rows += ([file['sample'], file['platform'], file['pipeline'], file['datalevel'], file['datatype'], file['cloudstorage_location'], file['access'].replace("-", " ")],)
        pseudo_buffer = Echo()
        writer = csv.writer(pseudo_buffer)
        response = StreamingHttpResponse((writer.writerow(row) for row in rows),
                                         content_type="text/csv")
        response['Content-Disposition'] = 'attachment; filename="file_list.csv"'
        return response

    return render(request)


@login_required
def unshare_cohort(request, cohort_id=0):

    if request.POST.get('owner'):
        # The owner of the resource should also be able to remove users they shared with.
        # Get user_id from post
        user_id = request.POST.get('user_id')
        resc = Cohort_Perms.objects.get(cohort_id=cohort_id, user_id=user_id)
    else:
        # This allows users to remove resources shared with them
        resc = Cohort_Perms.objects.get(cohort_id=cohort_id, user_id=request.user.id)

    resc.delete()

    return JsonResponse({
        'status': 'success'
    })


@login_required
def get_metadata(request):
    filters = json.loads(request.GET.get('filters', '{}'))
    cohort = request.GET.get('cohort_id', None)
    limit = request.GET.get('limit', None)

    user = Django_User.objects.get(id=request.user.id)

    results = metadata_counts_platform_list(filters, cohort, user, limit)

    if not results:
        results = {}
    else:

        data_attr = [
            'DNA_sequencing',
            'RNA_sequencing',
            'miRNA_sequencing',
            'Protein',
            'SNP_CN',
            'DNA_methylation',
        ]

        attr_details = {
            'RNA_sequencing': [],
            'miRNA_sequencing': [],
            'DNA_methylation': [],
        }

        for item in results['count']:
            key = item['name']
            values = item['values']

            if key.startswith('has_'):
                data_availability_sort(key, values, data_attr, attr_details)

        for key, value in attr_details.items():
            results['count'].append({
                'name': key,
                'values': value,
                'id': None
            })

    return JsonResponse(results)
