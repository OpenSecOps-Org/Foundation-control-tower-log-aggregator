"""
Error handling of copy_log_files, combine_log_files and delete_originals. No log file may be lost.

Throttling leaves them as AWSTransientError, which the state machine retries. A file that cannot be copied
or combined for another reason is kept: the others are still processed, the file is listed in the result
as 'kept', and Delete Originals leaves it in the source bucket. Before, a failed copy returned
{'status': 'error'}, after which Delete Originals deleted files that had not been copied.
"""

import itertools
from unittest.mock import MagicMock, patch

import botocore.exceptions
import pytest

from aws_errors import AWSTransientError
from functions.combine_log_files import app as combine_log_files
from functions.copy_log_files import app as copy_log_files
from functions.delete_originals import app as delete_originals


def client_error(code, status=400):
    return botocore.exceptions.ClientError(
        {'Error': {'Code': code, 'Message': code}, 'ResponseMetadata': {'HTTPStatusCode': status}}, 'op')


SLOW_DOWN = client_error('SlowDown', 503)


def context():
    c = MagicMock()
    c.get_remaining_time_in_millis.return_value = 900_000
    return c


# ---------------------------------------------------------------------------
# copy_log_files
# ---------------------------------------------------------------------------

COPY = {'date': '2026-10-27', 'bucket_name': 'example-source', 'files': ['a.log', 'b.log']}


def copy(s3):
    with patch.object(copy_log_files, 's3_client', s3):
        return copy_log_files.lambda_handler(dict(COPY), context())


def test_copies_every_file():
    s3 = MagicMock()
    assert copy(s3) == {'status': 'done', 'kept': []}
    assert s3.copy_object.call_count == 2


def test_a_throttled_copy_is_retried_and_does_not_reach_delete_originals():
    s3 = MagicMock()
    s3.copy_object.side_effect = [None, SLOW_DOWN]
    with pytest.raises(AWSTransientError):
        copy(s3)


@pytest.mark.parametrize('error', [client_error('AccessDenied', 403), client_error('NoSuchKey', 404)])
def test_a_file_that_cannot_be_copied_is_kept_and_the_others_still_copied(error):
    s3 = MagicMock()
    s3.copy_object.side_effect = [error, None]
    assert copy(s3) == {'status': 'done', 'kept': ['a.log']}
    assert s3.copy_object.call_count == 2


def test_kept_files_are_carried_across_continuations():
    s3 = MagicMock()
    s3.copy_object.side_effect = client_error('AccessDenied', 403)
    data = {**COPY, 'combineMainLogsResult': {'continuationMarker': 1, 'kept': ['earlier.log']}}
    with patch.object(copy_log_files, 's3_client', s3):
        assert copy_log_files.lambda_handler(data, context()) == {'status': 'done', 'kept': ['earlier.log', 'b.log']}


def test_a_continuation_returns_the_files_kept_so_far():
    s3 = MagicMock()
    s3.copy_object.side_effect = client_error('AccessDenied', 403)
    short = MagicMock()
    short.get_remaining_time_in_millis.return_value = 900_000
    clock = itertools.chain([0, 0], itertools.repeat(800))  # the second file finds under 2 minutes left
    with patch.object(copy_log_files, 's3_client', s3), \
            patch.object(copy_log_files.time, 'time', side_effect=lambda: next(clock)):
        assert copy_log_files.lambda_handler(dict(COPY), short) == {'continuationMarker': 1, 'kept': ['a.log']}


def test_a_throttled_manifest_read_is_retried():
    s3 = MagicMock()
    s3.get_object.side_effect = SLOW_DOWN
    with patch.object(copy_log_files, 's3_client', s3):
        with pytest.raises(AWSTransientError):
            copy_log_files.lambda_handler({**COPY, 'files': 'manifest.json'}, context())


# ---------------------------------------------------------------------------
# combine_log_files
# ---------------------------------------------------------------------------

COMBINE = {'bucket_name': 'example-source', 'key': 'combined/2026-10-27.log.gz', 'files': ['a.log', 'b.log']}


def combining_s3(unreadable=()):
    """An S3 client whose part copy fails for the source files in `unreadable`."""
    s3 = MagicMock()
    s3.create_multipart_upload.return_value = {'UploadId': 'upload-1'}

    def upload_part_copy(**kwargs):
        source = kwargs['CopySource']
        if source['Bucket'] == 'example-source' and source['Key'] in unreadable:
            raise unreadable[source['Key']]
        return {'CopyPartResult': {'ETag': 'etag'}}
    s3.upload_part_copy.side_effect = upload_part_copy
    return s3


def combine(s3, data=COMBINE):
    resource = MagicMock()
    resource.Object.return_value.content_length = combine_log_files.FIVE_MB + 100
    with patch.object(combine_log_files, 's3_client', s3), patch.object(combine_log_files, 's3_resource', resource):
        return combine_log_files.lambda_handler(dict(data), context())


def test_combines_every_file():
    s3 = combining_s3()
    assert combine(s3) == {'status': 'done', 'kept': []}
    s3.abort_multipart_upload.assert_not_called()


@pytest.mark.parametrize('error', [client_error('AccessDenied', 403), client_error('NoSuchKey', 404)])
def test_a_file_that_cannot_be_combined_is_kept_and_the_others_still_combined(error):
    s3 = combining_s3({'a.log': error})
    assert combine(s3) == {'status': 'done', 'kept': ['a.log']}
    s3.abort_multipart_upload.assert_called_once_with(
        Bucket=combine_log_files.TMP_LOGS_BUCKET_NAME, Key=COMBINE['key'], UploadId='upload-1')
    sources = [c.kwargs['CopySource']['Key'] for c in s3.upload_part_copy.call_args_list]
    assert 'b.log' in sources


def test_when_every_file_is_kept_nothing_is_moved_and_the_temp_file_is_removed():
    s3 = combining_s3({'a.log': client_error('AccessDenied', 403), 'b.log': client_error('AccessDenied', 403)})
    resource = MagicMock()
    resource.Object.return_value.content_length = combine_log_files.FIVE_MB
    with patch.object(combine_log_files, 's3_client', s3), patch.object(combine_log_files, 's3_resource', resource):
        assert combine_log_files.lambda_handler(dict(COMBINE), context()) == {'status': 'done',
                                                                            'kept': ['a.log', 'b.log']}
    s3.delete_object.assert_called_once_with(Bucket=combine_log_files.TMP_LOGS_BUCKET_NAME, Key=COMBINE['key'])
    assert all(c.kwargs['Bucket'] == combine_log_files.TMP_LOGS_BUCKET_NAME
               for c in s3.create_multipart_upload.call_args_list)


def test_a_throttled_combine_is_retried():
    s3 = combining_s3({'a.log': SLOW_DOWN})
    with pytest.raises(AWSTransientError):
        combine(s3)


def test_a_failure_with_the_combined_file_itself_still_fails_and_deletes_nothing():
    s3 = combining_s3()
    s3.complete_multipart_upload.side_effect = client_error('InvalidPart')
    with pytest.raises(botocore.exceptions.ClientError):
        combine(s3)


# ---------------------------------------------------------------------------
# delete_originals
# ---------------------------------------------------------------------------

DELETE = {'bucket_name': 'example-source', 'files': ['a.log']}


def versions_s3():
    s3 = MagicMock()
    s3.get_paginator.return_value.paginate.return_value = [{'Versions': [{'VersionId': 'v1'}]}]
    s3.delete_objects.return_value = {'Deleted': [{'Key': 'a.log'}], 'Errors': []}
    return s3


def delete(s3, data=DELETE):
    with patch.object(delete_originals, 's3_client', s3):
        return delete_originals.lambda_handler(dict(data), None)


def test_deletes_every_version():
    s3 = versions_s3()
    delete(s3)
    assert s3.delete_objects.call_args.kwargs['Delete']['Objects'] == [{'Key': 'a.log', 'VersionId': 'v1'}]


def test_kept_files_are_left_in_the_source_bucket(capsys):
    s3 = versions_s3()
    delete(s3, {'bucket_name': 'example-source', 'files': ['a.log', 'b.log'],
                'combineMainLogsResult': {'status': 'done', 'kept': ['a.log']}})
    listed = [c.kwargs['Prefix'] for c in s3.get_paginator.return_value.paginate.call_args_list]
    assert listed == ['b.log']
    assert 'a.log' in capsys.readouterr().out


def test_nothing_is_deleted_when_every_file_was_kept():
    s3 = versions_s3()
    delete(s3, {'bucket_name': 'example-source', 'files': ['a.log'],
                'combineMainLogsResult': {'status': 'done', 'kept': ['a.log']}})
    s3.delete_objects.assert_not_called()


@pytest.mark.parametrize('where', ['listing', 'delete'])
def test_a_throttled_listing_or_delete_is_retried(where):
    s3 = versions_s3()
    if where == 'listing':
        s3.get_paginator.return_value.paginate.side_effect = SLOW_DOWN
    else:
        s3.delete_objects.side_effect = SLOW_DOWN
    with pytest.raises(AWSTransientError):
        delete(s3)


def test_a_throttled_manifest_read_is_retried_not_taken_as_no_files():
    s3 = versions_s3()
    s3.get_object.side_effect = SLOW_DOWN
    with pytest.raises(AWSTransientError):
        delete(s3, {**DELETE, 'files': 'manifest.json'})
    s3.delete_objects.assert_not_called()


def test_a_listing_refused_for_another_reason_still_deletes_nothing():
    s3 = versions_s3()
    s3.get_paginator.return_value.paginate.side_effect = client_error('AccessDenied', 403)
    delete(s3)
    s3.delete_objects.assert_not_called()


def test_a_delete_refused_for_another_reason_is_still_logged(capsys):
    s3 = versions_s3()
    s3.delete_objects.side_effect = client_error('AccessDenied', 403)
    delete(s3)
    assert 'AccessDenied' in capsys.readouterr().out
