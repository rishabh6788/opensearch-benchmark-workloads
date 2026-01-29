import logging
import aiohttp
import subprocess
import json
import asyncio
import sys

from osbenchmark.worker_coordinator.runner import Runner
from osbenchmark.client import RequestContextHolder


def register(registry):
    registry.register_runner(
        PageFaultMetricsRunner.RUNNER_NAME,
        PageFaultMetricsRunner(),
        async_runner=True
    )
    registry.register_runner(
        ClearPageCacheRunner.RUNNER_NAME,
        ClearPageCacheRunner(),
        async_runner=True
    )

request_context_holder = RequestContextHolder()


class PageFaultMetricsRunner(Runner):
    RUNNER_NAME = "page-fault-metrics"

    async def __call__(self, opensearch, params):
        request_context_holder.on_client_request_start()
        refresh_response = await opensearch.transport.perform_request("GET", "/_cluster/health")
        request_context_holder.on_client_request_end()

        # Using AWS CLI via subprocess
        try:
            # Send SSM command
            result = subprocess.run([
                'aws', 'ssm', 'send-command',
                '--instance-ids', 'i-0902df606b50f1f26',
                '--document-name', 'ClearSystemCache',
                '--region', 'us-east-1',
                '--output', 'json'
            ], capture_output=True, text=True, check=True)

            response = json.loads(result.stdout)
            command_id = response['Command']['CommandId']

            #logging.getLogger(__name__).info(f"SSM command sent: {command_id}")

            # Wait for command to complete
            max_attempts = 20
            delay = 5

            for attempt in range(max_attempts):
                await asyncio.sleep(delay)

                status_result = subprocess.run([
                    'aws', 'ssm', 'get-command-invocation',
                    '--command-id', command_id,
                    '--instance-id', 'i-0902df606b50f1f26',
                    '--region', 'us-east-1',
                    '--output', 'json'
                ], capture_output=True, text=True, check=True)

                status_response = json.loads(status_result.stdout)
                status = status_response['Status']

                if status in ['Success', 'Failed', 'Cancelled', 'TimedOut']:
                    #logging.getLogger(__name__).info(f"Command completed with status: {status}")
                    break

                if attempt == max_attempts - 1:
                    print("WTF!!")

        except subprocess.CalledProcessError as e:
            print(f"Error: {e.stderr}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"Error: {str(e)}", file=sys.stderr)
            return None

        return None

    def __repr__(self, *args, **kwargs):
        return self.RUNNER_NAME


class ClearPageCacheRunner(Runner):
    RUNNER_NAME = "clear-page-cache"

    async def __call__(self, opensearch, params):
        request_context_holder.on_client_request_start()
        refresh_response = await opensearch.transport.perform_request("GET", "/_cluster/health")
        request_context_holder.on_client_request_end()

        return None

    def __repr__(self, *args, **kwargs):
        return self.RUNNER_NAME
