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

from oauth2client.client import GoogleCredentials
from django.conf import settings
from .utils import build_with_retries

STORAGE_SCOPES = [
    'https://www.googleapis.com/auth/devstorage.read_only',
    'https://www.googleapis.com/auth/devstorage.read_write',
    'https://www.googleapis.com/auth/devstorage.full_control'
]

# WJRL 4/25/25 This used in several places.
def get_storage_resource():
    credentials = GoogleCredentials.get_application_default().create_scoped(STORAGE_SCOPES)
    service = build_with_retries('storage', 'v1', credentials, 2)
    return service
