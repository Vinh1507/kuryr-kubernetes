# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
"""Fixed-IP-via-reserved-port support, mixed into VIF drivers that need it.

A pod can request a stable IP across restarts by setting the
'openstack.org/kuryr-fixed-ips' annotation (a comma-separated list of IPs,
one per StatefulSet ordinal) together with 'openstack.org/kuryr-fixed-ips-
subnet' (the subnet_id the list applies to). Ports used to satisfy such a
request are tagged 'kuryr-reserved' and unbound (kept, not deleted) on pod
release, so the same IP is handed back on the next restart.

This logic is independent of how a VIF driver actually binds/unbinds a
port (e.g. plain Neutron binding vs. nested-VLAN trunk subports), so it's
kept as a standalone mixin rather than living in any single driver module.
"""

from openstack import exceptions as os_exc
from oslo_log import log as logging

from kuryr_kubernetes import clients
from kuryr_kubernetes import exceptions as k_exc


LOG = logging.getLogger(__name__)

RESERVED_PORT_TAG = 'kuryr-reserved'
RESERVED_DEVICE_OWNER = 'kuryr:reserved'
FIXED_IPS_ANNOTATION = 'openstack.org/kuryr-fixed-ips'
FIXED_IPS_SUBNET_ANNOTATION = 'openstack.org/kuryr-fixed-ips-subnet'
POD_INDEX_LABEL = 'apps.kubernetes.io/pod-index'


class FixedIPMixin:
    """Resolves and reserves Neutron ports for pods requesting fixed IPs."""

    def _get_pod_ordinal(self, pod):
        """Get the StatefulSet ordinal index of a pod.

        Prefers the 'apps.kubernetes.io/pod-index' label (set automatically
        by Kubernetes >= 1.28 for StatefulSet pods). Falls back to parsing
        the trailing '-N' from the pod name for older clusters.
        """
        labels = pod['metadata'].get('labels', {})
        index_label = labels.get(POD_INDEX_LABEL)
        if index_label is not None:
            try:
                return int(index_label)
            except ValueError:
                pass

        pod_name = pod['metadata']['name']
        try:
            return int(pod_name.rsplit('-', 1)[-1])
        except ValueError:
            return None

    def _has_fixed_ip_annotation(self, pod):
        annotations = pod['metadata'].get('annotations', {})
        return FIXED_IPS_ANNOTATION in annotations

    def _get_fixed_ip_target(self, pod):
        """Try to resolve a specific IP for this pod's ordinal index.

        Reads the 'openstack.org/kuryr-fixed-ips' pod annotation, a
        comma-separated list of IPs shared across all replicas of a
        StatefulSet. Returns the IP at the pod's ordinal index, or None
        if the annotation is absent, the ordinal can't be determined,
        the index is out of range, or the entry at that index is empty.
        A None return (while the annotation key is still present) means
        the pod wants a fixed IP but doesn't care which address — see
        _resolve_fixed_ip().
        """
        ips_raw = pod['metadata'].get('annotations', {}).get(
            FIXED_IPS_ANNOTATION)
        if ips_raw is None:
            return None

        ordinal = self._get_pod_ordinal(pod)
        if ordinal is None:
            return None

        ip_list = [ip.strip() for ip in ips_raw.split(',')]
        if ordinal < 0 or ordinal >= len(ip_list) or not ip_list[ordinal]:
            return None

        return ip_list[ordinal]

    def _annotation_applies_to_subnet(self, pod, subnet_id):
        """Check whether the fixed-ips annotation targets this subnet.

        A pod's 'openstack.org/kuryr-fixed-ips' annotation is shared by
        every VIF request for that pod (e.g. both the main pod-cidr
        interface and any additional NPWG interface). Without this
        check, an IP meant for one subnet (say, a customer VPC) would
        also be (wrongly) attempted on unrelated subnets such as the
        main pod-cidr one.

        The 'openstack.org/kuryr-fixed-ips-subnet' annotation is the
        explicit, authoritative target subnet_id. If it's not set, the
        fixed-ips annotation doesn't apply to any subnet (safer than
        guessing) -- the pod must declare which subnet it targets.
        """
        annotations = pod['metadata'].get('annotations', {})
        target_subnet_id = annotations.get(FIXED_IPS_SUBNET_ANNOTATION)
        if not target_subnet_id:
            return False
        return target_subnet_id == subnet_id

    def _resolve_fixed_ip(self, pod, subnets):
        """Decide how to satisfy this pod's fixed-IP request, if any.

        Returns None if the pod has no 'openstack.org/kuryr-fixed-ips'
        annotation at all, or if 'openstack.org/kuryr-fixed-ips-subnet'
        doesn't match the subnet currently being requested -- fully
        normal dynamic-IP flow, untouched.

        Otherwise returns a dict with exactly one key:
          'port': an existing Neutron port to rebind to this pod
          'create_ip': a specific IP to request when creating a new
                       port (the annotation names one for this ordinal,
                       but no matching reserved port exists yet)
          'create_reserved': True, meaning create a port with a
                       dynamically-allocated IP and reserve it (no
                       specific IP could be resolved for this ordinal)
        """
        if not self._has_fixed_ip_annotation(pod):
            return None

        pod_name = pod['metadata']['name']
        namespace = pod['metadata']['namespace']
        subnet_id = next(iter(subnets))

        if not self._annotation_applies_to_subnet(pod, subnet_id):
            LOG.debug('Fixed-ips annotation on pod %s does not target '
                      'subnet %s, skipping fixed IP for this interface.',
                      pod_name, subnet_id)
            return None

        os_net = clients.get_network_client()

        target_ip = self._get_fixed_ip_target(pod)
        if target_ip:
            ports = list(os_net.ports(
                tags=[RESERVED_PORT_TAG],
                fixed_ips=[f'ip_address={target_ip}',
                          f'subnet_id={subnet_id}'],
            ))
            if ports:
                if len(ports) > 1:
                    LOG.warning('Multiple reserved ports found for IP %s '
                                'on subnet %s, using first one',
                                target_ip, subnet_id)
                return {'port': ports[0]}
            return {'create_ip': target_ip}

        LOG.debug('No specific fixed IP resolved for pod %s, falling back '
                  'to an auto-reserved dynamic IP.', pod_name)
        port_name = f"{namespace}_{pod_name}"
        ports = list(os_net.ports(
            name=port_name,
            tags=[RESERVED_PORT_TAG],
            fixed_ips=[f'subnet_id={subnet_id}'],
        ))
        if ports:
            if len(ports) > 1:
                LOG.warning('Multiple auto-reserved ports found for pod %s '
                            'on subnet %s, using first one',
                            pod_name, subnet_id)
            return {'port': ports[0]}
        return {'create_reserved': True}

    def _mark_port_reserved(self, port):
        """Tag a freshly created port so its IP is kept across restarts.

        Only needed as a fallback when Neutron doesn't support tagging
        during port creation (self._tag_on_creation is False); otherwise
        the tag is set atomically as part of the create_port request.
        """
        os_net = clients.get_network_client()
        try:
            existing_tags = list(port.tags or [])
            if RESERVED_PORT_TAG not in existing_tags:
                os_net.set_tags(port, tags=existing_tags + [RESERVED_PORT_TAG])
        except os_exc.SDKException:
            LOG.warning('Failed to tag port %s as reserved. Its IP will '
                        'not be kept across pod restarts.', port.id)

    def _is_statefulset_decommissioning(self, pod):
        """Whether this pod's reserved port should be released for good.

        Mirrors how Kubernetes' own persistentVolumeClaimRetentionPolicy
        decides whenDeleted/whenScaled for StatefulSet-owned PVCs: a
        reserved port's IP should only be released permanently when the
        owning StatefulSet itself is gone, or no longer wants this
        ordinal -- not on every transient pod restart (crash, rolling
        update, eviction, etc.), where the reservation must be kept so
        the same IP comes back.

        Returns False (keep reserved) if the pod isn't owned by a
        StatefulSet, or if that can't be determined -- never release
        without a clear, positive signal that this ordinal is gone.
        """
        owner_refs = pod['metadata'].get('ownerReferences', [])
        sts_ref = next(
            (o for o in owner_refs if o.get('kind') == 'StatefulSet'), None)
        if not sts_ref:
            return False

        k8s = clients.get_kubernetes_client()
        namespace = pod['metadata']['namespace']
        try:
            sts = k8s.get(f"/apis/apps/v1/namespaces/{namespace}"
                          f"/statefulsets/{sts_ref['name']}")
        except k_exc.K8sResourceNotFound:
            # The StatefulSet itself is gone -- this is a real decommission.
            return True

        ordinal = self._get_pod_ordinal(pod)
        replicas = sts['spec'].get('replicas', 1)
        if ordinal is not None and ordinal >= replicas:
            # Scaled down below this ordinal -- it's not coming back.
            return True

        return False
