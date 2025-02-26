#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Module used to set a privileged context for Kubernetes Statefulset containers
   and handle SR-IOV VF + hugepages resources.
"""

import logging

from lightkube import Client
from lightkube.core.exceptions import ApiError
from lightkube.models.core_v1 import Container, ResourceRequirements
from lightkube.resources.apps_v1 import StatefulSet

logger = logging.getLogger(__name__)


class OAIDUK8sError(Exception):
    """Generic error for OAI DU K8s operations."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(self.message)


class DUSecurityContext:
    """Class used to update the Kubernetes Statefulset for OAI DU container."""

    def __init__(
        self,
        namespace: str,
        statefulset_name: str,
        container_name: str,
    ):
        self.k8s_client = Client()
        self.statefulset_name = statefulset_name
        self.container_name = container_name
        self.namespace = namespace

    def is_privileged(self) -> bool:
        """Check whether the container in the StatefulSet runs in privileged context.

        Returns:
            bool: True if the container is privileged, otherwise False
        """
        try:
            statefulset = self.k8s_client.get(
                res=StatefulSet,
                name=self.statefulset_name,
                namespace=self.namespace,
            )
            container = next(
                filter(
                    lambda ctr: ctr.name == self.container_name,
                    statefulset.spec.template.spec.containers,  # type: ignore[union-attr]
                )
            )
            if not container.securityContext.privileged:
                return False
        except ApiError:
            raise OAIDUK8sError(f"Could not get statefulset {self.statefulset_name}")
        except StopIteration:
            raise OAIDUK8sError(f"Could not get container {self.container_name}")
        return True

    def set_privileged(self) -> None:
        """Patch the StatefulSet to run container in privileged context and add extra capabilities."""
        try:
            statefulset = self.k8s_client.get(
                res=StatefulSet,
                name=self.statefulset_name,
                namespace=self.namespace,
            )
            container = next(
                filter(
                    lambda ctr: ctr.name == self.container_name,
                    statefulset.spec.template.spec.containers,
                )
            )
            container.securityContext.privileged = True

            # Add extra capabilities
            if not container.securityContext.capabilities:
                container.securityContext.capabilities = {"add": ["IPC_LOCK", "SYS_ADMIN"]}
            else:
                container.securityContext.capabilities.add = ["IPC_LOCK", "SYS_ADMIN"]

            self.k8s_client.replace(obj=statefulset)
            logger.info("Container %s patched for privileged mode with extra capabilities", self.container_name)
        except ApiError:
            raise OAIDUK8sError(f"Could not get statefulset {self.statefulset_name}")
        except StopIteration:
            raise OAIDUK8sError(f"Could not get container {self.container_name}")

    def sriov_vfs_attached(self) -> bool:
        """Check if our container requests 2 SR-IOV VFs + 10Gi of hugepages."""
        try:
            statefulset = self.k8s_client.get(
                res=StatefulSet,
                name=self.statefulset_name,
                namespace=self.namespace,
            )
        except ApiError:
            raise OAIDUK8sError(f"Could not get statefulset {self.statefulset_name}")

        # find container
        container = next(
            (
                ctr for ctr in statefulset.spec.template.spec.containers  # type: ignore[union-attr]
                if ctr.name == self.container_name
            ),
            None,
        )
        if not container:
            raise OAIDUK8sError(f"Container {self.container_name} not found")

        # get existing requests
        requests = getattr(getattr(container, "resources", None), "requests", {}) or {}
        sriov = requests.get("intel.com/intel_oran_sriov", "0")
        hugepages = requests.get("hugepages-1Gi", "0")

        # If the container requests 2 SR-IOV VFs & 10Gi hugepages, consider them "attached"
        return sriov == "2" and hugepages == "10Gi"

    def attach_sriov_resources(self) -> None:
        """Patch the StatefulSet to request 2 SR-IOV VFs, 10Gi of hugepages, and add mounts."""
        try:
            statefulset = self.k8s_client.get(
                res=StatefulSet,
                name=self.statefulset_name,
                namespace=self.namespace,
            )
        except ApiError:
            raise OAIDUK8sError(f"Could not get statefulset {self.statefulset_name}")

        containers = statefulset.spec.template.spec.containers  # type: ignore[union-attr]
        container = next((c for c in containers if c.name == self.container_name), None)
        if not container:
            raise OAIDUK8sError(f"Container {self.container_name} not found")

        if not container.resources:
            container.resources = ResourceRequirements()

        if not container.resources.requests:
            container.resources.requests = {}
        if not container.resources.limits:
            container.resources.limits = {}

        # Patch resource requests and limits for SR-IOV and hugepages
        container.resources.requests["intel.com/intel_oran_sriov"] = "2"
        container.resources.limits["intel.com/intel_oran_sriov"] = "2"
        container.resources.requests["hugepages-1Gi"] = "10Gi"
        container.resources.limits["hugepages-1Gi"] = "10Gi"
        container.resources.requests["cpu"] = "8"
        container.resources.limits["cpu"] = "8"
        container.resources.requests["memory"] = "16Gi"
        container.resources.limits["memory"] = "16Gi"

        # ---- Add volume mounts ----

        # Ensure volumes list exists
        if not statefulset.spec.template.spec.volumes:
            statefulset.spec.template.spec.volumes = []

        # Add a volume for hugepages if not present
        if not any(vol.name == "hugepages" for vol in statefulset.spec.template.spec.volumes):
            statefulset.spec.template.spec.volumes.append({
                "name": "hugepages",
                "emptyDir": {"medium": "HugePages"}
            })

        # Add a volume for dpdk (hostPath mount)
        if not any(vol.name == "dpdk" for vol in statefulset.spec.template.spec.volumes):
            statefulset.spec.template.spec.volumes.append({
                "name": "dpdk",
                "hostPath": {"path": "/var/run/dpdk"}
            })

        # Ensure the container has a volumeMounts list
        if not container.volumeMounts:
            container.volumeMounts = []

        # Add mount for hugepages into container
        if not any(vm.name == "hugepages" for vm in container.volumeMounts):
            container.volumeMounts.append({
                "name": "hugepages",
                "mountPath": "/dev/hugepages"
            })

        # Add mount for dpdk
        if not any(vm.name == "dpdk" for vm in container.volumeMounts):
            container.volumeMounts.append({
                "name": "dpdk",
                "mountPath": "/var/run/dpdk"
            })

        try:
            self.k8s_client.replace(obj=statefulset)
            logger.info(
                "Patched StatefulSet %s with SR-IOV=2, hugepages=10Gi, and mounted hugepages and dpdk",
                self.statefulset_name,
            )
        except ApiError as e:
            raise OAIDUK8sError(f"Could not patch statefulset: {e}")
