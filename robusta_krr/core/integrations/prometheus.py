import asyncio
import datetime

from kubernetes import config as k8s_config
from kubernetes.client import ApiClient
from prometheus_api_client import PrometheusConnect
from requests.exceptions import ConnectionError, HTTPError

from robusta_krr.core.models.config import Config
from robusta_krr.core.models.objects import K8sObjectData
from robusta_krr.core.models.result import ResourceType
from robusta_krr.utils.configurable import Configurable
from robusta_krr.utils.service_discovery import ServiceDiscovery


class PrometheusDiscovery(ServiceDiscovery):
    def find_prometheus_url(self, *, api_client: ApiClient | None = None) -> str | None:
        return super().find_url(
            selectors=[
                "app=kube-prometheus-stack-prometheus",
                "app=prometheus,component=server",
                "app=prometheus-server",
                "app=prometheus-operator-prometheus",
                "app=prometheus-msteams",
                "app=rancher-monitoring-prometheus",
                "app=prometheus-prometheus",
            ],
            api_client=api_client,
        )


class PrometheusNotFound(Exception):
    pass


class PrometheusLoader(Configurable):
    def __init__(
        self,
        config: Config,
        *,
        cluster: str | None = None,
    ) -> None:
        super().__init__(config=config)

        self.debug(f"Initializing PrometheusLoader for {cluster or 'default'} cluster")

        self.auth_header = self.config.prometheus_auth_header
        self.ssl_enabled = self.config.prometheus_ssl_enabled

        self.api_client = k8s_config.new_client_from_config(context=cluster)
        self.prometheus_discovery = PrometheusDiscovery(config=self.config)

        self.url = self.config.prometheus_url
        self.url = self.url or self.prometheus_discovery.find_prometheus_url(api_client=self.api_client)

        if not self.url:
            raise PrometheusNotFound(
                f"Prometheus url could not be found while scanning in {cluster or 'default'} cluster"
            )

        headers = {}

        if self.auth_header:
            headers = {"Authorization": self.auth_header}
        elif not self.config.inside_cluster:
            self.api_client.update_params_for_auth(headers, {}, ["BearerToken"])

        self.prometheus = PrometheusConnect(url=self.url, disable_ssl=not self.ssl_enabled, headers=headers)
        self._check_prometheus_connection()

        self.debug(f"PrometheusLoader initialized for {cluster or 'default'} cluster")

    def _check_prometheus_connection(self):
        try:
            response = self.prometheus._session.get(
                f"{self.prometheus.url}/api/v1/query",
                verify=self.prometheus.ssl_verification,
                headers=self.prometheus.headers,
                # This query should return empty results, but is correct
                params={"query": "example"},
            )
            response.raise_for_status()
        except (ConnectionError, HTTPError) as e:
            raise PrometheusNotFound(
                f"Couldn't connect to Prometheus found under {self.prometheus.url}\nCaused by {e.__class__.__name__}: {e})"
            ) from e

    async def gather_data(
        self,
        object: K8sObjectData,
        resource: ResourceType,
        period: datetime.timedelta,
        *,
        timeframe: datetime.timedelta = datetime.timedelta(minutes=30),
    ) -> list[int]:
        # TODO: Queries do not work properly
        self.debug(f"Gathering data for {object} and {resource}")

        if resource == ResourceType.CPU:
            return await asyncio.to_thread(
                self.prometheus.custom_query_range,
                query='sum(node_namespace_pod_container:container_cpu_usage_seconds_total:sum_irate{namespace="$namespace", pod=~"$pod", container=~"$container"})',
                start_time=datetime.datetime.now() - period,
                end_time=datetime.datetime.now(),
                step="30m",
                params={
                    "namespace": object.namespace,
                    "pod": object.name,
                    "container": object.container,
                },
            )
        elif resource == ResourceType.Memory:
            return await asyncio.to_thread(
                self.prometheus.custom_query_range,
                query='sum(container_memory_working_set_bytes{job="kubelet", metrics_path="/metrics/cadvisor", pod=~"$pod", container=~"$container", image!=""})',
                start_time=datetime.datetime.now() - period,
                end_time=datetime.datetime.now(),
                step="30m",
                params={
                    # "namespace": object.namespace,
                    "pod": object.name,
                    "container": object.container,
                },
            )
        else:
            raise ValueError(f"Unknown resource type: {resource}")
