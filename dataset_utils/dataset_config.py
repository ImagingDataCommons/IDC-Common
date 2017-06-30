"""

Copyright 2017, Institute for Systems Biology

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

import logging

from jsonschema import validate as schema_validate, ValidationError

logger = logging.getLogger(__name__)

from enum import Enum

from accounts.utils import ServiceObjectBase

from dataset_utils.nih_auth_list import NIHDatasetAuthorizationList
from dataset_utils.gcs_support import GCSSupportConcrete


class DatasetConfiguration(ServiceObjectBase):
    SCHEMA = {
        "type": "object",
        "properties": {
            "authorization_list_files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "oneOf": [
                        {"$ref": "#/definitions/nih_dbgap_auth_list"},
                        {"$ref": "#/definitions/sanger_cosmic_auth_list"}
                    ],
                    "required": ["gcs_path"]
                }
            }
        },
        "required": [
            "authorization_list_files"
        ],
        "definitions": {
            "nih_dbgap_auth_list": {
                "type": "object",
                "properties": {
                    "type": {"enum": ["nih-dbgap"]},
                    "dataset_name": {"type": "string"},
                    "acl_group": {"type": "string"},
                    "dataset_id": {"type": "string"}
                },
                "required": ["type", "acl_group", "dataset_name", "gcs_path"]
            },
            "sanger_cosmic_auth_list": {
                "type": "object",
                "properties": {
                    "type": {"enum": ["sanger-cosmic"]},
                    "acl_group": {"type": "string"},
                    "logging_config": {"$ref": "#/definitions/logging_config"}
                },
                "required": ["type", "acl_group", "gcs_path", "logging_config"]
            },
            'logging_config': {
                "type": "object",
                "properties": {
                    "log_name": {"type": "string"}
                },
                "required": ["log_name"]
            }
        }
    }

    def __init__(self, authorization_list_files):
        self.authorization_list_files = authorization_list_files

    @classmethod
    def from_dict(cls, data):
        """

        Throws:
            ValidationError if the data object does not match the required schema.
        """
        schema_validate(data, cls.SCHEMA)

        # TODO Validate that dataset IDs are unique

        return cls(data['authorization_list_files'])


class GetDatasetsStatus(Enum):
    GET_DATASETS_STATUS_OK = 1
    GET_DATASETS_STATUS_ERROR = 2


class DatasetGoogleGroupPair(object):
    def __init__(self, dataset_id, google_group_name):
        self.dataset_id = dataset_id
        self.google_group_name = google_group_name


class DatasetAccessSupport(object):
    def __init__(self, dataset_config, gcs_support):
        self.dataset_config = dataset_config
        self.gcs_support = gcs_support
        self.authorization_list_map = {}

    @classmethod
    def from_webapp_django_settings(cls):
        """
        Builds and instance from a Django settings object.
        
        Creates a DatasetConfiguration instance using a JSON configuration file assumed to be
        present on the local file system in a path indicated by DATASET_CONFIGURATION_PATH.
        
        The above DatasetConfiguration instance and GCSSupportConcrete are then used to create
        an instance of this class.
        """
        from django.conf import settings as django_settings
        config_file_path = django_settings.DATASET_CONFIGURATION_PATH
        dataset_config = DatasetConfiguration.from_json_file_path(config_file_path)
        gcs_support = GCSSupportConcrete()

        return cls(dataset_config, gcs_support)

    def get_nih_dbgap_auth_lists(self):
        result = []
        for dataset_item in self.dataset_config.authorization_list_files:
            if dataset_item['type'] == 'nih-dbgap':
                result.append(dataset_item)

        return result

    def get_auth_list_gcs_path_for_dataset_id(self, dataset_id):
        """
        Answers the GCS bucket and object names of an authorization list file given a dataset ID.
        """
        # Is a dataset configured for this identifier?
        auth_list_config = None
        for dataset_item in self.get_nih_dbgap_auth_lists():
            if dataset_item["dataset_id"] == dataset_id:
                auth_list_config = dataset_item

        # TODO Implement error handling
        if auth_list_config is None:
            raise Exception("No auth list config for {}".format(dataset_id))

        full_gcs_path = auth_list_config['gcs_path']
        return full_gcs_path

    def get_auth_list_instance_for_dataset_id(self, dataset_id):
        # Has this already been loaded?
        if dataset_id in self.authorization_list_map:
            return self.authorization_list_map[dataset_id]

        auth_list_gcs_path = self.get_auth_list_gcs_path_for_dataset_id(dataset_id)
        auth_list_data = self.gcs_support.get_data_from_gcs_path(auth_list_gcs_path)
        auth_list_instance = NIHDatasetAuthorizationList.from_stream(auth_list_data)

        self.authorization_list_map[dataset_id] = auth_list_instance
        return auth_list_instance

    def is_era_login_in_authorization_list(self, era_login_name, dataset_id):
        auth_list = self.get_auth_list_instance_for_dataset_id(dataset_id)
        return auth_list.is_era_login_active(era_login_name)

    def get_datasets_for_era_login(self, user=None):
        """
        Answer the data sets an ERA user has access to.

        Returns: Array of DatasetGoogleGroupPair instances.
        """
        result = []
        
        for dataset_item in self.get_nih_dbgap_auth_lists():
            if self.is_era_login_in_authorization_list(user, dataset_item['dataset_id']):
                result.append(DatasetGoogleGroupPair(dataset_item['dataset_id'], dataset_item['acl_group']))
        
        return result
    
    def get_all_datasets_and_google_groups(self):
        """
        Returns a list of data set ID pairs and Google Group names.

        Returns: Array of DatasetGoogleGroupPair instances.
        """
        result = []
        for dataset_item in self.get_nih_dbgap_auth_lists():
            result.append(DatasetGoogleGroupPair(dataset_item['dataset_id'], dataset_item['acl_group']))
        
        return result
    
    def get_google_group_name_for_dataset_id(self, dataset_id):
        """
        Answers Google Group name for a dataset ID.
        """
        google_group_name = None

        for dataset_item in self.get_all_datasets_and_google_groups():
            if dataset_item.dataset_id == dataset_id:
                google_group_name = dataset_item.google_group_name
                break
        
        return google_group_name
