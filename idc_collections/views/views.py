#
# Copyright 2015-2025, Institute for Systems Biology
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

from builtins import str
from copy import deepcopy
import re
import sys
from django.shortcuts import render, redirect
from django.core import serializers
from django.core.exceptions import ObjectDoesNotExist
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.views.decorators.cache import never_cache, cache_page
from django.contrib import messages
from django.conf import settings
from django.db.models import Q
from django.http import JsonResponse, HttpResponseNotFound, HttpResponse, JsonResponse
from django.conf import settings
from django.db import connection
from django.urls import reverse
from collections import OrderedDict
from idc_collections.models import User_Feature_Definitions, User_Feature_Counts, \
    Program, Collection, Citation
from solr_helpers import *
from sharing.service import create_share
from googleapiclient.errors import HttpError

import json
import requests
import logging

logger = logging.getLogger(__name__)

DENYLIST_RE = settings.DENYLIST_RE


@cache_page(60 *15)
def collection_list(request):
    template = 'collections/collections_list.html'

    active_collections = Collection.objects.filter(active=True, access="Public")
    inactive_collections = Collection.objects.filter(active=False, access="Public")
    descs = {x.collection_id: x.description for x in active_collections}

    context = {
        'active_collections': active_collections,
        'inactive_collections': inactive_collections,
        'active_collection_descs': descs
    }

    return render(request, template, context)


@cache_page(60 *15)
def collection_details(request, collection_id):
    template = 'collections/collection_details.html'
    try:
        try:
            collex = Collection.objects.get(collection_id=collection_id)
        except ObjectDoesNotExist:
            non_numeric = re.search('[^\d]+',collection_id)
            if non_numeric:
                raise ObjectDoesNotExist
            else:
                collex = Collection.objects.get(id=int(collection_id))

        context = {
            'collection_name': collex.name,
            'collection_id': collex.collection_id,
            'subject_count': collex.subject_count,
            'image_types': collex.image_types,
            'desc': collex.description,
            'total_size': collex.total_size,
            'total_size_with_ar': collex.total_size_with_ar,
            'cancer_type': collex.cancer_type,
            'dois': collex.doi.split(" "),
            'species': collex.species,
            'supporting_data': collex.supporting_data,
            'species': collex.species,
            'citations': collex.get_citations(),
            'primary_tumor_location': collex.location,
            'license': collex.license.split("::"),
            'collection_type': "Collection" if collex.collection_type == Collection.ORIGINAL_COLLEX else "Analysis Result",
             'collections': collex.collections.split(", ") if collex.collection_type == Collection.ANALYSIS_COLLEX else "",
            'analysis_artifacts': collex.analysis_artifacts
        }

        return render(request, template, context)
    except ObjectDoesNotExist:
        messages.error(request, 'Requested collection not found.')
    except Exception as e:
        logger.error("[ERROR] While attempting to open a collection with the ID {}:".format(collection_id))
        logger.exception(e)
        messages.error(request, "There was an error while processing your request.")
    return redirect(reverse('collections'))
