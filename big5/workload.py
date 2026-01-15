import random
from .runners import register as register_runners


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


def register(registry):
    register_runners(registry)
    registry.register_param_source("random-process-name-source", RandomProcessNameParamSource)
