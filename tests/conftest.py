import os
import sys
from unittest.mock import MagicMock, patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
# The Lambdas import aws_errors from their layer, as they do in AWS
sys.path.insert(0, os.path.join(ROOT, 'functions', 'layers', 'aws_errors', 'python'))

# Module-level boto3 clients are created at import; no call is made, but a region is needed
os.environ.setdefault('AWS_DEFAULT_REGION', 'eu-north-1')
for name, value in {'TMP_LOGS_BUCKET_NAME': 'example-tmp-logs', 'DEST_LOGS_BUCKET_NAME': 'example-dest-logs',
                    'BUCKET_NAME': 'example-bucket', 'BUCKET_PREFIXES': '[]', 'CONTROL_TOWER_BUCKET': 'example-ct',
                    'COMMON_DESTINATION_BUCKET': 'example-common', 'FINAL_AGGREGATION_PREFIX': 'aggregated',
                    'MIN_SIZE': '1000', 'ORG_ID': 'o-example', 'REGION': 'eu-north-1',
                    'COMBINE_LOG_FILES_SM_ARN': 'arn:aws:states:eu-north-1:111111111111:stateMachine:example',
                    'DASHBOARD_NAME': 'example', 'FIRST_VARIABLE': 'x', 'REST_VARIABLE': 'x',
                    'BODY_VARIABLE': 'x'}.items():
    os.environ.setdefault(name, value)


@pytest.fixture(autouse=True)
def mock_aws_services():
    """Prevents ALL AWS API calls: every boto3 client and resource the Lambdas create is a MagicMock."""
    with patch('boto3.client', side_effect=lambda *args, **kwargs: MagicMock(name=f'client:{args[0]}')), \
            patch('boto3.resource', side_effect=lambda *args, **kwargs: MagicMock(name=f'resource:{args[0]}')):
        yield
