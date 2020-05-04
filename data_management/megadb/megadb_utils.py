"""
Functions to download the `datasets` and `splits` tables, which are small.

The environment variables COSMOS_ENDPOINT and COSMOS_KEY need to be set
or passed in to the initializer.
"""

import os
import humanfriendly
from datetime import datetime

from azure.cosmos.cosmos_client import CosmosClient
from azure.storage.blob import BlockBlobService


class MegadbUtils:

    def __init__(self, url=None, key=None):
        if not url:
            url = os.environ['COSMOS_ENDPOINT']
        if not key:
            key = os.environ['COSMOS_KEY']
        client = CosmosClient(url, credential=key)
        self.database = client.get_database_client('camera-trap')
        self.container_sequences = self.database.get_container_client('sequences')


    def get_datasets_table(self):
        """

        Returns: a dict where the keys are the `dataset` property in sequences and splits,
                 and the values are properties of the dataset

        """
        query = '''SELECT * FROM datasets d'''

        container_datasets = self.database.get_container_client('datasets')
        result_iterable = container_datasets.query_items(query=query, enable_cross_partition_query=True)

        datasets = {i['dataset_name']: {k: v for k, v in i.items() if not k.startswith('_')} for i in
                    iter(result_iterable)}
        return datasets


    def get_splits_table(self):
        """

        Returns: a dict where the key is the name of the `dataset` and value is a dict with
        the train, val and test splits by location as *sets*.

        """
        query = '''SELECT * FROM datasets d'''

        container_splits = self.database.get_container_client('splits')
        result_iterable = container_splits.query_items(query=query, enable_cross_partition_query=True)

        splits = {i['dataset']: {k: set(v) for k, v in i.items() if not k.startswith('_')} for i in
                    iter(result_iterable)}
        return splits


    def query_sequences_table(self, query, partition_key=None):
        startTime = datetime.now()

        if partition_key:
            result_iterable = self.container_sequences.query_items(query=query,
                                                                    partition_key=partition_key)
        else:
            result_iterable = self.container_sequences.query_items(query=query,
                                                                    enable_cross_partition_query=True)

        duration = datetime.now() - startTime
        results = [item for item in iter(result_iterable)]  # TODO could return the iterable instead

        # print('Query took {}. Number of entries in result: {}'.format(
        #     humanfriendly.format_timespan(duration), len(results)
        # ))

        return results


    @staticmethod
    def get_blob_service(datasets_table, dataset_name):
        """
        Return the azure.storage.blob BlockBlobService object corresponding to the dataset

        datasets_table is updated (no new copy) if a new BlockBlobService is created for a dataset
        """
        if dataset_name not in datasets_table:
            raise KeyError('Dataset {} is not in the datasets table.'.format(dataset_name))

        entry = datasets_table[dataset_name]

        if 'blob_service' in entry:
            return entry['blob_service']

        # need to create a new blob service for this dataset
        if 'container_sas_key' not in entry:
            raise KeyError('Dataset {} does not have the container_sas_key field in the datasets table.'.format(dataset_name))

        # the SAS token can be just for the container, not the storage account
        # - will be fine for accessing files in that container later
        blob_service = BlockBlobService(account_name=entry['storage_account'],
                                        sas_token=entry['container_sas_key'])
        datasets_table[dataset_name]['blob_service'] = blob_service  # in-place update
        return blob_service


    @staticmethod
    def get_full_path(datasets_table, dataset_name, img_path):
        entry = datasets_table[dataset_name]
        if 'path_prefix' not in entry or entry['path_prefix'] == '':
            return img_path
        else:
            return os.path.join(entry['path_prefix'], img_path)