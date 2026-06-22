import json

from oslo_log import log as logging

from kuryr_kubernetes import constants
from kuryr_kubernetes import exceptions as k_exc
from kuryr_kubernetes.controller.drivers import utils
from kuryr_kubernetes import utils as k_utils

LOG = logging.getLogger(__name__)


def get_if_ip_map(vifs):
    """Return dict mapping ifname to first IP of each VIF."""
    result = {}
    for ifname, data in vifs.items():
        try:
            vif = data['vif']
            ip = str(vif.network.subnets.objects[0].ips.objects[0].address)
            result[ifname] = ip
        except (AttributeError, IndexError):
            LOG.warning('Could not extract IP from vif %s', ifname)
    return result


def annotate_pod_if_ips(k8s, pod, vifs):
    """Patch pod annotation with interface-to-IP mapping from vifs."""
    if_ip_map = get_if_ip_map(vifs)
    if not if_ip_map:
        return
    try:
        k8s.annotate(
            k_utils.get_res_link(pod),
            {constants.K8S_ANNOTATION_IF_IP: json.dumps(if_ip_map)},
            resource_version=pod['metadata']['resourceVersion'])
    except k_exc.K8sClientException:
        LOG.warning('Failed to annotate pod %s with interface IPs',
                    pod['metadata']['name'])
