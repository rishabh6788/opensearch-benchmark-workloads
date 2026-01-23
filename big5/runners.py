import logging
import aiohttp
import boto3
import time
import sys

from osbenchmark.worker_coordinator.runner import Runner
from osbenchmark.client import RequestContextHolder

from osbenchmark.utils.parse import parse_int_parameter, parse_string_parameter


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
        #host = opensearch.transport.hosts[0].get('host')
        #port = params.get("port", 8000)
        #endpoint = params.get("endpoint", "/_send_pagefault_metrics")
        #body = params.get("body", None)

        # First, call refresh API (establishes request context)
        request_context_holder.on_client_request_start()
        refresh_response = await opensearch.transport.perform_request("GET", "/_cluster/health")
        request_context_holder.on_client_request_end()


        # Then make HTTP call to port 8000
        #url = f"http://{host}:{port}{endpoint}"
        #async with aiohttp.ClientSession() as session:
        #    async with session.request("POST", url, json=body) as response:
        #        status = response.status
        #        logging.getLogger(__name__).info(f"API call to {url} completed with status {status}")

        return None

    def __repr__(self, *args, **kwargs):
        return self.RUNNER_NAME

class ClearPageCacheRunner(Runner):

    RUNNER_NAME = "clear-page-cache"

    async def __call__(self, opensearch, params):
        #host = opensearch.transport.hosts[0].get('host')
        #port = params.get("port", 8000)
        #endpoint = params.get("endpoint", "/_flush_page_cache")
        #body = params.get("body", None)

        # First, call refresh API (establishes request context)
        request_context_holder.on_client_request_start()
        refresh_response = await opensearch.transport.perform_request("GET", "/_cluster/health")
        request_context_holder.on_client_request_end()

        # Then make HTTP call to port 8000
        #url = f"http://{host}:{port}{endpoint}"
        #async with aiohttp.ClientSession() as session:
        #    async with session.request("POST", url, json=body) as response:
        #        status = response.status
        #        logging.getLogger(__name__).info(f"API call to {url} completed with status {status}")

        return None

    def __repr__(self, *args, **kwargs):
        return self.RUNNER_NAME
