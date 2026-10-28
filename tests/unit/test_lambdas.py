"""
Error handling of copy_log_files and delete_originals, the two Lambdas that catch AWS errors themselves.

Throttling leaves them as AWSTransientError, which the state machine retries. Before, a throttled copy
returned {'status': 'error'}, after which the state machine went on to Delete Originals and deleted files
that had not been copied; a throttled listing or delete left the originals in place.
"""

from unittest.mock import MagicMock, patch

import botocore.exceptions
import pytest

from aws_errors import AWSTransientError
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
    assert copy(s3) == {'status': 'done'}
    assert s3.copy_object.call_count == 2


def test_a_throttled_copy_is_retried_and_does_not_reach_delete_originals():
    s3 = MagicMock()
    s3.copy_object.side_effect = [None, SLOW_DOWN]
    with pytest.raises(AWSTransientError):
        copy(s3)


def test_a_copy_refused_for_another_reason_still_returns_an_error_status():
    """Unchanged: the state machine then continues to Delete Originals."""
    s3 = MagicMock()
    s3.copy_object.side_effect = client_error('AccessDenied', 403)
    assert copy(s3)['status'] == 'error'


def test_a_throttled_manifest_read_is_retried():
    s3 = MagicMock()
    s3.get_object.side_effect = SLOW_DOWN
    with patch.object(copy_log_files, 's3_client', s3):
        with pytest.raises(AWSTransientError):
            copy_log_files.lambda_handler({**COPY, 'files': 'manifest.json'}, context())


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
