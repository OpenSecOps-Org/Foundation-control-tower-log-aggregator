"""
dashboard-builder must report a stable PhysicalResourceId.

Without one, cfnresponse falls back to the Lambda's log stream name, which changes between invocations.
CloudFormation then treats every update as a replacement and sends a Delete for the old ID during
cleanup, and this function's Delete removes the dashboard it has just rebuilt. Update and Delete must
echo the ID CloudFormation holds; Create must return a fixed name.
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

APP = Path(__file__).parent.parent.parent / 'functions' / 'dashboard-builder' / 'app.py'
FIXED_ID = 'dashboard-builder'

ENV = {
    'CONTROL_TOWER_BUCKET': 'aws-controltower-logs-111111111111-eu-north-1',
    'BUCKET_PREFIXES': 'example-logs',
    'DASHBOARD_NAME': 'Example-Log-Aggregation',
    'REGION': 'eu-north-1',
    'COMMON_DESTINATION_BUCKET': 'example-common-destination',
    'COMBINE_LOG_FILES_SM_ARN': 'arn:aws:states:eu-north-1:111111111111:stateMachine:Example',
    'BODY_VARIABLE': '<FIRST><REST>',
    'FIRST_VARIABLE': '"<BUCKET>"',
    'REST_VARIABLE': ',"<BUCKET>"',
}


def load_app(monkeypatch):
    """Import app.py by path with cfnresponse stubbed and AWS clients mocked."""
    cfnresponse = SimpleNamespace(SUCCESS='SUCCESS', FAILED='FAILED', send=MagicMock())
    monkeypatch.setitem(sys.modules, 'cfnresponse', cfnresponse)
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    client = MagicMock()
    client.list_buckets.return_value = {'Buckets': [{'Name': 'example-logs-1'}]}
    with patch('boto3.client', return_value=client):
        spec = importlib.util.spec_from_file_location('dashboard_builder_app', APP)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module, cfnresponse.send, client


def event(request_type, physical_id=None):
    e = {'RequestType': request_type, 'ResponseURL': 'https://example.invalid/response',
         'StackId': 'arn:aws:cloudformation:eu-north-1:111111111111:stack/test/1',
         'RequestId': 'req-1', 'LogicalResourceId': 'DashboardBuilderCustomResource', 'ResourceProperties': {}}
    if physical_id:
        e['PhysicalResourceId'] = physical_id
    return e


def context(stream):
    return SimpleNamespace(log_stream_name=stream)


def physical_id_sent(send):
    _, kwargs = send.call_args
    return kwargs.get('physicalResourceId')


def test_create_returns_fixed_id(monkeypatch):
    app, send, _ = load_app(monkeypatch)
    app.lambda_handler(event('Create'), context('2026/10/04/[$LATEST]stream-a'))
    assert physical_id_sent(send) == FIXED_ID


@pytest.mark.parametrize('request_type', ['Update', 'Delete'])
def test_update_and_delete_echo_existing_id(request_type, monkeypatch):
    app, send, _ = load_app(monkeypatch)
    app.lambda_handler(event(request_type, 'id-held-by-cloudformation'), context('2026/10/04/[$LATEST]stream-b'))
    assert physical_id_sent(send) == 'id-held-by-cloudformation'


def test_failure_reports_the_same_id(monkeypatch):
    app, send, client = load_app(monkeypatch)
    client.put_dashboard.side_effect = RuntimeError('boom')
    client.list_buckets.side_effect = RuntimeError('boom')
    app.lambda_handler(event('Update', 'id-held-by-cloudformation'), context('2026/10/04/[$LATEST]stream-c'))
    assert send.call_args.args[2] == 'FAILED'
    assert physical_id_sent(send) == 'id-held-by-cloudformation'
