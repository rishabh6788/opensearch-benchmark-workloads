import random
from .runners import register as register_runners
from osbenchmark.worker_coordinator.runner import BulkIndex, CreateIndex, DeleteIndex, Runner, request_context_holder
from osbenchmark.workload.params import (
    BulkIndexParamSource,
    CreateIndexParamSource,
    DeleteIndexParamSource,
)

#from osbenchmark.client import RequestContextHolder

#request_context_holder = RequestContextHolder()

class RandomProcessNameParamSource:
    def __init__(self, workload, params, **kwargs):
        self._params = params
        self.infinite = True
        self.process_names = ["udev", "systemd", "sshd", "kernel", "journal", "httpd", "cron"]
        random.seed(42)

    def partition(self, partition_index, total_partitions):
        return self

    def params(self):
        return {
            "process_name": random.choice(self.process_names)
        }

class ClusterAwareBulk(BulkIndex):
    multi_cluster = True

    async def __call__(self, opensearch, params):
        cluster = params.get("cluster", "default")
        return await super().__call__(opensearch[cluster], params)

    def __repr__(self, *args, **kwargs):
        return "cluster-aware-bulk"

class ClusterAwareCreateIndex(CreateIndex):
    multi_cluster = True

    async def __call__(self, opensearch, params):
        cluster = params.get("cluster", "default")
        return await super().__call__(opensearch[cluster], params)

    def __repr__(self, *args, **kwargs):
        return "cluster-aware-create-index"

class ClusterAwareDeleteIndex(DeleteIndex):
    multi_cluster = True

    async def __call__(self, opensearch, params):
        cluster = params.get("cluster", "default")
        return await super().__call__(opensearch[cluster], params)

    def __repr__(self, *args, **kwargs):
        return "cluster-aware-delete-index"

class ClusterAwareSwitchoverSeal(Runner):
    multi_cluster = True

    async def __call__(self, opensearch, params):
        cluster = params.get("cluster", "default")
        relationship = params.get("relationship","my-relationship")
        epoch = params.get("epoch", 1)

        client = opensearch[cluster]
        path = "/_remote_replication/cluster/{}/switchover/_seal".format(relationship)
        request_context_holder.on_client_request_start()

        await client.transport.perform_request(
            "POST",
            path,
            body={"epoch": epoch},
            headers={"Content-Type": "application/json"}
            )

        request_context_holder.on_client_request_end()

        return 1, "ops"

    def __repr__(self, *args, **kwargs):
        return "cluster-aware-switchover-seal"


def register(registry):
    register_runners(registry)
    registry.register_param_source("random-process-name-source", RandomProcessNameParamSource)

    registry.register_runner("cluster-aware-bulk", ClusterAwareBulk(), async_runner=True)
    registry.register_runner("cluster-aware-create-index", ClusterAwareCreateIndex(), async_runner=True)
    registry.register_runner("cluster-aware-delete-index", ClusterAwareDeleteIndex(), async_runner=True)
    registry.register_runner("cluster-aware-switchover-seal", ClusterAwareSwitchoverSeal(), async_runner=True)

    registry.register_param_source("cluster-aware-bulk-source", BulkIndexParamSource)
    registry.register_param_source("cluster-aware-create-index-source", CreateIndexParamSource)
    registry.register_param_source("cluster-aware-delete-index-source", DeleteIndexParamSource)