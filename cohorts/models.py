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

from builtins import str
from builtins import object
import operator
import string
import sys
import logging
from django.db import models
from django.db.models import Count
from django.contrib.auth.models import User
from django.db.models import Q
from django.utils.html import escape
from idc_collections.models import Collection, Attribute, User_Feature_Definitions, DataVersion
from django.core.exceptions import ObjectDoesNotExist
from sharing.models import Shared_Resource
from functools import reduce
from google_helpers.bigquery.bq_support import BigQuerySupport

logger = logging.getLogger('main_logger')


class CohortManager(models.Manager):
    def search(self, search_terms):
        terms = [term.strip() for term in search_terms.split()]
        q_objects = []
        for term in terms:
            q_objects.append(Q(name__icontains=term))

        # Start with a bare QuerySet
        qs = self.get_queryset()

        # Use operator's or_ to string together all of your Q objects.
        return qs.filter(reduce(operator.and_, [reduce(operator.or_, q_objects), Q(active=True)]))

    def get_all_tcga_cohort(self):
        isb_user = User.objects.get(is_superuser=True, username='isb')
        all_isb_cohort_ids = Cohort_Perms.objects.filter(user=isb_user, perm=Cohort_Perms.OWNER).values_list('cohort_id', flat=True)
        return Cohort.objects.filter(name='All TCGA Data', id__in=all_isb_cohort_ids)[0]


class Cohort(models.Model):
    id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255, null=False, blank=True)
    description = models.TextField(null=True, blank=True)
    data_version = models.IntegerField(blank=False, null=False, default=1)
    active = models.BooleanField(default=True)
    objects = CohortManager()
    shared = models.ManyToManyField(Shared_Resource)

    def get_collections(self):
        collex_filters = self.filters_set.values_list('collection_id', flat=True).distinct()
        return Collection.objects.filter(active=True, id__in=collex_filters).distinct()

    def get_collection_names(self):
        collex = self.get_collections()
        names = collex.distinct().values_list('name', flat=True)
        return [str(x) for x in names]

    def only_user_data(self):
        return bool(Collection.objects.filter(id__in=self.get_collections(), is_public=True).count() <= 0)

    '''
    Returns the highest level of permission the user has.
    '''
    def get_perm(self, request):
        perm = self.cohort_perms_set.filter(user_id=request.user.id).order_by('perm')

        if perm.count() > 0:
            return perm[0]
        else:
            return None

    def get_owner(self):
        return self.cohort_perms_set.filter(perm=Cohort_Perms.OWNER)[0].user

    def is_public(self):
        isbuser = User.objects.get(username='isb', is_superuser=True)
        return (self.cohort_perms_set.filter(perm=Cohort_Perms.OWNER)[0].user_id == isbuser.id)

    # Produce a BigQuery filter WHERE clause for use in the console
    #
    def get_bq_filter_string(self, prefix=None):

        filter_sets = []

        group_filter_dict = self.get_filters_as_dict()

        for group in group_filter_dict:
            filter_sets.append(BigQuerySupport.build_bq_where_clause(
                group_filter_dict[group], field_prefix=prefix
            ))

        # TODO: For now there will only be a single group, but in the future if there are multiple groups
        # we will need to consider how to return this

        return filter_sets[0]

    # Produce a BigQuery filter clause and parameters; this is for *programmatic* use of BQ, NOT copy-paste into
    # the console
    #
    def get_filters_for_bq(self, prefix=None, suffix=None, counts=False, schema=None):

        filter_sets = []

        group_filter_dict = self.get_filters_as_dict()

        for group in group_filter_dict:
            filter_sets.append(BigQuerySupport.build_bq_filter_and_params(
                group_filter_dict[group], field_prefix=prefix, param_suffix=suffix, with_count_toggle=counts,
                type_schema=schema
                  ))

        # TODO: For now there will only be a single group, but in the future if there are multiple groups
        # we will need to consider how to return this

        return filter_sets[0]

    # Get a simple dict of the attributes and values of this cohort; note this is NOT intended for UI display
    #
    def get_filters_as_dict(self):
        filter_dict = {}

        groups = self.filter_group_set.all()

        for group in groups:
            filter_dict[group.id] = {}
            filter_group = filter_dict[group.id]
            filters = group.filter_set.all()
            for filter in filters:
                filter_group[filter.attribute.name] = filter.value.split(",")
                if filter.attribute.data_type == filter.attribute.CONTINUOUS_NUMERIC:
                    filter_group[filter.attribute.name] = [int(x) if "." not in x else float(x) for x in filter_dict[filter.attribute.name]]

        return filter_dict

    # Returns the list of filters used to create this cohort
    #
    def get_filters(self, with_display_vals=False):
        filter_list = Filters.objects.filter(resulting_cohort=self)
        dict_filters = {}

        attribute_display_vals = {}

        for filter in filter_list:
            if with_display_vals and filter.attribute.id not in attribute_display_vals:
                attribute_display_vals[filter.attribute.id] = filter.attribute.get_display_values()
            if filter.collection.short_name not in dict_filters:
                dict_filters[filter.collection.name] = {}
            collex_filters = dict_filters[filter.collection.name]
            if filter.attribute.name not in collex_filters:
                collex_filters[filter.attribute.name] = {}
            values = collex_filters[filter.attribute.name]
            if filter.value not in values:
                values[filter.value] = attribute_display_vals[filter.filter.attribute.id][filter.value] if with_display_vals else filter.value

        return dict_filters


# A 'source' Cohort is a cohort which was used to produce a subsequent cohort, either via cloning, editing,
# or set operations
class Source(models.Model):
    SET_OPS = 'SET_OPS'
    CLONE = 'CLONE'
    SOURCE_TYPES = (
        (SET_OPS, 'Set Operations'),
        (CLONE, 'Clone')
    )

    parent = models.ForeignKey(Cohort, null=True, blank=True, related_name='source_parent', on_delete=models.CASCADE)
    cohort = models.ForeignKey(Cohort, null=False, blank=False, related_name='source_cohort', on_delete=models.CASCADE)
    type = models.CharField(max_length=10, choices=SOURCE_TYPES)
    notes = models.CharField(max_length=1024, blank=True)


class Cohort_Perms(models.Model):
    READER = 'READER'
    OWNER = 'OWNER'
    PERMISSIONS = (
        (READER, 'Reader'),
        (OWNER, 'Owner')
    )
    cohort = models.ForeignKey(Cohort, null=False, blank=False, on_delete=models.CASCADE)
    user = models.ForeignKey(User, null=False, blank=True, on_delete=models.CASCADE)
    perm = models.CharField(max_length=10, choices=PERMISSIONS, default=READER)


class Filter_Group(models.Model):
    AND = 'A'
    OR = 'O'
    OPS = (
        (AND, 'And'),
        (OR, 'Or')
    )
    id = models.AutoField(primary_key=True)
    resulting_cohort = models.ForeignKey(Cohort, null=False, blank=False, on_delete=models.CASCADE)
    operator = models.CharField(max_length=1, blank=False, null=False, choices=OPS, default=OR)
    version = models.ManyToManyField(DataVersion)

    @classmethod
    def get_op(cls, op_string):
        if op_string.lower() == 'and':
            return Filter_Group.AND
        elif op_string.lower() == 'or':
            return Filter_Group.OR
        else:
            return None
    

class Filters(models.Model):
    resulting_cohort = models.ForeignKey(Cohort, null=False, blank=False, on_delete=models.CASCADE)
    attribute = models.ForeignKey(Attribute, null=False, blank=False, on_delete=models.CASCADE)
    value = models.CharField(max_length=256, null=False, blank=False)
    filter_group = models.ForeignKey(Filter_Group, null=True, blank=True, on_delete=models.CASCADE)
    feature_def = models.ForeignKey(User_Feature_Definitions, null=True, blank=True, on_delete=models.CASCADE)


class Cohort_Comments(models.Model):
    cohort = models.ForeignKey(Cohort, blank=False, related_name='cohort_comment', on_delete=models.CASCADE)
    user = models.ForeignKey(User, null=False, blank=False, on_delete=models.CASCADE)
    date_created = models.DateTimeField(auto_now_add=True)
    content = models.CharField(max_length=1024, null=False)
