#
# Copyright 2015-2020, Institute for Systems Biology
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

import logging
import time
import copy
import re
from time import sleep
from idc_collections.models import Collection, Attribute_Tooltips, DataSource, Attribute, \
    Attribute_Display_Values, Program, DataVersion, DataSourceJoin, DataSetType, Attribute_Set_Type, \
    ImagingDataCommonsVersion

from solr_helpers import *
from google_helpers.bigquery.bq_support import BigQuerySupport
from google_helpers.bigquery.export_support import BigQueryExportFileList
import hashlib
from django.conf import settings
import math
BQ_ATTEMPT_MAX = 10
MAX_FILE_LIST_ENTRIES = settings.MAX_FILE_LIST_REQUEST

logger = logging.getLogger('main_logger')

BMI_MAPPING = {
    'underweight': [0, 18.5],
    'normal weight': [18.5,25],
    'overweight': [25,30],
    'obese': 30
}

# a cached  of comprehensive information mapping attributes to data sources:
#
# {
#  '<source_IDs_asc_joined_by_colon>' : {
#    'list': [<String>, ...],
#    'ids': [<Integer>, ...],
#    'sources': {
#       <data source database ID>: {
#          'list': [<String>, ...],
#          'attrs': [<Attribute>, ...],
#          'id': <Integer>,
#          'name': <String>,
#          'data_sets': [<DataSetType>, ...],
#          'count_col': <Integer>
#       }
#     }
#   }
# }
DATA_SOURCE_ATTR = {}
DATA_SOURCE_TYPES = {}
SOLR_FACETS = {}

TYPE_SCHEMA = {
    'sample_type': 'STRING',
    'SOPInstanceUID': 'STRING',
    'SeriesInstanceUID': 'STRING',
    'StudyInstanceUID': 'STRING',
    'SOPClassUID': 'STRING'
}


def fetch_data_source_attr(sources, fetch_settings, cache_as=None):
    source_set = None

    if cache_as:
        cache_name = "{}_{}".format(cache_as, ":".join([str(x) for x in list(sources.order_by('-id').values_list('id',flat=True))]))
        if cache_name not in DATA_SOURCE_ATTR:
            logger.debug("[STATUS] Cache of {} not found, pulling.".format(cache_name))
            DATA_SOURCE_ATTR[cache_name] = sources.get_source_attrs(**fetch_settings)
        source_set = DATA_SOURCE_ATTR[cache_name]
    else:
        logger.debug("[STATUS] Cache not requested for: {}".format(sources))
        source_set = sources.get_source_attrs(**fetch_settings)

    return source_set


def fetch_data_source_types(sources):
    source_ids = [str(x) for x in sources.order_by('id').values_list('id',flat=True)]
    source_set = ":".join(source_ids)

    if source_set not in DATA_SOURCE_TYPES:
        DATA_SOURCE_TYPES[source_set] = sources.get_source_data_types()

    return DATA_SOURCE_TYPES[source_set]


def fetch_solr_facets(fetch_settings, cache_as=None):
    facet_set = None

    if cache_as:
        if cache_as not in SOLR_FACETS:
            SOLR_FACETS[cache_as] = build_solr_facets(**fetch_settings)
        facet_set = SOLR_FACETS[cache_as]
    else:
        facet_set = build_solr_facets(**fetch_settings)

    return facet_set


def fetch_solr_stats(fetch_settings,cache_as=None):
    stat_set = None

    if cache_as:
        if cache_as not in SOLR_FACETS:
            SOLR_FACETS[cache_as] = build_solr_stats(**fetch_settings)
        stat_set = SOLR_FACETS[cache_as]
    else:
        stat_set = build_solr_stats(**fetch_settings)

    return stat_set


# Helper method which, given a list of attribute names, a set of data version objects,
# and a data source type, will produce a list of the Attribute ORM objects. Primarily
# for use with the API, which will accept filter sets from users, who won't be able to
# provide Attribute keys
#
# The returned dict is keyed by source names (as source names must be unique in BigQuery and Solr), with the following
# structure:
# {
#     <source name>: {
#         'id': ID of this Solr collection or BQ table,
#         'alias': <alias for table in BQ queries; required for BQ, unneeded for Solr>,
#         'list': <list of attributes by name>,
#         'attrs': <list of attributes as ORM objects>,
#         'data_type': <data type of the this source, per its version>
#     }
# }
def _build_attr_by_source(attrs, data_version, source_type=DataSource.BIGQUERY, attr_data=None, cache_as=None, active=None):
    
    if cache_as and cache_as in DATA_SOURCE_ATTR:
        attr_by_src = DATA_SOURCE_ATTR[cache_as] 
    else:
        attr_by_src = {'sources': {}}
    
        if not attr_data:
            sources = data_version.get_data_sources(source_type=source_type, active=active)
            attr_data = sources.get_source_attrs(with_set_map=False, for_faceting=False)
            
        for attr in attrs:
            stripped_attr = attr if (not '_' in attr) else \
                attr if not attr.rsplit('_', 1)[1] in ['gt', 'gte','ebtwe','ebtw','btwe', 'btw', 'lte', 'lt'] else \
                attr.rsplit('_', 1)[0]
    
            for id, source in attr_data['sources'].items():
                if stripped_attr in source['list']:
                    source_name = source['name']
                    if source_name not in attr_by_src["sources"]:
                        attr_by_src["sources"][source_name] = {
                            'name': source_name,
                            'id': source['id'],
                            'alias': source_name.split(".")[-1].lower().replace("-", "_"),
                            'list': [attr],
                            'attrs': [stripped_attr],
                            'attr_objs': source['attrs'],
                            'data_type': source['data_sets'].first().data_type,
                            'set_type':  source['data_sets'].first().set_type,
                            'count_col': source['count_col']
                        }
                    else:
                        attr_by_src["sources"][source_name]['list'].append(attr)
                        attr_by_src["sources"][source_name]['attrs'].append(stripped_attr)
        if cache_as:
            DATA_SOURCE_ATTR[cache_as] = attr_by_src

    return attr_by_src


def sortNum(x):
    if x == 'None':
        return float(-1)
    else:
        strt = x.split(' ')[0];
        if strt =='*':
            return float(0)
        else:
            return float(strt)


# Build data exploration context/response
def build_explorer_context(is_dicofdic, source, versions, filters, fields, order_docs, counts_only, with_related,
                           with_derived, collapse_on, is_json, uniques=None, totals=None):
    attr_by_source = {}
    attr_sets = {}
    context = {}
    facet_aggregates = ["StudyInstanceUID","case_barcode","sample_barcode"]
    collex_attr_id = Attribute.objects.get(name='collection_id').id

    try:
        if not is_json:
            context['collection_tooltips'] = Attribute_Tooltips.objects.all().get_tooltips(collex_attr_id)

        collectionSet = Collection.objects.select_related('program').filter(active=True, collection_type=Collection.ORIGINAL_COLLEX)
        collectionsIdList = collectionSet.values_list('collection_id',flat=True)

        versions = versions or DataVersion.objects.filter(active=True)

        data_types = [DataSetType.IMAGE_DATA,]
        with_related and data_types.extend(DataSetType.ANCILLARY_DATA)
        with_derived and data_types.extend(DataSetType.DERIVED_DATA)
        data_sets = DataSetType.objects.filter(data_type__in=data_types)
        sources = data_sets.get_data_sources().filter(
            source_type=source,
            aggregate_level__in=facet_aggregates,
            id__in=versions.get_data_sources().filter(source_type=source).values_list("id", flat=True)
        ).distinct()
        record_source = None
        if collapse_on not in facet_aggregates:
            record_source = data_sets.get_data_sources().filter(
                source_type=source,
                aggregate_level=collapse_on,
                id__in=versions.get_data_sources().filter(source_type=source).values_list("id", flat=True)
            ).distinct().first()

        source_attrs = fetch_data_source_attr(sources, {'for_ui': True, 'with_set_map': True}, cache_as="ui_faceting_set_map")

        source_data_types = fetch_data_source_types(sources)

        for source in sources:
            is_origin = DataSetType.IMAGE_DATA in source_data_types[source.id]
            # If a field list wasn't provided, work from a default set
            if is_origin and not len(fields):
                fields = source.get_attr(for_faceting=False).filter(default_ui_display=True).values_list('name',
                                                                                                         flat=True)

            for dataset in data_sets:
                if dataset.data_type in source_data_types[source.id]:
                    set_type = dataset.get_set_name()
                    if set_type not in attr_by_source:
                        attr_by_source[set_type] = {}
                    attrs = source_attrs['sources'][source.id]['attr_sets'][dataset.id]
                    if 'attributes' not in attr_by_source[set_type]:
                        attr_by_source[set_type]['attributes'] = {}
                        attr_sets[set_type] = attrs
                    else:
                        attr_sets[set_type] = attr_sets[set_type] | attrs

                    attr_by_source[set_type]['attributes'].update(
                        {attr.name: {'source': source.id, 'obj': attr, 'vals': None, 'id': attr.id} for attr in attrs}
                    )

        start = time.time()
        source_metadata = get_collex_metadata(
            filters, fields, record_limit=3000, offset=0, counts_only=counts_only, with_ancillary=with_related,
            collapse_on=collapse_on, order_docs=order_docs, sources=sources, versions=versions, uniques=uniques,
            record_source=record_source, search_child_records_by=None, totals=totals
        )
        stop = time.time()
        logger.debug("[STATUS] Benchmarking: Time to collect metadata for source type {}: {}s".format(
            "BigQuery" if sources.first().source_type == DataSource.BIGQUERY else "Solr",
            str((stop - start))
        ))
        filtered_attr_by_source = copy.deepcopy(attr_by_source)

        for which, _attr_by_source in {'filtered_facets': filtered_attr_by_source,
                                       'facets': attr_by_source}.items():
            facet_counts = source_metadata.get(which,{})
            if not len(facet_counts):
                filtered_attr_by_source = {}
            for source in facet_counts:
                source_name = ":".join(source.split(":")[0:2])
                facet_set = facet_counts[source]['facets']
                for dataset in data_sets:
                    if dataset.data_type in source_data_types[int(source.split(":")[-1])]:
                        set_name = dataset.get_set_name()
                        if dataset.data_type in data_types and set_name in attr_sets:
                            attr_display_vals = Attribute_Display_Values.objects.filter(
                                attribute__id__in=attr_sets[set_name]).to_dict()
                            if dataset.data_type == DataSetType.DERIVED_DATA:
                                attr_cats = attr_sets[set_name].get_attr_cats()
                                for attr in facet_set:
                                    if attr in _attr_by_source[set_name]['attributes']:
                                        source_name = "{}:{}".format(source_name.split(":")[0], attr_cats[attr]['cat_name'])
                                        if source_name not in _attr_by_source[set_name]:
                                            _attr_by_source[set_name][source_name] = {'attributes': {}}
                                        _attr_by_source[set_name][source_name]['attributes'][attr] = \
                                            _attr_by_source[set_name]['attributes'][attr]
                                        this_attr = _attr_by_source[set_name]['attributes'][attr]['obj']
                                        values = []
                                        for val in facet_set[attr]:
                                            if val == 'min_max':
                                                _attr_by_source[set_name][source_name]['attributes'][attr][val] = \
                                                facet_set[attr][val]
                                            else:
                                                displ_val = val if this_attr.preformatted_values else attr_display_vals.get(
                                                    this_attr.id, {}).get(val, None)
                                                values.append({
                                                    'value': val,
                                                    'display_value': displ_val,
                                                    'units': this_attr.units,
                                                    'count': facet_set[attr][val] if val in facet_set[attr] else 0
                                                })
                                        if _attr_by_source[set_name][source_name]['attributes'][attr]['obj'].data_type == 'N':
                                            _attr_by_source[set_name][source_name]['attributes'][attr]['vals'] = sorted(values, key=lambda x: sortNum(x['value']))
                                            if _attr_by_source[set_name][source_name]['attributes'][attr]['vals'][0][
                                                'value'] == 'None':
                                                litem = _attr_by_source[set_name][source_name]['attributes'][attr]['vals'].pop(0)
                                                _attr_by_source[set_name][source_name]['attributes'][attr]['vals'].append(litem)
                                            pass
                                        else:
                                            _attr_by_source[set_name][source_name]['attributes'][attr]['vals'] = sorted(values, key=lambda x: x['value'])
                            else:
                                _attr_by_source[set_name]['All'] = {'attributes': _attr_by_source[set_name]['attributes']}
                                for attr in facet_set:
                                    if attr in _attr_by_source[set_name]['attributes']:
                                        this_attr = _attr_by_source[set_name]['attributes'][attr]['obj']
                                        values = []
                                        for val in facet_counts[source]['facets'][attr]:
                                            if val == 'min_max':
                                                _attr_by_source[set_name]['All']['attributes'][attr][val] = facet_set[attr][
                                                    val]
                                            else:
                                                displ_val = val if this_attr.preformatted_values else attr_display_vals.get(
                                                    this_attr.id, {}).get(val, None)
                                                values.append({
                                                    'value': val,
                                                    'display_value': displ_val,
                                                    'count': facet_set[attr][val] if val in facet_set[attr] else 0
                                                })
                                        if attr == 'bmi':
                                            sortDic = {'underweight': 0, 'normal weight': 1, 'overweight': 2, 'obese': 3,
                                                       'None': 4}
                                            _attr_by_source[set_name]['All']['attributes'][attr]['vals'] = sorted(values, key=lambda x: sortDic[x['value']])
                                        elif _attr_by_source[set_name]['All']['attributes'][attr]['obj'].data_type=='N':
                                            _attr_by_source[set_name]['All']['attributes'][attr]['vals'] = sorted(values, key= lambda x: sortNum(x['value']))
                                            if _attr_by_source[set_name]['All']['attributes'][attr]['vals'][0]['value']=='None':
                                                litem=_attr_by_source[set_name]['All']['attributes'][attr]['vals'].pop(0)
                                                _attr_by_source[set_name]['All']['attributes'][attr]['vals'].append(litem)
                                            pass
                                        else:
                                            _attr_by_source[set_name]['All']['attributes'][attr]['vals'] = sorted(values, key=lambda x: x['value'])

        for which, _attr_by_source in {'filtered_attr_by_source': filtered_attr_by_source, 'attr_by_source': attr_by_source}.items():
            for set in _attr_by_source:
                for source in _attr_by_source[set]:
                    if source == 'attributes':
                        continue
                    if is_dicofdic:
                        for x in list(_attr_by_source[set][source]['attributes'].keys()):
                            if 'min_max' in _attr_by_source[set][source]['attributes'][x]:
                                min_max = _attr_by_source[set][source]['attributes'][x]['min_max']
                            else:
                                min_max = None
                            if (isinstance(_attr_by_source[set][source]['attributes'][x]['vals'], list) and (
                                    len(_attr_by_source[set][source]['attributes'][x]['vals']) > 0)):
                                _attr_by_source[set][source]['attributes'][x] = {y['value']: {
                                    'display_value': y['display_value'], 'count': y['count']
                                } for y in _attr_by_source[set][source]['attributes'][x]['vals']}
                            else:
                                _attr_by_source[set][source]['attributes'][x] = {}
                            if min_max is not None:
                                _attr_by_source[set][source]['attributes'][x]['min_max'] = min_max

                        if set == 'origin_set':
                            context['collections'] = {
                            a: _attr_by_source[set][source]['attributes']['collection_id'][a]['count'] for a in
                            _attr_by_source[set][source]['attributes']['collection_id']}
                            context['collections']['All'] = source_metadata['total']
                    else:
                        if set == 'origin_set':
                            collex = _attr_by_source[set][source]['attributes']['collection_id']
                            if collex['vals']:
                                context['collections'] = {a['value']: a['count'] for a in collex['vals'] if
                                                          a['value'] in collectionsIdList}
                            else:
                                context['collections'] = {a: 0 for a in collectionsIdList}
                            context['collections']['All'] = source_metadata['total']

                        _attr_by_source[set][source]['attributes'] = [{
                            'name': x,
                            'id': _attr_by_source[set][source]['attributes'][x]['obj'].id,
                            'display_name': _attr_by_source[set][source]['attributes'][x]['obj'].display_name,
                            'values': _attr_by_source[set][source]['attributes'][x]['vals'],
                            'units': _attr_by_source[set][source]['attributes'][x]['obj'].units,
                            'min_max': _attr_by_source[set][source]['attributes'][x].get('min_max', None)
                        } for x, val in sorted(_attr_by_source[set][source]['attributes'].items())]

                if not counts_only:
                    _attr_by_source[set]['docs'] = source_metadata['docs']

            for key, source_set in _attr_by_source.items():
                sources = list(source_set.keys())
                for key in sources:
                    if key == 'attributes':
                        source_set.pop(key)

        attr_by_source['total'] = source_metadata['total']
        context['set_attributes'] = attr_by_source
        context['filtered_set_attributes'] = filtered_attr_by_source
        context['filters'] = filters

        prog_attr_id = Attribute.objects.get(name='program_name').id

        programSet = {}
        for collection in collectionSet:
            name = collection.program.short_name if collection.program else collection.name
            if name not in programSet:
                programSet[name] = {
                    'projects': {},
                    'val': 0,
                    'prog_attr_id': prog_attr_id,
                    'collex_attr_id': collex_attr_id
                }
            if collection.collection_id in context['collections']:
                name = collection.program.short_name if collection.program else collection.name
                programSet[name]['projects'][collection.collection_id] = { 'val' :context['collections'][collection.collection_id], 'display':collection.tcia_collection_id }
                programSet[name]['val'] += context['collections'][collection.collection_id]

        if with_related:
            context['tcga_collections'] = Program.objects.get(short_name="TCGA").collection_set.all()

        context['programs'] = programSet

        derived_display_info = {
            'segmentation': {'display_name': 'Segmentation', 'name': 'segmentation'},
            'qualitative': {'display_name': 'Qualitative Analysis', 'name': 'qualitative'},
            'quantitative': {'display_name': 'Quantitative Analysis', 'name': 'quantitative'}
        }

        for key in context['set_attributes'].get('derived_set',{}).keys():
            set_name = key.split(':')[-1]
            if set_name in derived_display_info:
                context['set_attributes']['derived_set'].get(key,{}).update(derived_display_info.get(set_name,{}))

        if is_json:
            attr_by_source['programs'] = programSet
            attr_by_source['filtered_counts'] = filtered_attr_by_source
            if 'uniques' in source_metadata:
                attr_by_source['uniques'] = source_metadata['uniques']
            if 'totals' in source_metadata:
                attr_by_source['totals'] = source_metadata['totals']
            return attr_by_source
        
        return context

    except Exception as e:
        logger.error("[ERROR] While attempting to load the search page:")
        logger.exception(e)

    return None


# Based on the provided settings, fetch faceted counts and/or records from the desired data source type
#
# filters: dict, {<attribute name>: [<val1>, ...]}
# fields: string of fields to include for record returns (ignored if counts_only=True)
# with_ancillary: include anillcary data types in filtering and faceted counting
# with_derived: include derived data types in filtering and faceted counting
# collapse_on: the field used to specify unique counts
# order_docs: array for ordering documents
# sources (optional): List of data sources to query; all active sources will be used if not provided
# versions (optional): List of data versions to query; all active data versions will be used if not provided
# facets: array of strings, attributes to faceted count as a list of attribute names; if not provided no faceted
#   counts will be performed
def get_collex_metadata(filters, fields, record_limit=3000, offset=0, counts_only=False, with_ancillary=True,
                        collapse_on='PatientID', order_docs=None, sources=None, versions=None, with_derived=True,
                        facets=None, records_only=False, sort=None, uniques=None, record_source=None, totals=None,
                        search_child_records_by=None, filtered_needed=True, custom_facets=None, raw_format=False):

    try:
        source_type = sources.first().source_type if sources else DataSource.SOLR

        if not versions:
            versions = ImagingDataCommonsVersion.objects.get(active=True).dataversion_set.all().distinct()
        if not versions.first().active and not sources:
            source_type = DataSource.BIGQUERY

        if not sources:
            data_types = [DataSetType.IMAGE_DATA,]
            with_ancillary and data_types.extend(DataSetType.ANCILLARY_DATA)
            with_derived and data_types.extend(DataSetType.DERIVED_DATA)
            data_sets = DataSetType.objects.filter(data_type__in=data_types)

            sources = data_sets.get_data_sources().filter(source_type=source_type, id__in=versions.get_data_sources().filter(
                source_type=source_type).values_list("id", flat=True)).distinct()

        # Only active data is available in Solr, not archived
        if len(versions.filter(active=False)) and len(sources.filter(source_type=DataSource.SOLR)):
            raise Exception("[ERROR] Can't request archived data from Solr, only BigQuery.")

        start = time.time()
        logger.debug("Metadata fetch beginning:")
        if source_type == DataSource.BIGQUERY:
            results = get_metadata_bq(filters, fields, {
                'filters': sources.get_source_attrs(for_ui=True, for_faceting=False, with_set_map=False),
                'facets': sources.get_source_attrs(for_ui=True, with_set_map=False),
                'fields': sources.get_source_attrs(for_faceting=False, named_set=fields, with_set_map=False)
            }, counts_only, collapse_on, record_limit, offset, search_child_records_by=search_child_records_by)
        elif source_type == DataSource.SOLR:

            results = get_metadata_solr(
                filters, fields, sources, counts_only, collapse_on, record_limit, offset, facets, records_only, sort,
                uniques, record_source, totals, search_child_records_by=search_child_records_by,
                filtered_needed=filtered_needed, custom_facets=custom_facets, raw_format=raw_format
            )
        stop = time.time()
        logger.debug("Metadata received: {}".format(stop-start))
        if not raw_format:
            for counts in ['facets', 'filtered_facets']:
                facet_set = results.get(counts,{})
                for source in facet_set:
                    facets = facet_set[source]['facets']
                    if facets and 'BodyPartExamined' in facets:
                        if 'Kidney' in facets['BodyPartExamined']:
                            if 'KIDNEY' in facets['BodyPartExamined']:
                                facets['BodyPartExamined']['KIDNEY'] += facets['BodyPartExamined']['Kidney']
                            else:
                                facets['BodyPartExamined']['KIDNEY'] = facets['BodyPartExamined']['Kidney']
                            del facets['BodyPartExamined']['Kidney']
                    if not facets:
                        logger.debug("[STATUS] Facets not seen for {}".format(source))

        if not counts_only:
            if 'SeriesNumber' in fields:
                for res in results['docs']:
                    res['SeriesNumber'] = res['SeriesNumber'][0] if 'SeriesNumber' in res else 'None'
            if order_docs:
                results['docs'] = sorted(results['docs'], key=lambda x: tuple([x[item] for item in order_docs]))

    except Exception as e:
        logger.error("[ERROR] While fetching metadata:")
        logger.exception(e)

    return results

def get_table_data(filters,fields,table_type,sources = None, versions = None, custom_facets = None):
    source_type = sources.first().source_type if sources else DataSource.SOLR
    if not versions:
        versions = ImagingDataCommonsVersion.objects.get(active=True).dataversion_set.all().distinct()
    if not sources:
        sources = ImagingDataCommonsVersion.objects.get(active=True).get_data_sources(active=True, source_type=DataSource.SOLR, aggregate_level="StudyInstanceUID")

    custom_facets = None
    collapse_on = 'PatientID'
    record_limit = 2000
    offset = 0
    counts_only = True

    custom_facets = {'uc': {'type': 'terms', 'field': 'PatientID', 'limit': -1, 'missing': True, 'facet': {'unique_count': 'unique(StudyInstanceUID)'}} }


    results = get_metadata_solr(filters, fields, sources, counts_only, collapse_on, record_limit, offset=0,custom_facets=custom_facets,raw_format=False)

    return results


# Based on a solr query array, set of sources, and UI attributes, produce a Solr-compattible queryset
def create_query_set(solr_query, sources, source, all_ui_attrs, image_source, DataSetType):
    query_set = []
    joined_origin = False
    source_data_types = fetch_data_source_types(sources)

    if solr_query:
        for attr in solr_query['queries']:
            attr_name = re.sub("(_ebtwe|_ebtw|_btwe|_btw|_lte|_lt|_gte|_gt)", "", attr)
            # If an attribute from the filters isn't in the attribute listing, just warn and continue
            if attr_name in all_ui_attrs['list']:
                # If the attribute is from this source, just add the query
                if attr_name in all_ui_attrs['sources'][source.id]['list']:
                    query_set.append(solr_query['queries'][attr])
                # If it's in another source for this program, we need to join on that source
                else:
                    for ds in sources:
                        if ds.name != source.name and attr_name in all_ui_attrs['sources'][ds.id]['list']:
                            if DataSetType.IMAGE_DATA in source_data_types[source.id] or DataSetType.IMAGE_DATA in \
                                    source_data_types[ds.id]:
                                joined_origin = True
                            # DataSource join pairs are unique, so, this should only produce a single record
                            source_join = DataSourceJoin.objects.get(from_src__in=[ds.id, source.id],
                                                                     to_src__in=[ds.id, source.id])
                            joined_query = ("{!join %s}" % "from={} fromIndex={} to={}".format(
                                source_join.get_col(ds.name), ds.name, source_join.get_col(source.name)
                            )) + solr_query['queries'][attr]
                            if DataSetType.ANCILLARY_DATA in source_data_types[
                                ds.id] and not DataSetType.ANCILLARY_DATA in source_data_types[source.id]:
                                joined_query = 'has_related:"False" OR _query_:"%s"' % joined_query.replace("\"",
                                                                                                            "\\\"")
                            query_set.append(joined_query)
            else:
                logger.warning("[WARNING] Attribute {} not found in data sources {}".format(attr_name, ", ".join(
                    list(sources.values_list('name', flat=True)))))

    if not joined_origin and not DataSetType.IMAGE_DATA in source_data_types[source.id]:
        source_join = DataSourceJoin.objects.get(from_src__in=[image_source.id, source.id],
                                                 to_src__in=[image_source.id, source.id])
        query_set.append(("{!join %s}" % "from={} fromIndex={} to={}".format(
            source_join.get_col(image_source.name), image_source.name, source_join.get_col(source.name)
        )) + "*:*")

    return query_set


# Use solr to fetch faceted counts and/or records
def get_metadata_solr(filters, fields, sources, counts_only, collapse_on, record_limit, offset=0, facets=None,
                      records_only=False, sort=None, uniques=None, record_source=None, totals=None, cursor=None,
                      search_child_records_by=None, filtered_needed=True, custom_facets=None, sort_field=None,
                      raw_format=False):

    filters = filters or {}
    results = {'docs': None, 'facets': {}}

    if filters:
        results['filtered_facets'] = {}

    source_versions = sources.get_source_versions()

    attrs_for_faceting = None
    if not records_only and facets or facets is None:
        attrs_for_faceting = fetch_data_source_attr(
            sources, {'for_ui': True, 'named_set': facets},
            cache_as="ui_facet_set" if not sources.contains_inactive_versions() and not facets else None
        )

    all_ui_attrs = fetch_data_source_attr(
        sources, {'for_ui': True, 'for_faceting': False},
        cache_as="all_ui_attr" if not sources.contains_inactive_versions() else None)

    source_data_types = fetch_data_source_types(sources)

    image_source = sources.filter(id__in=DataSetType.objects.get(
        data_type=DataSetType.IMAGE_DATA).datasource_set.all()).first()

    # Eventually this will need to go per program
    for source in sources:
        # Uniques and totals are only read from Image Data sources; set the actual field names to None for
        # other set types
        curUniques = uniques if DataSetType.IMAGE_DATA in source_data_types[source.id] else None
        curTotals = totals if DataSetType.IMAGE_DATA in source_data_types[source.id] else None
        start = time.time()
        solr_query = build_solr_query(
            copy.deepcopy(filters),
            with_tags_for_ex=True,
            search_child_records_by=search_child_records_by
        ) if filters else None
        solr_facets = None
        solr_facets_filtered = None
        solr_stats = None
        if not records_only and attrs_for_faceting:
            if not filters:
                solr_facets = fetch_solr_facets({'attrs': attrs_for_faceting['sources'][source.id]['attrs'],
                                                'filter_tags': None, 'unique': source.count_col},
                                                'facet_main_{}'.format(source.id))
                solr_stats = fetch_solr_stats({'filter_tags': None,
                                               'attrs': attrs_for_faceting['sources'][source.id]['attrs']},
                                               'stats_main_{}'.format(source.id))
            else:
                solr_facets = fetch_solr_facets({'attrs': attrs_for_faceting['sources'][source.id]['attrs'],
                                                 'filter_tags': solr_query['filter_tags'] if solr_query else None,
                                                 'unique': source.count_col})
                solr_stats = fetch_solr_stats({'filter_tags': solr_query['filter_tags'] if solr_query else None,
                                               'attrs': attrs_for_faceting['sources'][source.id]['attrs']})

            stop = time.time()
            logger.debug("[STATUS] Time to build Solr facets: {}s".format(stop-start))
            if filters and attrs_for_faceting and filtered_needed:
                solr_facets_filtered = fetch_solr_facets({'attrs': attrs_for_faceting['sources'][source.id]['attrs'], 'unique': source.count_col})
                solr_stats_filtered = fetch_solr_stats({'attrs': attrs_for_faceting['sources'][source.id]['attrs']})

            if custom_facets is not None:
                if solr_facets is None:
                    solr_facets = {}
                solr_facets.update(custom_facets)
                solr_facets = custom_facets
                if filtered_needed:
                    if solr_facets_filtered is None:
                        solr_facets_filtered = {}
                    solr_facets_filtered.update(custom_facets)

        query_set = create_query_set(solr_query, sources, source, all_ui_attrs, image_source, DataSetType)

        stop = time.time()
        logger.debug("[STATUS] Time to build Solr submission: {}s".format(str(stop-start)))

        if not records_only:
            # Get facet counts
            solr_result = query_solr_and_format_result({
                'collection': source.name,
                'facets': solr_facets,
                'fqs': query_set,
                'query_string': None,
                'limit': record_limit,
                'counts_only': True,
                'fields': None,
                'uniques': curUniques,
                'stats': solr_stats,
                'totals': curTotals,
                'sort': sort,
            },raw_format=raw_format)

            solr_count_filtered_result = None
            if solr_facets_filtered:
                solr_count_filtered_result = query_solr_and_format_result({
                    'collection': source.name,
                    'facets': solr_facets_filtered,
                    'fqs': query_set,
                    'query_string': None,
                    'limit': record_limit,
                    'sort': sort_field,
                    'counts_only': True,
                    'fields': None,
                    'stats': solr_stats_filtered,
                    'totals': curTotals
                },raw_format=raw_format)

            stop = time.time()
            logger.info("[BENCHMARKING] Total time to examine source {} and query: {}".format(source.name, str(stop-start)))

            if DataSetType.IMAGE_DATA in source_data_types[source.id] and 'numFound' in solr_result:
                results['total'] = solr_result['numFound']
                if 'uniques' in solr_result:
                    results['uniques'] = solr_result['uniques']

            if raw_format:
                results['facets'] = solr_result['facets']
            else:
                results['facets']["{}:{}:{}".format(source.name, ";".join(source_versions[source.id].values_list("name",flat=True)), source.id)] = {
                    'facets': solr_result.get('facets',None)}

            if solr_count_filtered_result:
                results['filtered_facets']["{}:{}:{}".format(source.name, ";".join(source_versions[source.id].values_list("name",flat=True)), source.id)] = {
                    'facets': solr_count_filtered_result['facets']}

            totals_source = solr_count_filtered_result or solr_result
            if 'totals' in totals_source:
                results['totals'] = totals_source['totals']

        if DataSetType.IMAGE_DATA in source_data_types[source.id] and not counts_only:
            # Get the records
            solr_result = query_solr_and_format_result({
                'collection': source.name if not record_source else record_source.name,
                'fields': list(fields),
                'fqs': query_set,
                'query_string': None,
                'collapse_on': collapse_on,
                'counts_only': counts_only,
                'sort': sort,
                'limit': record_limit,
                'offset': offset if not cursor else 0,
                'with_cursor': cursor
            })

            results['docs'] = solr_result['docs']
            if records_only:
                results['total'] = solr_result['numFound']

    return results


# Use BigQuery to fetch the faceted counts and/or records
def get_metadata_bq(filters, fields, sources_and_attrs, counts_only, collapse_on, record_limit, offset,
                    search_child_records_by=None):
    results = {'docs': None, 'facets': {}}

    try:
        res = get_bq_facet_counts(filters, None, None, sources_and_attrs)
        results['facets'] = res['facets']
        results['total'] = res['facets']['total']

        if not counts_only:
            docs = get_bq_metadata(filters, fields, None, sources_and_attrs, [collapse_on], record_limit, offset,
                                   search_child_records_by=search_child_records_by)
            doc_result_schema = {i: x['name'] for i,x in enumerate(docs['schema']['fields'])}

            results['docs'] = [{
                doc_result_schema[i]: y['v'] for i,y in enumerate(x['f'])
            } for x in docs['results'] ]

    except Exception as e:
        logger.error("[ERROR] During BQ facet and doc fetching:")
        logger.exception(e)
    return results


###################
# BigQuery Metods
###################
#

# Faceted counting for an arbitrary set of filters and facets.
# filters and facets can be provided as lists of names (in which case _build_attr_by_source is used to convert them
# into Attribute objects) or as part of the sources_and_attrs construct, which is a dictionary of objects with the same
# structure as the dict output by _build_attr_by_source.
#
# Queries are structured with the 'image' data type sources as the first table, and all 'ancillary' (i.e. non-image)
# tables as JOINs into the first table. Faceted counts are done on a per attribute basis (though could be restructed
# into a single call). Filters are handled by BigQuery API parameterization, and disabled for faceted bucket counts
# based on their presence in a secondary WHERE clause field which resolves to 'true' if that filter's attribute is the
# attribute currently being counted
def get_bq_facet_counts(filters, facets, data_versions, sources_and_attrs=None):
    filter_attr_by_bq = {}
    facet_attr_by_bq = {}

    counted_total = False
    total = 0

    query_base = """
        #standardSQL
        SELECT {count_clause}
        FROM {table_clause} 
        {join_clause}
        {where_clause}
        GROUP BY {facet}
    """

    count_clause_base = "{sel_count_col}, COUNT(DISTINCT {count_col}) AS count"

    join_clause_base = """
        JOIN `{join_to_table}` {join_to_alias}
        ON {join_to_alias}.{join_to_id} = {join_from_alias}.{join_from_id}
    """

    image_tables = {}

    if not sources_and_attrs:
        if not data_versions or not facets:
            raise Exception("Can't determine facet attributes without facets and versions.")
        filter_attr_by_bq = _build_attr_by_source(list(filters.keys()), data_versions, DataSource.BIGQUERY)
        facet_attr_by_bq = _build_attr_by_source(facets, data_versions, DataSource.BIGQUERY)
    else:
        filter_attr_by_bq = sources_and_attrs['filters']
        facet_attr_by_bq = sources_and_attrs['facets']

    for attr_set in [filter_attr_by_bq, facet_attr_by_bq]:
        for source in attr_set['sources']:
            if attr_set['sources'][source]['data_type'] == DataSetType.IMAGE_DATA:
                image_tables[source] = 1

    table_info = {
        x: {
            'name': y['sources'][x]['name'],
            'alias': y['sources'][x]['name'].split(".")[-1].lower().replace("-", "_"),
            'id': y['sources'][x]['id'],
            'type': y['sources'][x]['data_type'],
            'set': y['sources'][x]['set_type'],
            'count_col': y['sources'][x]['count_col']
        } for y in [facet_attr_by_bq, filter_attr_by_bq] for x in y['sources']
    }

    filter_clauses = {}

    count_jobs = {}
    params = []
    param_sfx = 0

    results = {'facets': {
        'origin_set': {},
        'related_set': {}
    }}

    facet_map = {}

    # We join image tables to corresponding ancillary tables
    for image_table in image_tables:
        tables_in_query = []
        joins = []
        query_filters = []
        if image_table in filter_attr_by_bq['sources']:
            filter_set = {x: filters[x] for x in filters if x in filter_attr_by_bq['sources'][image_table]['list']}
            if len(filter_set):
                filter_clauses[image_table] = BigQuerySupport.build_bq_filter_and_params(
                    filter_set, param_suffix=str(param_sfx), field_prefix=table_info[image_table]['alias'],
                    case_insens=True, with_count_toggle=True, type_schema={'sample_type': 'STRING'}
                )
                param_sfx += 1
                query_filters.append(filter_clauses[image_table]['filter_string'])
                params.append(filter_clauses[image_table]['parameters'])
        tables_in_query.append(image_table)
        for filter_bqtable in filter_attr_by_bq['sources']:
            if filter_bqtable not in image_tables and filter_bqtable not in tables_in_query:
                filter_set = {x: filters[x] for x in filters if x in filter_attr_by_bq['sources'][filter_bqtable]['list']}
                if len(filter_set):
                    filter_clauses[filter_bqtable] = BigQuerySupport.build_bq_filter_and_params(
                        filter_set, param_suffix=str(param_sfx), field_prefix=table_info[filter_bqtable]['alias'],
                        case_insens=True, with_count_toggle=True, type_schema={'sample_type': 'STRING'}
                    )
                    param_sfx += 1

                    source_join = DataSourceJoin.objects.get(
                        from_src__in=[table_info[filter_bqtable]['id'], table_info[image_table]['id']],
                        to_src__in=[table_info[filter_bqtable]['id'], table_info[image_table]['id']]
                    )
                    join_type = ""
                    if table_info[filter_bqtable]['set'] == DataSetType.RELATED_SET:
                        join_type = "LEFT "
                        filter_clauses[filter_bqtable]['filter_string'] = "({} OR {}.{} IS NULL)".format(
                            filter_clauses[filter_bqtable]['filter_string'],
                            table_info[filter_bqtable]['alias'],
                            table_info[filter_bqtable]['count_col']
                        )
                        
                    joins.append(join_clause_base.format(
                        join_type=join_type,
                        join_to_table=table_info[filter_bqtable]['name'],
                        join_to_alias=table_info[filter_bqtable]['alias'],
                        join_to_id=source_join.get_col(table_info[filter_bqtable]['name']),
                        join_from_alias=table_info[image_table]['alias'],
                        join_from_id=source_join.get_col(table_info[image_table]['name'])
                    ))
                    params.append(filter_clauses[filter_bqtable]['parameters'])
                    query_filters.append(filter_clauses[filter_bqtable]['filter_string'])
                    tables_in_query.append(filter_bqtable)
        # Submit jobs, toggling the 'don't filter' var for each facet
        for facet_table in facet_attr_by_bq['sources']:
            for attr_facet in facet_attr_by_bq['sources'][facet_table]['attrs']:
                facet_joins = copy.deepcopy(joins)
                source_join = None
                if facet_table not in image_tables and facet_table not in tables_in_query:
                    source_join = DataSourceJoin.objects.get(
                        from_src__in=[table_info[facet_table]['id'], table_info[image_table]['id']],
                        to_src__in=[table_info[facet_table]['id'], table_info[image_table]['id']]
                    )
                    facet_joins.append(join_clause_base.format(
                        join_from_alias=table_info[image_table]['alias'],
                        join_from_id=source_join.get_col(table_info[image_table]['name']),
                        join_to_alias=table_info[facet_table]['alias'],
                        join_to_table=table_info[facet_table]['name'],
                        join_to_id=source_join.get_col(table_info[facet_table]['name']),
                    ))
                facet = attr_facet.name
                source_set = table_info[facet_table]['set']
                if source_set not in results['facets']:
                    results['facets'][source_set] = { facet_table: {'facets': {}}}
                if facet_table not in results['facets'][source_set]:
                    results['facets'][source_set][facet_table] = {'facets': {}}
                results['facets'][source_set][facet_table]['facets'][facet] = {}
                facet_map[facet] = {'set': source_set, 'source': facet_table}
                filtering_this_facet = facet_table in filter_clauses and facet in filter_clauses[facet_table]['attr_params']
                count_jobs[facet] = {}
                sel_count_col = None
                if attr_facet.data_type == Attribute.CONTINUOUS_NUMERIC:
                    sel_count_col = _get_bq_range_case_clause(
                        attr_facet,
                        table_info[facet_table]['name'],
                        table_info[facet_table]['alias'],
                        source_join.get_col(table_info[facet_table]['name'])
                    )
                else:
                    sel_count_col = "{}.{} AS {}".format(table_info[facet_table]['alias'], facet, facet)
                count_clause = count_clause_base.format(
                    sel_count_col=sel_count_col, count_col="{}.{}".format(table_info[image_table]['alias'], table_info[image_table]['count_col'],))
                count_query = query_base.format(
                    facet=facet,
                    table_clause="`{}` {}".format(table_info[image_table]['name'], table_info[image_table]['alias']),
                    count_clause=count_clause,
                    where_clause="{}".format("WHERE {}".format(" AND ".join(query_filters)) if len(query_filters) else ""),
                    join_clause=""" """.join(facet_joins)
                )
                # Toggle 'don't filter'
                if filtering_this_facet:
                    for param in filter_clauses[facet_table]['attr_params'][facet]:
                        filter_clauses[facet_table]['count_params'][param]['parameterValue']['value'] = 'not_filtering'
                count_jobs[facet]['job'] = BigQuerySupport.insert_query_job(count_query, params if len(params) else None)
                count_jobs[facet]['done'] = False
                # Toggle 'don't filter'
                if filtering_this_facet:
                    for param in filter_clauses[facet_table]['attr_params'][facet]:
                        filter_clauses[facet_table]['count_params'][param]['parameterValue']['value'] = 'filtering'
        # Poll the jobs until they're done, or we've timed out
        not_done = True
        still_checking = True
        num_retries = 0
        while still_checking and not_done:
            not_done = False
            for facet in count_jobs:
                if not count_jobs[facet]['done']:
                    count_jobs[facet]['done'] = BigQuerySupport.check_job_is_done(count_jobs[facet]['job'])
                    if not count_jobs[facet]['done']:
                        not_done = True
            sleep(1)
            num_retries += 1
            still_checking = (num_retries < BQ_ATTEMPT_MAX)

        if not_done:
            logger.error("[ERROR] Timed out while trying to count case/sample totals in BQ")
        else:
            for facet in count_jobs:
                bq_results = BigQuerySupport.get_job_results(count_jobs[facet]['job']['jobReference'])
                for row in bq_results:
                    val = row['f'][0]['v'] if row['f'][0]['v'] is not None else "None"
                    count = row['f'][1]['v']
                    results['facets'][facet_map[facet]['set']][facet_map[facet]['source']]['facets'][facet][val] = int(count)
                    if not counted_total:
                        total += int(count)
                counted_total = True

        results['facets']['total'] = total

    return results


# Fetch the related metadata from BigQuery
# filters: dict filter set
# fields: list of columns to return, string format only
# data_versions: QuerySet<DataVersion> of the data versions(s) to search
# returns: 
#   no_submit is False: { 'results': <BigQuery API v2 result set>, 'schema': <TableSchema Obj> }
#   no_submit is True: { 'sql_string': <BigQuery API v2 compatible SQL Standard SQL parameterized query>,
#     'params': <BigQuery API v2 compatible parameter set> }
def get_bq_metadata(filters, fields, data_version, sources_and_attrs=None, group_by=None, limit=0, 
                    offset=0, order_by=None, order_asc=True, paginated=False, no_submit=False,
                    search_child_records_by=None):

    if not data_version and not sources_and_attrs:
        data_version = DataVersion.objects.select_related('datasettype').filter(active=True)

    ranged_numerics = Attribute.get_ranged_attrs()

    if not group_by:
        group_by = fields
    else:
        if type(group_by) is not list:
            group_by = [group_by]
        group_by.extend(fields)
        group_by = set(group_by)

    filter_attr_by_bq = {}
    field_attr_by_bq = {}
    child_record_search_field = ""

    query_base = """
        SELECT {field_clause}
        FROM {table_clause} 
        {join_clause}
        {where_clause}
        {intersect_clause}
        {group_clause}
        {order_clause}
        {limit_clause}
        {offset_clause}
    """

    if search_child_records_by:
        query_base = """
            SELECT {field_clause}
            FROM {table_clause} 
            {join_clause}
            WHERE {search_by} IN (
                SELECT {search_by}
                FROM {table_clause} 
                {join_clause}
                {where_clause}
                {intersect_clause}
                GROUP BY {search_by}    
            )
            {group_clause}
            {order_clause}
            {limit_clause}
            {offset_clause}
        """

    intersect_base = """
        SELECT {search_by}
        FROM {table_clause} 
        {join_clause}
        {where_clause}
        GROUP BY {search_by}  
    """

    join_type = ""

    join_clause_base = """
        {join_type}JOIN `{filter_table}` {filter_alias}
        ON {field_alias}.{field_join_id} = {filter_alias}.{filter_join_id}
    """

    image_tables = {}
    filter_clauses = {}
    field_clauses = {}

    if len(data_version.filter(active=False)) <= 0:
        sources = data_version.get_data_sources(active=True, source_type=DataSource.BIGQUERY).filter().distinct()
    else:
        sources = data_version.get_data_sources(current=True, source_type=DataSource.BIGQUERY).filter().distinct()

    attr_data = sources.get_source_attrs(with_set_map=False, for_faceting=False)

    if not sources_and_attrs:
        filter_attr_by_bq = _build_attr_by_source(list(filters.keys()), data_version, DataSource.BIGQUERY, attr_data)
        field_attr_by_bq = _build_attr_by_source(fields, data_version, DataSource.BIGQUERY, attr_data)
    else:
        filter_attr_by_bq = sources_and_attrs['filters']
        field_attr_by_bq = sources_and_attrs['fields']

    for attr_set in [filter_attr_by_bq, field_attr_by_bq]:
        for source in attr_set['sources']:
            if attr_set['sources'][source]['data_type'] == DataSetType.IMAGE_DATA:
                image_tables[source] = 1

    # If search_child_records_by isn't None--meaning we want all members of a study or series
    # rather than just the instances--our query is a set of intersections to ensure we find the right
    # series or study
    may_need_intersect = search_child_records_by and bool(len(filters.keys()) > 1)

    table_info = {
        x: {
            'name': y['sources'][x]['name'],
            'alias': y['sources'][x]['name'].split(".")[-1].lower().replace("-", "_"),
            'id': y['sources'][x]['id'],
            'type': y['sources'][x]['data_type'],
            'set': y['sources'][x]['set_type'],
            'count_col': y['sources'][x]['count_col']
        } for y in [field_attr_by_bq, filter_attr_by_bq] for x in y['sources']
    }

    for bqtable in field_attr_by_bq['sources']:
        field_clauses[bqtable] = ",".join(
            ["{}.{}".format(table_info[bqtable]['alias'], x) for x in field_attr_by_bq['sources'][bqtable]['list']]
        )

    for_union = []
    intersect_statements = []
    params = []
    param_sfx = 0

    if order_by:
        new_order = []
        for order in order_by:
            for id, source in attr_data['sources'].items():
                if order in source['list']:
                    order_table = source['name']
                    new_order.append("{}.{}".format(table_info[order_table]['alias'], order))
                    break
        order_by = new_order

    # Failures to find grouping tables typically means the wrong version is being polled for the data sources.
    # Make sure the right version is being used!
    if group_by:
        new_groups = []
        for grouping in group_by:
            group_table = None
            if sources_and_attrs:
                source_set = list(sources_and_attrs['filters']['sources'].keys())
                source_set.extend(list(sources_and_attrs['fields']['sources'].keys()))
                group_table = Attribute.objects.get(active=True, name=grouping).data_sources.all().filter(
                    id__in=set(source_set)
                ).distinct().first()
            else:
                for id, source in attr_data['sources'].items():
                    if grouping in source['list']:
                        group_table = source['name']
                        break
            new_groups.append("{}.{}".format(table_info[group_table]['alias'], grouping))

        group_by = new_groups

    # We join image tables to corresponding ancillary tables, and union between image tables
    for image_table in image_tables:
        tables_in_query = []
        joins = []
        query_filters = []
        regular_filters = {}
        fields = [field_clauses[image_table]] if image_table in field_clauses else []
        if search_child_records_by:
            child_record_search_fields = [y for x, y in field_attr_by_bq['sources'][image_table]['attr_objs'].get_attr_set_types().get_child_record_searches().items() if y is not None]
            child_record_search_field = list(set(child_record_search_fields))[0]
        if image_table in filter_attr_by_bq['sources']:
            filter_set = {x: filters[x] for x in filters if x in filter_attr_by_bq['sources'][image_table]['list']}
            if len(filter_set):
                if may_need_intersect and len(filter_set.keys()) > 1:
                    for filter in filter_set:
                        bq_filter = BigQuerySupport.build_bq_filter_and_params(
                            {filter: filter_set[filter]}, param_suffix=str(param_sfx),
                            field_prefix=table_info[image_table]['alias'],
                            case_insens=True, type_schema=TYPE_SCHEMA, continuous_numerics=ranged_numerics
                        )
                        intersect_statements.append(intersect_base.format(
                            search_by=child_record_search_field,
                            table_clause="`{}` {}".format(
                                table_info[image_table]['name'], table_info[image_table]['alias']
                            ),
                            join_clause="",
                            where_clause="WHERE {}".format(bq_filter['filter_string'])
                        ))
                        params.append(bq_filter['parameters'])
                else:
                    filter_clauses[image_table] = BigQuerySupport.build_bq_filter_and_params(
                        filter_set, param_suffix=str(param_sfx), field_prefix=table_info[image_table]['alias'],
                        case_insens=True, type_schema=TYPE_SCHEMA, continuous_numerics=ranged_numerics
                    )
                param_sfx += 1
                # If we weren't running on intersected sets, append them here as simple filters
                if filter_clauses.get(image_table,None):
                    query_filters.append(filter_clauses[image_table]['filter_string'])
                    params.append(filter_clauses[image_table]['parameters'])
        tables_in_query.append(image_table)
        for filter_bqtable in filter_attr_by_bq['sources']:
            if filter_bqtable not in image_tables and filter_bqtable not in tables_in_query:
                filter_set = {x: filters[x] for x in filters if x in filter_attr_by_bq['sources'][filter_bqtable]['list']}
                if len(filter_set):
                    filter_clauses[filter_bqtable] = BigQuerySupport.build_bq_filter_and_params(
                        filter_set, param_suffix=str(param_sfx), field_prefix=table_info[filter_bqtable]['alias'],
                        case_insens=True, type_schema=TYPE_SCHEMA, continuous_numerics=ranged_numerics
                    )
                    param_sfx += 1

                    source_join = DataSourceJoin.objects.get(
                        from_src__in=[table_info[filter_bqtable]['id'],table_info[image_table]['id']],
                        to_src__in=[table_info[filter_bqtable]['id'],table_info[image_table]['id']]
                    )

                    join_type = ""
                    if table_info[filter_bqtable]['set'] == DataSetType.RELATED_SET:
                        join_type = "LEFT "
                        filter_clauses[filter_bqtable]['filter_string'] = "({} OR {}.{} IS NULL)".format(
                            filter_clauses[filter_bqtable]['filter_string'],
                            table_info[filter_bqtable]['alias'],
                            table_info[filter_bqtable]['count_col']
                        )

                    joins.append(join_clause_base.format(
                        join_type=join_type,
                        filter_alias=table_info[filter_bqtable]['alias'],
                        filter_table=table_info[filter_bqtable]['name'],
                        filter_join_id=source_join.get_col(filter_bqtable),
                        field_alias=table_info[image_table]['alias'],
                        field_join_id=source_join.get_col(image_table)
                    ))
                    params.append(filter_clauses[filter_bqtable]['parameters'])
                    query_filters.append(filter_clauses[filter_bqtable]['filter_string'])
                    tables_in_query.append(filter_bqtable)

        # Any remaining field clauses not pulled are for tables not being filtered and which aren't the image table,
        # so we add them last
        for field_bqtable in field_attr_by_bq['sources']:
            if field_bqtable not in image_tables and field_bqtable not in tables_in_query:
                if len(field_clauses[field_bqtable]):
                    fields.append(field_clauses[field_bqtable])
                source_join = DataSourceJoin.objects.get(
                    from_src__in=[table_info[field_bqtable]['id'], table_info[image_table]['id']],
                    to_src__in=[table_info[field_bqtable]['id'], table_info[image_table]['id']]
                )
                joins.append(join_clause_base.format(
                    join_type=join_type,
                    field_alias=table_info[image_table]['alias'],
                    field_join_id=source_join.get_col(table_info[image_table]['name']),
                    filter_alias=table_info[field_bqtable]['alias'],
                    filter_table=table_info[field_bqtable]['name'],
                    filter_join_id=source_join.get_col(table_info[field_bqtable]['name'])
                ))

        intersect_clause = ""
        if len(intersect_statements):
            intersect_clause = """
                INTERSECT DISTINCT
            """.join(intersect_statements)

        for_union.append(query_base.format(
            field_clause= ",".join(fields),
            table_clause="`{}` {}".format(table_info[image_table]['name'], table_info[image_table]['alias']),
            join_clause=""" """.join(joins),
            where_clause="{}".format("WHERE {}".format(" AND ".join(query_filters) if len(query_filters) else "") if len(filters) else ""),
            intersect_clause="{}".format("" if not len(intersect_statements) else "{}{}".format(
                " AND " if len(regular_filters) else "","{} IN ({})".format(
                    child_record_search_field, intersect_clause
            ))),
            order_clause="{}".format("ORDER BY {}".format(", ".join([
                "{} {}".format(x, "ASC" if order_asc else "DESC") for x in order_by
            ])) if order_by and len(order_by) else ""),
            group_clause="{}".format("GROUP BY {}".format(", ".join(group_by)) if group_by and len(group_by) else ""),
            limit_clause="{}".format("LIMIT {}".format(str(limit)) if limit > 0 else ""),
            offset_clause="{}".format("OFFSET {}".format(str(offset)) if offset > 0 else ""),
            search_by=child_record_search_field
        ))

    full_query_str =  """
            #standardSQL
    """ + """UNION DISTINCT""".join(for_union)

    settings.DEBUG and logger.debug("[STATUS] get_bq_metadata: {}".format(full_query_str))

    if no_submit:
        results = {"sql_string": full_query_str, "params": params}
    else:
        results = BigQuerySupport.execute_query_and_fetch_results(full_query_str, params, paginated=paginated)

    return results


# For faceted counting of continuous numeric fields, ranges must be constructed so the faceted counts are properly
# bucketed. This method makes use of the Attribute_Ranges ORM object, and requires this be set for an attribute
# in order to build a range clause.
#
# Attributes must be passed in as a proper Attribute ORM object
def _get_bq_range_case_clause(attr, table, alias, count_on, include_nulls=True):
    ranges = Attribute_Ranges.objects.filter(attribute=attr)
    ranges_case = []

    for attr_range in ranges:
        if attr_range.gap == "0":
            # This is a single range, no iteration to be done
            if attr_range.first == "*":
                ranges_case.append(
                    "WHEN {}.{} < {} THEN '{}'".format(alias, attr.name, str(attr_range.last), attr_range.label))
            elif attr_range.last == "*":
                ranges_case.append(
                    "WHEN {}.{} > {} THEN '{}'".format(alias, attr.name, str(attr_range.first), attr_range.label))
            else:
                ranges_case.append(
                    "WHEN {}.{} BETWEEN {} AND {} THEN '{}'".format(alias, attr.name, str(attr_range.first),
                                                                   str(attr_range.last), attr_range.label))
        else:
            # Iterated range
            cast = int if attr_range.type == Attribute_Ranges.INT else float
            gap = cast(attr_range.gap)
            last = cast(attr_range.last)
            lower = cast(attr_range.first)
            upper = cast(attr_range.first) + gap

            if attr_range.unbounded:
                upper = lower
                lower = "*"

            while lower == "*" or lower < last:
                if lower == "*":
                    ranges_case.append(
                        "WHEN {}.{} < {} THEN {}".format(alias, attr.name, str(upper), "'* TO {}'".format(str(upper))))
                else:
                    ranges_case.append(
                        "WHEN {}.{} BETWEEN {} AND {} THEN {}".format(alias, attr.name, str(lower),
                                                                       str(upper), "'{} TO {}'".format(str(lower),str(upper))))
                lower = upper
                upper = lower + gap

            # If we stopped *at* the end, we need to add one last bucket.
            if attr_range.unbounded:
                ranges_case.append(
                    "WHEN {}.{} > {} THEN {}".format(alias, attr.name, str(attr_range.last), "'{} TO *'".format(str(attr_range.last))))

    if include_nulls:
        ranges_case.append(
            "WHEN {}.{} IS NULL THEN 'none'".format(alias, attr.name))

    case_clause = "(CASE {} END) AS {}".format(" ".join(ranges_case), attr.name)

    return case_clause


# Fetch the related metadata from BigQuery
# filters: dict filter set
# fields: list of columns to return, string format only
# data_versions: QuerySet<DataVersion> of the data versions(s) to search
# returns: { 'results': <BigQuery API v2 result set>, 'schema': <TableSchema Obj> }
def get_bq_string(filters, fields, data_version, sources_and_attrs=None, group_by=None, limit=0, offset=0,
                    order_by=None, order_asc=True, search_child_records_by=None):
    if not data_version and not sources_and_attrs:
        data_version = DataVersion.objects.selected_related('datasettype').filter(active=True)

    if not group_by:
        group_by = fields
    else:
        if type(group_by) is not list:
            group_by = [group_by]
        group_by.extend(fields)
        group_by = set(group_by)

    filter_attr_by_bq = {}
    field_attr_by_bq = {}

    query_base = """
        SELECT {field_clause}
        FROM {table_clause} 
        {join_clause}
        {where_clause}
        {group_clause}
        {order_clause}
        {limit_clause}
        {offset_clause}
    """

    join_type=""

    join_clause_base = """
        {join_type}JOIN `{filter_table}` {filter_alias}
        ON {field_alias}.{field_join_id} = {filter_alias}.{filter_join_id}
    """

    image_tables = {}


    if len(data_version.filter(active=False)) <= 0:
        sources = data_version.get_data_sources(active=True, source_type=DataSource.BIGQUERY).filter().distinct()
    else:
        sources = data_version.get_data_sources(current=True, source_type=DataSource.BIGQUERY).filter().distinct()
    # sources = data_version.get_data_sources(active=True, source_type=DataSource.BIGQUERY).filter().distinct()
    attr_data = sources.get_source_attrs(with_set_map=False, for_faceting=False)


    if not sources_and_attrs:
        filter_attr_by_bq = _build_attr_by_source(list(filters.keys()), data_version, DataSource.BIGQUERY, attr_data)
        field_attr_by_bq = _build_attr_by_source(fields, data_version, DataSource.BIGQUERY, attr_data)
    else:
        filter_attr_by_bq = sources_and_attrs['filters']
        field_attr_by_bq = sources_and_attrs['fields']

    for attr_set in [filter_attr_by_bq, field_attr_by_bq]:
        for source in attr_set['sources']:
            if attr_set['sources'][source]['data_type'] == DataSetType.IMAGE_DATA:
                image_tables[source] = 1

    table_info = {
        x: {
            'name': y['sources'][x]['name'],
            'alias': y['sources'][x]['name'].split(".")[-1].lower().replace("-", "_"),
            'id': y['sources'][x]['id']
        } for y in [field_attr_by_bq, filter_attr_by_bq] for x in y['sources']
    }

    filter_clauses = {}
    field_clauses = {}

    for bqtable in field_attr_by_bq['sources']:
        field_clauses[bqtable] = ",".join(
            ["{}.{}".format(table_info[bqtable]['alias'], x) for x in field_attr_by_bq['sources'][bqtable]['list']])

    for_union = []

    if order_by:
        new_order = []
        for order in order_by:
            for id, source in attr_data['sources'].items():
                if order in source['list']:
                    order_table = source['name']
                    new_order.append("{}.{}".format(table_info[order_table]['alias'], order))
                    break
        order_by = new_order

    if group_by:
        new_groups = []
        for grouping in group_by:
            group_table = None
            if sources_and_attrs:
                source_set = list(sources_and_attrs['filters']['sources'].keys())
                source_set.extend(list(sources_and_attrs['fields']['sources'].keys()))
                group_table = Attribute.objects.get(active=True, name=grouping).data_sources.all().filter(
                    id__in=set(source_set)).distinct().first()
            else:
                for id, source in attr_data['sources'].items():
                    if grouping in source['list']:
                        group_table = source['name']
                        break
            new_groups.append("{}.{}".format(table_info[group_table]['alias'], grouping))

        group_by = new_groups

    # We join image tables to corresponding ancillary tables, and union between image tables
    for image_table in image_tables:
        tables_in_query = []
        joins = []
        query_filters = []
        fields = [field_clauses[image_table]] if image_table in field_clauses else []
        if image_table in filter_attr_by_bq['sources']:
            filter_set = {x: filters[x] for x in filters if x in filter_attr_by_bq['sources'][image_table]['list']}
            if len(filter_set):
                filter_clauses[image_table] = BigQuerySupport.build_bq_where_clause(
                        filter_set, field_prefix=table_info[image_table]['alias'], type_schema=TYPE_SCHEMA)
                query_filters.append(filter_clauses[image_table])
        tables_in_query.append(image_table)
        for filter_bqtable in filter_attr_by_bq['sources']:
            if filter_bqtable not in image_tables and filter_bqtable not in tables_in_query:
                filter_set = {x: filters[x] for x in filters if
                              x in filter_attr_by_bq['sources'][filter_bqtable]['list']}
                if len(filter_set):
                    filter_clauses[filter_bqtable] = BigQuerySupport.build_bq_where_clause(
                        filter_set, field_prefix=table_info[filter_bqtable]['alias'], type_schema=TYPE_SCHEMA)
                    source_join = DataSourceJoin.objects.get(
                        from_src__in=[table_info[filter_bqtable]['id'], table_info[image_table]['id']],
                        to_src__in=[table_info[filter_bqtable]['id'], table_info[image_table]['id']]
                    )

                    joins.append(join_clause_base.format(
                        join_type=join_type,
                        filter_alias=table_info[filter_bqtable]['alias'],
                        filter_table=table_info[filter_bqtable]['name'],
                        filter_join_id=source_join.get_col(filter_bqtable),
                        field_alias=table_info[image_table]['alias'],
                        field_join_id=source_join.get_col(image_table)
                    ))
                    query_filters.append(filter_clauses[filter_bqtable])
                    tables_in_query.append(filter_bqtable)

        # Any remaining field clauses not pulled are for tables not being filtered and which aren't the image table,
        # so we add them last
        for field_bqtable in field_attr_by_bq['sources']:
            if field_bqtable not in image_tables and field_bqtable not in tables_in_query:
                if len(field_clauses[field_bqtable]):
                    fields.append(field_clauses[field_bqtable])
                source_join = DataSourceJoin.objects.get(
                    from_src__in=[table_info[field_bqtable]['id'], table_info[image_table]['id']],
                    to_src__in=[table_info[field_bqtable]['id'], table_info[image_table]['id']]
                )
                joins.append(join_clause_base.format(
                    join_type=join_type,
                    field_alias=table_info[image_table]['alias'],
                    field_join_id=source_join.get_col(table_info[image_table]['name']),
                    filter_alias=table_info[field_bqtable]['alias'],
                    filter_table=table_info[field_bqtable]['name'],
                    filter_join_id=source_join.get_col(table_info[field_bqtable]['name'])
                ))

        for_union.append(query_base.format(
            field_clause=",".join(fields),
            table_clause="`{}` {}".format(table_info[image_table]['name'], table_info[image_table]['alias']),
            join_clause=""" """.join(joins),
            where_clause="{}".format("WHERE {}".format(" AND ".join(query_filters)) if len(query_filters) else ""),
            order_clause="{}".format("ORDER BY {}".format(
                ", ".join(["{} {}".format(x, "ASC" if order_asc else "DESC") for x in order_by])) if order_by and len(
                order_by) else ""),
            group_clause="{}".format("GROUP BY {}".format(", ".join(group_by)) if group_by and len(group_by) else ""),
            limit_clause="{}".format("LIMIT {}".format(str(limit)) if limit > 0 else ""),
            offset_clause="{}".format("OFFSET {}".format(str(offset)) if offset > 0 else "")
        ))

    full_query_str = """
            #standardSQL
    """ + """UNION DISTINCT""".join(for_union)

    return full_query_str
