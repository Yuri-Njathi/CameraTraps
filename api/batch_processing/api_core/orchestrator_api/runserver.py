# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.

from datetime import datetime
import json
import math
import multiprocessing
import os
from random import shuffle
import string
import time
from typing import Any, Dict, Mapping
import urllib.parse

from flask import Flask, request, make_response, jsonify, Response
from azure.storage.blob import BlockBlobService
# /ai4e_api_tools has been added to the PYTHONPATH
from ai4e_app_insights_wrapper import AI4EAppInsights
from ai4e_service import APIService

import api_config
import orchestrator
from sas_blob_utils import SasBlob  # file in this directory, not the ai4eutil repo one

import sys

print('Python version is {}'.format(sys.version))

print('Creating application')

api_prefix = os.getenv('API_PREFIX')
print('API prefix:', api_prefix)
assert api_prefix is not None

app = Flask(__name__)

# Use the AI4EAppInsights library to send log messages. NOT REQUIRED
log = AI4EAppInsights()

# Use the APIService to execute functions within a logging trace, supports
# long-running/async functions, handles SIGTERM signals from AKS, etc., and
# handles concurrent requests.
with app.app_context():
    ai4e_service = APIService(app, log)

# hacking the API Framework a bit, to use some functions directly instead of
# through its decorators in order for the return value of the async call to be
# backwards compatible
api_task_manager = ai4e_service.api_task_manager
ai4e_service.func_request_counts[api_prefix + '/request_detections'] = 0
ai4e_service.func_request_counts[api_prefix + '/request_detections_aml'] = 0

# Instantiate blob storage service to the internal container to put intermediate
# results and files
storage_account_name = os.getenv('STORAGE_ACCOUNT_NAME')
storage_account_key = os.getenv('STORAGE_ACCOUNT_KEY')
assert storage_account_name is not None
assert storage_account_key is not None
internal_storage_service = BlockBlobService(
    account_name=storage_account_name, account_key=storage_account_key)
internal_container = api_config.INTERNAL_CONTAINER
internal_datastore = {
    'account_name': storage_account_name,
    'account_key': storage_account_key,
    'container_name': internal_container
}
print('Internal storage container {} in account {} is set up.'.format(
    internal_container, storage_account_name))

# We use multiprocessing.Lock which works everywhere a threading.Lock works,
# i.e., even in single-process, multi-threaded code. However, this lock is only
# effective if the lock is actually created before worker processes are forked
# and therefore shared. Thus, we require --preload flag when running gunicorn.
# See https://stackoverflow.com/q/18213619.
task_status_lock = multiprocessing.Lock()


def update_task_status(task_manager: Any, request_id: str,
                       task_status: Mapping[str, Any]) -> None:
    """Thread-safe wrapper around api_task_manager.UpdateTaskStatus().

    Args:
        task_manager: task_management.api_task.TaskManager
            see ai4e_api_tools
        request_id: str
        task_status: dict
    """
    with task_status_lock:
        task_manager.UpdateTaskStatus(request_id, task_status)


def _abort(error_code: int, error_message: str) -> Response:
    return make_response(jsonify({'error': error_message}), error_code)


@app.route(api_prefix + '/request_detections', methods=['POST'])
def request_detections() -> Response:
    if not request.is_json:
        msg = 'Body needs to have a JSON mimetype (e.g., application/json).'
        return _abort(415, msg)

    try:
        post_body = request.get_json()
    except Exception as e:
        return _abort(415, 'Error occurred reading POST request body: {}.'.format(e))

    # required params
    caller_id = post_body.get('caller', None)
    if caller_id is None or caller_id not in api_config.ALLOWLIST:
        msg = ('Parameter caller is not supplied or is not on our allowlist. '
               'Please email cameratraps@microsoft.com to request access.')
        return _abort(401, msg)

    use_url = post_body.get('use_url', False)

    input_container_sas = post_body.get('input_container_sas', None)
    if not input_container_sas and not use_url:
        msg = ('input_container_sas with read and list access is a required '
               'field when not using image URLs.')
        return _abort(400, msg)

    if input_container_sas is not None:
        result = orchestrator.check_data_container_sas(input_container_sas)
        if result is not None:
            return _abort(result[0], result[1])

    # if use_url, then images_requested_json_sas is required
    if use_url:
        images_requested_json_sas = post_body.get(
            'images_requested_json_sas', None)
        if images_requested_json_sas is None:
            msg = 'images_requested_json_sas is required since use_url is true.'
            return _abort(400, msg)

    # check model_version and request_name params are valid
    model_version = post_body.get('model_version', '')
    if model_version != '':
        model_version = str(model_version)  # in case an int is specified
        if model_version not in api_config.SUPPORTED_MODEL_VERSIONS:
            return _abort(400, 'model_version {} is unsupported.'.format(model_version))

    # check request_name has only allowed characters
    request_name = post_body.get('request_name', '')
    if request_name != '':
        if len(request_name) > 92:
            return _abort(400, 'request_name is longer than 92 characters.')
        allowed = set(string.ascii_letters + string.digits + '_' + '-')
        if not set(request_name) <= allowed:
            msg = ('request_name contains unallowed characters (only letters, '
                   'digits, - and _ are allowed).')
            return _abort(400, msg)

    # TODO check that the expiry date of input_container_sas is at least a month
    # into the future

    # TODO check images_requested_json_sas is a blob not a container

    with task_status_lock:
        task_info = api_task_manager.AddTask(request)
    request_id = str(task_info['TaskId'])

    # HACK hacking the API Framework a bit because we want to return a JSON with
    # the request_id, otherwise a string "TaskId: the_task_id" will be returned

    ai4e_service._create_and_execute_thread(
        func=_request_detections, api_path='/request_detections',
        request_id=request_id, post_body=post_body)

    # wrap_async_endpoint executes the function in a new thread and wraps it
    # within a logging trace
    # ai4e_service.wrap_async_endpoint(
    #     _request_detections, trace_name='post:request_detections',
    #     request_id=request_id, post_body=post_body)

    return jsonify(request_id=request_id)


def _request_detections(**kwargs: Any) -> None:
    try:
        body = kwargs.get('post_body')
        assert body is not None

        input_container_sas = body.get('input_container_sas', None)

        use_url = body.get('use_url', False)

        images_requested_json_sas = body.get('images_requested_json_sas', None)

        image_path_prefix = body.get('image_path_prefix', None)

        first_n = body.get('first_n', None)
        first_n = int(first_n) if first_n else None

        sample_n = body.get('sample_n', None)
        sample_n = int(sample_n) if sample_n else None

        model_version = body.get('model_version', '')
        if model_version == '':
            model_version = api_config.AML_CONFIG['default_model_version']
        model_name = api_config.AML_CONFIG['models'][model_version]

        # request_name and request_submission_timestamp are for appending to
        # output file names
        request_name = body.get('request_name', '')
        request_submission_timestamp = orchestrator.get_utc_timestamp()

        request_id = kwargs['request_id']
        task_status = orchestrator.get_task_status(
            'running', 'Request received.')
        update_task_status(api_task_manager, request_id, task_status)
        print('runserver.py, request_id {}, '.format(request_id),
              'model_version {}, model_name {}, '.format(model_version, model_name),
              'request_name {}, '.format(request_name),
              'submission timestamp is {}'.format(request_submission_timestamp))

        # image_paths can be a list of strings (Azure blob names or public URLs)
        # or a list of length-2 lists where each is a [image_id, metadata] pair

        # Case 1: listing all images in the container
        # - not possible to have attached metadata if listing images in a blob
        if images_requested_json_sas is None:
            metadata_available = False
            task_status = orchestrator.get_task_status(
                'running', 'Listing all images to process.')
            update_task_status(api_task_manager, request_id, task_status)
            print('runserver.py, running - listing all images to process.')

            # list all images to process
            image_paths = SasBlob.list_blobs_in_container(
                api_config.MAX_NUMBER_IMAGES_ACCEPTED + 1,
                # so > MAX_NUMBER_IMAGES_ACCEPTED will find that there are too many images requested so should not proceed
                sas_uri=input_container_sas,
                blob_prefix=image_path_prefix, blob_suffix='.jpg')

        # Case 2: user supplied a list of images to process; can include metadata
        else:
            print('runserver.py, running - using provided list of images.')
            image_paths_text = SasBlob.download_blob_to_text(
                images_requested_json_sas)
            image_paths = json.loads(image_paths_text)
            print('runserver.py, length of image_paths provided by the user: {}'.format(len(image_paths)))
            if len(image_paths) == 0:
                task_status = orchestrator.get_task_status(
                    'completed', '0 images found in provided list of images.')
                update_task_status(api_task_manager, request_id, task_status)
                return

            error, metadata_available = orchestrator.validate_provided_image_paths(image_paths)
            if error is not None:
                msg = 'image paths provided in the json are not valid: {}'.format(error)
                raise ValueError(msg)

            valid_image_paths = []
            for p in image_paths:
                locator = p[0] if metadata_available else p
                # urlparse(p).path also preserves the extension on local paths
                path = urllib.parse.urlparse(locator).path.lower()
                if path.endswith(api_config.ACCEPTED_IMAGE_FILE_ENDINGS):
                    valid_image_paths.append(p)
            image_paths = valid_image_paths
            print('runserver.py, length of image_paths provided by user, '
                  'after filtering to jpg: {}'.format(len(image_paths)))

            valid_image_paths = []
            if image_path_prefix is not None:
                for p in image_paths:
                    locator = p[0] if metadata_available else p
                    if locator.startswith(image_path_prefix):
                        valid_image_paths.append(p)
                image_paths = valid_image_paths
                print('runserver.py, length of image_paths provided by user, '
                      'after filtering for image_path_prefix: {}'.format(len(image_paths)))

            if not use_url:
                res = orchestrator.spot_check_blob_paths_exist(
                    image_paths, input_container_sas, metadata_available)
                if res is not None:
                    msg = ('path {} provided in list of images to process '.format(res),
                           'does not exist in the container pointed to by '
                           'data_container_sas.')
                    raise LookupError(msg)

        # apply the first_n and sample_n filters
        if first_n is not None:
            assert first_n > 0, 'parameter first_n is 0.'
            # OK if first_n > total number of images
            image_paths = image_paths[:first_n]

        if sample_n is not None:
            assert sample_n > 0, 'parameter sample_n is 0.'
            if sample_n > len(image_paths):
                msg = ('parameter sample_n specifies more images than '
                       'available (after filtering by other provided params).')
                raise ValueError(msg)

            # sample by shuffling image paths and take the first sample_n images
            print('First path before shuffling:', image_paths[0])
            shuffle(image_paths)
            print('First path after shuffling:', image_paths[0])
            image_paths = orchestrator.sort_image_paths(
                image_paths[:sample_n], metadata_available)

        num_images = len(image_paths)
        print('runserver.py, num_images after applying all filters: {}'.format(num_images))
        if num_images < 1:
            task_status = orchestrator.get_task_status(
                'completed',
                'Zero images found in container or in provided list of images '
                'after filtering with the provided parameters.')
            update_task_status(api_task_manager, request_id, task_status)
            return
        if num_images > api_config.MAX_NUMBER_IMAGES_ACCEPTED:
            task_status = orchestrator.get_task_status(
                'failed',
                'The number of images ({}) requested for processing exceeds the maximum accepted {} in one call'.format(
                    num_images, api_config.MAX_NUMBER_IMAGES_ACCEPTED))
            update_task_status(api_task_manager, request_id, task_status)
            return

        # finalized image_paths is uploaded to internal_container; all sharding
        # and scoring use the uploaded list
        image_paths_string = json.dumps(image_paths, indent=1)
        internal_storage_service.create_blob_from_text(
            internal_container, '{}/{}_images.json'.format(request_id, request_id),
            image_paths_string)
        # the list of images json does not have request_name or timestamp in the
        # file name so that score.py can locate it

        task_status = orchestrator.get_task_status(
            'running', 'Images listed; processing {} images.'.format(num_images))
        update_task_status(api_task_manager, request_id, task_status)
        print('runserver.py, running - images listed; processing {} images'.format(num_images))

        # set up connection to AML Compute and data stores
        # do this for each request since pipeline step is associated with the
        # data stores
        aml_compute = orchestrator.AMLCompute(
            request_id=request_id, use_url=use_url,
            input_container_sas=input_container_sas,
            internal_datastore=internal_datastore, model_name=model_name)
        print('AMLCompute resource connected successfully.')

        num_images_per_job = api_config.NUM_IMAGES_PER_JOB
        num_jobs = math.ceil(num_images / num_images_per_job)

        # list_jobs: Dict[str, Dict[str, int]] = {}
        list_jobs = {}
        for job_index in range(num_jobs):
            begin = job_index * num_images_per_job
            end = begin + num_images_per_job

            # Experiment name must be between 1 and 36 characters long. Its
            # first character has to be alphanumeric, and the rest may contain
            # hyphens and underscores.
            shortened_request_id = request_id.split('-')[0]
            if len(shortened_request_id) > 8:
                shortened_request_id = shortened_request_id[:8]

            # request ID, job index, total
            job_id = 'r{}_i{}_t{}'.format(shortened_request_id, job_index, num_jobs)

            list_jobs[job_id] = {'begin': begin, 'end': end}

        list_jobs_submitted = aml_compute.submit_jobs(
            list_jobs, api_task_manager, num_images)
        task_status = orchestrator.get_task_status(
            'running',
            'All {} images submitted to cluster for processing.'.format(num_images))
        update_task_status(api_task_manager, request_id, task_status)

    except Exception as e:
        task_status = orchestrator.get_task_status(
            'failed', 'An error occurred while processing the request: {}'.format(e))
        update_task_status(api_task_manager, request_id, task_status)
        print('runserver.py, exception in _request_detections: {}'.format(e))
        return  # do not initiate _monitor_detections_request

    try:
        aml_monitor = orchestrator.AMLMonitor(
            request_id=request_id,
            shortened_request_id=shortened_request_id,
            list_jobs_submitted=list_jobs_submitted,
            request_name=request_name,
            request_submission_timestamp=request_submission_timestamp,
            model_version=model_version)

        # start another thread to monitor the jobs and consolidate the results
        # when they finish
        # HACK
        ai4e_service._create_and_execute_thread(
            func=_monitor_detections_request,
            api_path='/request_detections_aml',
            request_id=request_id, aml_monitor=aml_monitor)

        # ai4e_service.wrap_async_endpoint(
        #     _monitor_detections_request,
        #     trace_name='post:_monitor_detections_request',
        #     request_id=request_id, aml_monitor=aml_monitor)
    except Exception as e:
        task_status = orchestrator.get_task_status(
            'problem',
            ('An error occurred when starting the status monitoring process. '
             'The images should be submitted for processing though - please '
             'contact us to retrieve your results. Error: {}'.format(e)))
        update_task_status(api_task_manager, request_id, task_status)
        print('runserver.py, exception when starting orchestrator.AMLMonitor: {}'.format(e))


def _monitor_detections_request(**kwargs: Any) -> None:
    try:
        request_id = kwargs['request_id']
        aml_monitor = kwargs['aml_monitor']

        max_num_checks = api_config.MAX_MONITOR_CYCLES
        num_checks = 0

        # errors encountered during aml_monitor.check_job_status()
        num_errors_job_status = 0

        # errors encountered during aml_monitor.aggregate_results()
        num_errors_aggregation = 0

        print('Monitoring thread with _monitor_detections_request started.')

        while True:
            # time.sleep() blocks the current thread only
            time.sleep(api_config.MONITOR_PERIOD_MINUTES * 60)

            print('runserver.py, _monitor_detections_request, woke up at '
                  '{} for check number {}.'.format(datetime.now(), num_checks))

            # check the status of the jobs, with retries
            try:
                all_jobs_finished, status_tally = aml_monitor.check_job_status()
            except Exception as e:
                num_errors_job_status += 1
                print('runserver.py, _monitor_detections_request, exception in '
                      'aml_monitor.check_job_status(): {}'.format(e))

                if num_errors_job_status <= api_config.NUM_RETRIES:
                    print('Will retry in the next monitoring cycle. Number of '
                          'errors so far: {}'.format(num_errors_job_status))
                    continue
                else:
                    print('Number of retries reached for '
                          'aml_monitor.check_job_status().')
                    raise e

            print('all jobs finished? {}'.format(all_jobs_finished))
            for status, count in status_tally.items():
                print('status {}, number of jobs = {}'.format(status, count))

            num_failed = status_tally['Failed']
            # need to periodically check the enumerations are what AML returns
            # - not the same as in their doc
            num_finished = status_tally['Finished'] + num_failed

            # if all jobs finished, aggregate the results and return the URLs
            # to the output files
            if all_jobs_finished:
                task_status = orchestrator.get_task_status(
                    'running',
                    'Model inference finished; now aggregating results.')
                update_task_status(api_task_manager, request_id, task_status)

                # retrieve and join the output CSVs from each job, with retries
                try:
                    output_file_urls = aml_monitor.aggregate_results()
                except Exception as e:
                    num_errors_aggregation += 1
                    print('runserver.py, _monitor_detections_request, '
                          'exception in aml_monitor.aggregate_results(): {}'.format(e))

                    if num_errors_aggregation <= api_config.NUM_RETRIES:
                        print('Will retry during the next monitoring wake-up '
                              'cycle. Number of errors so far: {}'.format(num_errors_aggregation))
                        task_status = orchestrator.get_task_status(
                            'running',
                            'All shards finished but results aggregation '
                            'failed. Will retry in '
                            '{} minutes.'.format(api_config.MONITOR_PERIOD_MINUTES))
                        update_task_status(api_task_manager, request_id,
                                           task_status)
                        continue

                    print('Number of retries reached for '
                          'aml_monitor.aggregate_results().')
                    raise e

                # output_file_urls_str = json.dumps(output_file_urls)
                message = {
                    'num_failed_shards': num_failed,
                    'output_file_urls': output_file_urls
                }
                task_status = orchestrator.get_task_status('completed', message)
                update_task_status(api_task_manager, request_id, task_status)
                break

            # not all jobs are finished, update the status with number of shards
            # finished
            task_status = orchestrator.get_task_status(
                'running',
                'Last status check at {}, '
                '{} out of {} shards '
                'finished processing, {} failed.'.format(
                    orchestrator.get_utc_time(), num_finished, aml_monitor.get_total_jobs(), num_failed
                ))
            update_task_status(api_task_manager, request_id, task_status)

            # not all jobs are finished but the maximum number of checking cycle
            # is reached, stop this thread
            num_checks += 1
            if num_checks >= max_num_checks:
                task_status = orchestrator.get_task_status(
                    'problem',
                    'Request unfinished after {} '
                    'x {} minutes; '
                    'abandoning the monitoring thread. Please contact us to '
                    'retrieve any results.'.format(
                        api_config.MAX_MONITOR_CYCLES, api_config.MONITOR_PERIOD_MINUTES
                    ))
                update_task_status(api_task_manager, request_id, task_status)
                print('runserver.py, _monitor_detections_request, ending!')
                break

    except Exception as e:
        task_status = orchestrator.get_task_status(
            'problem',
            'An error occurred while monitoring the status of this request. '
            'The images should be processing though - please contact us to '
            'retrieve your results. Error: {}'.format(e))
        update_task_status(api_task_manager, request_id, task_status)
        print('runserver.py, exception in _monitor_detections_request(): ', e)
    # maybe not needed?
    # finally:
    #     url = api_prefix + '/request_detections_aml'
    #     ai4e_service.func_request_counts[url] -= 1


# for the following sync end points, we use the ai4e_service decorator instead
# of the flask app decorator, so that wrap_sync_endpoint() is done for us

@ai4e_service.api_sync_func(
    api_path='/default_model_version',
    methods=['GET'],
    maximum_concurrent_requests=100,
    trace_name='get:default_model_version')
def default_model_version(*args: Any, **kwargs: Any) -> str:
    return api_config.AML_CONFIG['default_model_version']


@ai4e_service.api_sync_func(
    api_path='/supported_model_versions',
    methods=['GET'],
    maximum_concurrent_requests=100,
    trace_name='get:supported_model_versions')
def supported_model_versions(*args: Any, **kwargs: Any) -> Response:
    return jsonify(api_config.SUPPORTED_MODEL_VERSIONS)


if __name__ == '__main__':
    app.run()
