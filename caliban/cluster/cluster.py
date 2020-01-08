"""cluster abstraction for gcloud/gke"""

from __future__ import annotations  # this is for 'forward-decl' type hinting

from typing import Optional, List, Tuple, Dict, Any, Union
from enum import Enum
import os
from argparse import REMAINDER, ArgumentTypeError
import re
import sys

# silence warnings about ssl connection not being verified
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import google
from google.cloud import container_v1
from google.cloud.container_v1.types import Cluster as GCPCluster, NodePool
from google.auth import compute_engine
from google.auth.credentials import Credentials
from google.oauth2 import service_account

import googleapiclient
from googleapiclient.discovery import Resource as discovery

import kubernetes
from kubernetes.client import (V1Job, V1ObjectMeta, V1JobSpec, V1Pod,
                               V1Container, V1EnvVar, V1PodTemplateSpec,
                               V1ResourceRequirements, V1PodSpec, V1Toleration,
                               V1DaemonSet)

import logging

import caliban
import caliban.cli as cli
import caliban.util as u
import caliban.config as conf
from caliban.cloud.types import (TPU, GPU, Accelerator, parse_machine_type,
                                 MachineType, GPUSpec, TPUSpec)
from caliban.cluster.cli import parse_cmd_dict, invoke_command
from caliban.cloud import generate_image_tag

import pprint as pp
from time import sleep
import requests
import yaml
import tqdm

# ----------------------------------------------------------------------------
# misc constants
_COMPUTE_SCOPE_URL = 'https://www.googleapis.com/auth/compute'
_COMPUTE_READONLY_SCOPE_URL = 'https://www.googleapis.com/auth/compute.readonly'
_CLOUD_PLATFORM_SCOPE_URL = 'https://www.googleapis.com/auth/cloud-platform'
_CLOUD_PLATFORM_CREDENTIALS_ENV = 'GOOGLE_APPLICATION_CREDENTIALS'
_KUBE_SYSTEM_NAMESPACE = 'kube-system'
_DEFAULT_NAMESPACE = 'default'
_BATCH_V1_VERSION = 'batch/v1'
_NODE_SELECTOR_GKE_ACCELERATOR = 'cloud.google.com/gke-accelerator'
_NODE_SELECTOR_INSTANCE_TYPE = 'beta.kubernetes.io/instance-type'
_NODE_SELECTOR_PREEMPTIBLE = 'cloud.google.com/gke-preemptible'
_CONTAINER_RESOURCE_LIMIT_TPU = 'cloud-tpus.google.com'  #/v2, /v3
_CONTAINER_RESOURCE_LIMIT_GPU = 'nvidia.com/gpu'
_TEMPLATE_META_ANNOTATION_TPU_TF_VERSION = 'tf-version.cloud-tpus.google.com'  #: "1.14"
_ZONE_DEFAULT = '-'  # all zones
_DEFAULT_MACHINE_TYPE_CPU = conf.DEFAULT_MACHINE_TYPE[conf.JobMode.CPU].value
_DEFAULT_MACHINE_TYPE_GPU = conf.DEFAULT_MACHINE_TYPE[conf.JobMode.GPU].value
_DEFAULT_GPU_SPEC = GPUSpec(GPU.P100, 1)
_DASHBOARD_JOB_URL = 'https://pantheon.corp.google.com/kubernetes/job'
_MAX_GB_PER_CPU = 64
_DEFAULT_CLUSTER_NAME = 'blueshift'
_NVIDIA_DRIVER_COS_DAEMONSET_URL = 'https://raw.githubusercontent.com/GoogleCloudPlatform/container-engine-accelerators/master/nvidia-driver-installer/cos/daemonset-preloaded.yaml'

# ----------------------------------------------------------------------------
# tone down logging from discovery
logging.getLogger('googleapiclient.discovery').setLevel(logging.ERROR)

# ----------------------------------------------------------------------------
def _user_verify(msg: str, default: bool) -> bool:

  if default:
    choice_str = '[Yn]]'
  else:
    choice_str = '[yN]'

  while True:
    ok = input(f'\n {msg} {choice_str}: ').lower()

    if len(ok) == 0:
      return default

    if ok not in ['y', 'n']:
      print('please enter y or n')
      continue

    return (ok == 'y')

  return False

# ----------------------------------------------------------------------------
def _wait_for_operation(client: discovery.Resource,
                        name: str,
                        conditions: List[str] = ['DONE', 'ABORTING'],
                        sleep_sec: int = 1) -> Optional[dict]:
  """wait for cluster operation to reach given state(s)

  Args:
  client (discovery.Resource(container)): api client
  name (str): operation name, of form projects/*/locations/*/operations/*
  conditions (list(str)): exit status conditions
  sleep_sec (int): polling interval

  Returns:
  response dictionary on success, None otherwise
  """

  while True:
    rsp = client.projects().locations().operations().get(name=name).execute()
    if rsp is None:
      logging.error(f'error getting operation {name}')
      return None

    if rsp['status'] in conditions:
      return rsp

    sleep(sleep_sec)

  return None


# ----------------------------------------------------------------------------
def _validate_gpu_spec_against_limits(
    gpu_spec: GPUSpec,
    gpu_limits: Dict[GPU, int],
    limit_type: str,
) -> bool:
  """validate gpu spec against provided limits

  Args:
  gpu_spec (GPUSpec): gpu spec
  gpu_limits (Dict(GPU, int)): limits
  limit_type (str): label for error messages

  Returns:
  True if spec is valid, False otherwise
  """

  if gpu_spec.gpu not in gpu_limits:
    logging.error(f'unsupported gpu type {gpu_spec.gpu.name}. ' +
                  f'Supported types for {limit_type}:')
    for g in gpu_limits:
      print(g)
      return False

  if gpu_spec.count > gpu_limits[gpu_spec.gpu]:
    logging.error(
        f'error: requested {gpu_spec.gpu.name} gpu count {gpu_spec.count} unsupported,'
        + f' {limit_type} max = {gpu_limits[gpu_spec.gpu]}')
    return False

  return True


# ----------------------------------------------------------------------------
def _parse_zone(zone: str) -> Optional[Tuple[str, str]]:
  """parse zone into region and zone tuple

  Args:
  zone (str): zone string

  Returns:
  (region, zone) string tuple on success, None otherwise
  """

  if zone is None:
    return None

  zone_re = re.compile('^(?P<region>[a-z0-9]+-[a-z0-9]+)-(?P<zone>[a-z]+)$')
  match = zone_re.match(zone)
  if match is None:
    return None

  gd = match.groupdict()

  return (gd['region'], gd['zone'])


# ----------------------------------------------------------------------------
def _parse_tpu_spec(spec: str) -> TPUSpec:
  """parse tpu spec, asserts if invalid"""

  tpu_spec_re = re.compile('^(?P<count>[0-9]+)x(?P<tpu>(V2|V3))$')

  match = tpu_spec_re.match(spec)
  if match is None:
    raise ArgumentTypeError(f'invalid tpu spec: {spec}')

  gd = match.groupdict()

  return TPUSpec(TPU[gd['tpu']], int(gd['count']))


# ----------------------------------------------------------------------------
def _get_zone_tpu_types(project_id: str, zone: str,
                        tpu_api: discovery.Resource) -> Optional[List[TPUSpec]]:
  """get list of tpus available in given zone

  Args:
  project_id (str): project id
  zone (str): zone
  tpu_api (discovery.Resource(tpu)): tpu api instance

  Returns:
  list of supported tpu specs on success, None otherwise
  """

  location = 'projects/' + project_id + '/locations/' + zone
  rsp = tpu_api.projects().locations().acceleratorTypes().list(
      parent=location).execute()

  if rsp is None:
    logging.error('error getting tpu types')
    return None

  tpu_re = re.compile('^(?P<tpu>(v2|v3))-(?P<count>[0-9]+)$')

  tpus = []
  for t in rsp['acceleratorTypes']:
    match = tpu_re.match(t['type'])
    if match is None:
      continue
    gd = match.groupdict()
    tpus.append(TPUSpec(TPU[gd['tpu'].upper()], int(gd['count'])))

  return tpus


# ----------------------------------------------------------------------------
def _get_zone_gpu_types(
    project_id: str, zone: str,
    compute_api: discovery.Resource) -> Optional[List[GPUSpec]]:
  """get list of gpu accelerators available in given zone

  Args:
  project_id (str): project id
  zone (str): zone
  compute_api (discovery.Resource(compute)): compute api instance

  Returns:
  list of GPUSpec on success (count is max count), None otherwise
  """

  rsp = compute_api.acceleratorTypes().list(
      project=project_id, zone=zone).execute()

  if rsp is None:
    logging.error('error getting accelerator types')
    return None

  gpu_re = re.compile('^nvidia-tesla-(?P<type>[a-z0-9]+)$')

  gpus = []

  for x in rsp['items']:
    match = gpu_re.match(x['name'])
    if match is None:
      continue
    gd = match.groupdict()
    gpus.append(
        GPUSpec(GPU[gd['type'].upper()], int(x['maximumCardsPerInstance'])))

  return gpus


# ----------------------------------------------------------------------------
def _get_region_quotas(
    project_id: str, region: str,
    compute_api: discovery.Resource) -> Optional[List[Dict[str, Any]]]:
  """get compute quotas for given region

  Args:
  project_id (str): project id
  region (str): region
  compute_api (discovery.Resource(compute)): compute_api instance

  Returns:
  list of quota dicts, with keys {'limit', 'metric', 'usage'}, None on error
  """

  #rsp = compute.instances().list(project=project_id, zone='-').execute()
  #print(f'{rsp}')

  # get our quota data
  rsp = compute_api.regions().get(project=project_id, region=region).execute()

  if rsp is None:
    logging.error('error getting quota information')
    return None

  return rsp['quotas']


# ----------------------------------------------------------------------------
def _generate_resource_limits(
    project_id: str, region: str,
    compute_api: discovery.Resource) -> Optional[List[Dict[str, Any]]]:
  """generate resource limits from quota information

  Args:
  project_id (str): project id
  region (str): region
  compute_api (discovery.Resource(compute)): compute_api instance

  Returns:
  resource limits dictionary on success, None otherwise
  """

  quotas = _get_region_quotas(project_id, region, compute_api)
  if quotas is None:
    return None

  limits = []

  gpu_re = re.compile('^NVIDIA_(?P<gpu>[A-Z0-9]+)_GPUS$')

  for q in quotas:
    metric = q['metric']
    limit = q['limit']

    if metric == 'CPUS':
      limits.append({'resourceType': 'cpu', 'maximum': str(limit)})
      limits.append({
          'resourceType': 'memory',
          'maximum': str(int(limit) * _MAX_GB_PER_CPU)
      })
      continue

    gpu_match = gpu_re.match(metric)
    if gpu_match is None:
      continue

    gd = gpu_match.groupdict()
    gpu_type = gd['gpu']

    limits.append({
        'resourceType': f'nvidia-tesla-{gpu_type.lower()}',
        'maximum': str(limit)
    })

  return limits


# ----------------------------------------------------------------------------
def _job_str(job: V1Job) -> str:
  """format job string to remove all default (None) values

  Args: job (V1Job): job spec

  Returns:
  string describing job
  """

  def nonnull_dict(d: dict) -> dict:
    nnd = {}
    for k, v in d.items():
      if v is None:
        continue
      if type(v) == dict:
        nnd[k] = nonnull_dict(v)
      elif type(v) == list:
        nnd[k] = [nonnull_dict(x) if type(x) == dict else x for x in v]
      else:
        nnd[k] = v

    return nnd

  return pp.pformat(nonnull_dict(job.to_dict()), indent=2, width=80)


# ----------------------------------------------------------------------------
def _sanitize_job_name(name: str) -> str:
  """sanitize job name to fit DNS-1123 restrictions:

  ... a DNS-1123 subdomain must consist of lower case alphanumeric characters,
  '-' or '.', and must start and end with an alphanumeric character.

  Args:
  name (str): job name

  Returns:
  sanitized job name
  """

  name = name.lower()

  if not name[0].isalnum():
    name = 'job-' + name

  if not name[-1].isalnum():
    name = name + '-0'

  def valid(x):
    return x.isalnum() or x in ['-', '.']

  return ''.join([x if valid(x) else '-' for x in name])


# ----------------------------------------------------------------------------
def default_credentials() -> Optional[Tuple(Credentials, str)]:
  """get default cloud credentials

  Returns:
  Credentials on success, None otherwise
  """
  creds, project_id = google.auth.default(
      scopes=[_CLOUD_PLATFORM_SCOPE_URL, _COMPUTE_SCOPE_URL])
  creds.refresh(google.auth.transport.requests.Request())

  return (creds, project_id)


# ----------------------------------------------------------------------------
def credentials_from_file(cred_file: str) -> Optional[Credentials]:
  """get cloud credentials from service account file

  Args:
  cred_file (str): service account credentials file to read

  Returns:
  credentials on success, None otherwise
  """

  creds = service_account.Credentials.from_service_account_file(
      cred_file, scopes=[_CLOUD_PLATFORM_SCOPE_URL, _COMPUTE_SCOPE_URL])

  creds.refresh(google.auth.transport.requests.Request())

  return creds


# ----------------------------------------------------------------------------
def credentials_from_env() -> Optional[Credentials]:
  """get cloud credentials from environment

  Returns:
  credentials on success, None otherwise
  """
  cred_file = os.environ[_CLOUD_PLATFORM_CREDENTIALS_ENV]
  creds = credentials_from_file(cred_file)
  creds.refresh(google.auth.transport.requests.Request())

  return creds


# --------------------------------------------------------------------------
def _get_gcp_clusters(client: ClusterManagerClient,
                      project_id: str,
                      creds: Credentials,
                      zone: str = '-') -> Optional[List[GCPCluster]]:
  """get list of gcp clusters for given project, zone

  Args:
  client (ClusterMangerClient): cluster api client
  project_id (str): project id
  creds (Credentials): credentials
  zone (str): zone, - = all zones

  Returns:
  list of clusters on success, None otherwise
  """

  response = _k(None)(client.list_clusters)(project_id=project_id, zone=zone)
  return response.clusters if response is not None else None


# ----------------------------------------------------------------------------
def _get_gcp_cluster(client: ClusterManagerClient,
                     name: str,
                     project_id: str,
                     creds: Credentials,
                     zone: str = '-') -> Optional[GCPCluster]:
  """get specific cluster instance by name

  Args:
  client (ClusterMangerClient): cluster api client
  name (str): cluster name
  project_id (str): project id
  creds (Credentials): credentials
  zone (str): zone, - = all zones

  Returns:
  GCPCluster on success, None otherwise
  """

  cluster_list = _get_gcp_clusters(client, project_id, creds, zone)
  if cluster_list is None:
    return None

  cluster_dict = dict([(c.name, c) for c in cluster_list])
  if name not in cluster_dict:
    logging.error(f'cluster {name} not found')
    return None

  return cluster_dict[name]


# ----------------------------------------------------------------------------
def connected(error_value: Any) -> Any:
  """decorator for Cluster that checks connection status

  Args:
  error_value (Any): return value on error

  Returns:
  error_value if error connecting, function return value otherwise
  """

  def check(fn):

    def wrapper(self, *args, **kwargs):
      if not self.connected:
        if not self.connect():
          logging.error('error connecting to cluster')
          return error_value
      return fn(self, *args, **kwargs)

    return wrapper

  return check


# ----------------------------------------------------------------------------
def _k(error_value: Any) -> Any:
  """decorator for kuberntes api calls that traps execptions

  This logs exceptions using logging.error()

  Args:
  error_value (Any): value to return on error

  Returns:
  error_value on exception, function return value otherwise
  """

  def check(fn):

    def wrapper(*args, **kwargs):
      try:
        response = fn(*args, **kwargs)
      except Exception as e:
        logging.error(f'error in api call:\n{e}')
        return error_value
      return response

    return wrapper

  return check


# ----------------------------------------------------------------------------
class Cluster(object):
  """cluster

  This is meant as a thin wrapper around GKE clusters and the raw kubernetes
  python api, mainly to provide a simple interface for the most common
  cluster tasks.
  """

  # --------------------------------------------------------------------------
  def __init__(self, name: str, project_id: str, zone: str,
               credentials: Credentials):
    self._cluster_client = None
    self._gcp_cluster = None
    self._core_api = None
    self._batch_api = None
    self._apps_api = None
    self.name = name
    self.project_id = project_id
    self.zone = zone
    self.credentials = credentials
    self.connected = False
    return

  # --------------------------------------------------------------------------
  def connect(self) -> bool:
    """connect to cluster instance

    Returns:
    True on success, False otherwise
    """

    self.connected = False

    # if gcp cluster info already populated, then noop
    # otherwise uses cluster api to get cluster info
    if not self._set_gcp_cluster():
      return False

    # resolve our zone in case the wildcard '-' was passed
    self.zone = self._gcp_cluster.zone

    # set our name in case None was passed
    self.name = self._gcp_cluster.name

    # ok, now we set up the kubernetes api using our cluster info and
    # credentials
    cfg = kubernetes.client.Configuration()
    cfg.host = f'https://{self._gcp_cluster.endpoint}:443'
    cfg.verify_ssl = False  #True #todo: figure out how to do this properly
    #cfg.ssl_ca_cert = c.master_auth.cluster_ca_certificate
    cfg.api_key = {'authorization': 'Bearer ' + self.credentials.token}

    api_client = kubernetes.client.ApiClient(cfg)

    self._core_api = kubernetes.client.CoreV1Api(api_client)
    self._batch_api = kubernetes.client.BatchV1Api(api_client)
    self._apps_api = kubernetes.client.AppsV1Api(api_client)

    # using this as a connection test
    # todo: is there a better way to verify connectivity?
    self.connected = _k(False)(self._core_api.list_pod_for_all_namespaces)(
        watch=False)

    return self.connected

  # --------------------------------------------------------------------------
  def _set_gcp_cluster(self) -> bool:
    """set the gcp cluster for this instance

    Returns:
    True on success, False otherwise
    """

    if self._gcp_cluster is not None:
      return True

    self._cluster_client = container_v1.ClusterManagerClient(
        credentials=self.credentials)

    if self._cluster_client is None:
      logging.error('error getting cluster management client')
      return False

    cluster_list = _get_gcp_clusters(self._cluster_client, self.project_id,
                                     self.credentials, self.zone)
    if cluster_list is None:
      return False
    if len(cluster_list) < 1:
      return False

    if self.name is None and len(cluster_list) > 1:
      logging.error('multiple clusters found, please specify:')
      for c in cluster_list:
        logging.info(c.name)
      return False

    if self.name is None:
      self._gcp_cluster = cluster_list[0]
      return True

    cluster_dict = dict([(c.name, c) for c in cluster_list])
    if self.name not in cluster_dict:
      logging.error(f'cluster {self.name} not found')
      return False

    self._gcp_cluster = cluster_dict[self.name]

    return True

  # --------------------------------------------------------------------------
  @staticmethod
  def list(project_id: str,
           creds: Credentials,
           zone: str = '-') -> Optional[List[str]]:
    """get a list of clusters for given project and zone

    Args:
    project_id (str): gcp project id
    creds (Credentials): credentials
    zone (str): zone, - = all zones

    Returns:
    list of cluster names on success, None otherwise
    """

    client = container_v1.ClusterManagerClient(credentials=creds)

    if client is None:
      logging.error('error getting cluster management client')
      return False

    clusters = _get_gcp_clusters(client, project_id, creds, zone)
    return [c.name for c in clusters] if clusters is not None else None

  # --------------------------------------------------------------------------
  @staticmethod
  def get(name: Optional[str], project_id: str, zone: str,
          creds: Credentials) -> Optional(Cluster):
    """factory method for generating Cluster object

    Note that this also calls connect(), so the resulting cluster is
    already connected. If connect fails, then this method returns None.

    Args:
    name (str): name of cluster, if None, auto-detect
    project_id (str): project id
    zone (str): zone, - = all zones

    Returns:
    cluster instance on success, None otherwise
    """

    cluster = Cluster(
        name=name, project_id=project_id, zone=zone, credentials=creds)

    return cluster if cluster.connect() else None

  # --------------------------------------------------------------------------
  @staticmethod
  def container_limits(
      accelerator: Optional[Accelerator],
      count: int = 1,
      preemptible_tpu: bool = True) -> Optional[Dict[str, str]]:
    """create container limits dictionary for given accelerator type and count

    Args:
    accelerator (Accelerator): (optional) accelerator type
    count (int): accelerator count
    preemptible_tpu (bool): use preemptible tpus (valid only for v2-8 and v3-8)
                            see: https://cloud.google.com/tpu/docs/preemptible
                              this is ignored for other tpu specs

    Returns:
    None for cpu, limits dictionary for gpu/tpu
    """

    if accelerator is None:  # cpu-only
      return None

    if type(accelerator) == GPU:
      return {_CONTAINER_RESOURCE_LIMIT_GPU: count}

    # todo: should we validate tpu/count compatibility here, or should we
    #       assume this is done upstream?
    if type(accelerator) == TPU:
      return {
          '/'.join([
              _CONTAINER_RESOURCE_LIMIT_TPU,
              ('preemptible-' if (preemptible_tpu and count == 8) else '') +
              accelerator.name.lower()
          ]):
              count
      }

    logging.error(f'error: invalid accelerator type: {type(accelerator)}')

    return None

  # --------------------------------------------------------------------------
  @staticmethod
  def template_metadata(
      accelerator: Optional[Accelerator] = None) -> Optional[V1ObjectMeta]:
    """generate template metadata for given accelerator type

    Args:
    accelerator (Accelerator): (optional) accelerator type, or None for cpu

    Returns:
    template metadata necessary for given accelerator
    """

    if type(accelerator) == TPU:
      # todo: right now this is set to 1.14, but need to pass this
      #       somehow...
      return V1ObjectMeta(
          annotations={_TEMPLATE_META_ANNOTATION_TPU_TF_VERSION: '1.14'})

    return None

  # --------------------------------------------------------------------------
  @staticmethod
  def node_selector(
      preemptible: bool = True,
      machine_type: Optional[MachineType] = None,
      accelerator: Optional[Accelerator] = None) -> Optional[Dict[str, str]]:
    """get node selector for given accelerator type and machine spec

    Args:
    preemptible (bool): request preemptible instance
    machine_type (MachineType): (optional) machine type, None = not specified
    accelerator (Accelerator): (optional) accelerator, or None for cpu

    Returns:
    node selector dictionary for given criteria
    """

    selector = {}

    if preemptible:
      selector[_NODE_SELECTOR_PREEMPTIBLE] = 'true'

    if machine_type is not None:
      selector[_NODE_SELECTOR_INSTANCE_TYPE] = machine_type.value

    # see: https://cloud.google.com/kubernetes-engine/docs/how-to/gpus
    if type(accelerator) == GPU:
      selector[_NODE_SELECTOR_GKE_ACCELERATOR] = accelerator.value.lower(
      ).replace('_', '-')

    if len(selector) == 0:
      return None

    return selector

  # --------------------------------------------------------------------------
  @staticmethod
  def tolerations(preemptible: bool = True) -> Optional[List[V1Toleration]]:

    if not preemptible:
      return None

    return [
        V1Toleration(
            key=_NODE_SELECTOR_PREEMPTIBLE,
            operator='Equal',
            value='true',
            effect='NoSchedule')
    ]

  # --------------------------------------------------------------------------
  @connected(None)
  def pods(self) -> Optional[List[V1Pod]]:
    """get a list of pods for this cluster

    Note that this filters out the pods in the kube-system namespace

    Returns:
    list of V1Pod instances on success, None otherwise
    """

    # this returns a V1PodList
    rsp = _k(None)(self._core_api.list_pod_for_all_namespaces)(watch=False)
    if rsp is None:
      return None

    cluster_pods = [
        p for p in rsp.items if p.metadata.namespace != _KUBE_SYSTEM_NAMESPACE
    ]

    return cluster_pods

  # --------------------------------------------------------------------------
  @connected(None)
  def jobs(self) -> Optional[List[V1Job]]:
    """get a list of jobs for this cluster

    Returns:
    list of V1Job instances on success, None otherwise
    """
    rsp = _k(None)(self._batch_api.list_job_for_all_namespaces)(watch=False)
    return rsp.items if rsp is not None else None

  # --------------------------------------------------------------------------
  @connected(None)
  def node_pools(self) -> Optional[List[NodePool]]:
    """get a list of node pools for this cluster

    Returns:
    list of node pools on success, None otherwise
    """

    # todo: re-query this
    return self._gcp_cluster.node_pools

  # --------------------------------------------------------------------------
  @connected(None)
  def submit_job(self,
                 job: V1Job,
                 namespace: str = _DEFAULT_NAMESPACE) -> Optional(V1Job):
    """submit kubernetes

    Args:
    job (V1Job): job spec
    namespace (str): kubernetes namespace

    Returns:
    V1Job on success, None otherwise
    """

    return _k(None)(self._batch_api.create_namespaced_job)(
        namespace=namespace, body=job, async_req=False, pretty=True)

  # --------------------------------------------------------------------------
  @connected(None)
  def create_simple_job(self,
                        name: str,
                        image: str,
                        command: Optional(List[str]) = None,
                        args: Optional(List[str]) = None,
                        env: Dict[str, str] = {},
                        accelerator: Optional[Accelerator] = None,
                        accelerator_count: int = 1,
                        namespace: str = _DEFAULT_NAMESPACE,
                        machine_type: Optional[MachineType] = None,
                        preemptible: bool = True,
                        labels: Optional[Dict[str, str]] = None,
                        preemptible_tpu: bool = True) -> Optional(V1Job):
    """create a simple kubernetes job (1 container, 1 pod) for this cluster

    Args:
    name (str): job name
    image (str): container image url (gcr.io/...)
    command (list of strings): command to execute, None = container entrypoint
    args (list of strings): args to pass to command
    env (dictionary (str, str)): environment vars for container
    accelerator (Accelerator): (optional) accelerator type, None=cpu only
    accelerator_count (int): accelerator count
    namespace (str): kubernetes namespace
    machine_type (MachineType): (optional) machine type, None=default for mode
      (cpu/gpu)
    preemptible (bool): use preemptible instance
    labels (dict(str,str)): (optional) labels to add to job metadata
    preemptible_tpu (bool): use preemptible tpus

    Returns:
    V1Job on success, None otherwise
    """

    # ------------------------------------------------------------------------
    # container

    # tpu/gpu resources
    container_resources = V1ResourceRequirements(
        limits=Cluster.container_limits(accelerator, accelerator_count,
                                        preemptible_tpu))

    container_env = [V1EnvVar(name=k, value=v) for k, v in env.items()]

    # this is a simple 1-container, 1-pod job, so we just name the
    # container the same thing (minus the generated suffix) as the job itself
    container = V1Container(
        name=name,
        image=image,
        command=command,
        args=args,
        resources=container_resources,
        env=container_env)

    # ------------------------------------------------------------------------
    # template

    # todo: should we support anything other than a 'never' restart policy?
    # see this for discussion
    # https://kubernetes.io/docs/concepts/workloads/controllers/jobs-run-to-completion/#pod-backoff-failure-policy
    # backoff count plus 'OnFailure' may be correct here
    template_spec = V1PodSpec(
        restart_policy='Never',
        containers=[container],
        node_selector=Cluster.node_selector(
            preemptible=preemptible,
            machine_type=machine_type,
            accelerator=accelerator))

    # break glass in case NAP adds a taint to auto-created preemptible node pools
    #tolerations=Cluster.tolerations(preemptible=preemptible))

    template = V1PodTemplateSpec(
        metadata=Cluster.template_metadata(accelerator), spec=template_spec)

    # ------------------------------------------------------------------------
    # job
    job_spec = V1JobSpec(template=template, backoff_limit=4)

    # always use generate_name here...todo: is this the best thing to do?
    job_metadata = V1ObjectMeta(generate_name=name + '-', labels=labels)

    job = V1Job(
        api_version=_BATCH_V1_VERSION,
        kind='Job',
        metadata=job_metadata,
        spec=job_spec)

    return job

  # --------------------------------------------------------------------------
  @connected(None)
  def submit_simple_job(self,
                        name: str,
                        image: str,
                        command: Optional(List[str]) = None,
                        args: Optional(List[str]) = None,
                        env: Dict[str, str] = {},
                        accelerator: Optional[Accelerator] = None,
                        accelerator_count: int = 1,
                        namespace: str = _DEFAULT_NAMESPACE,
                        preemptible: bool = True,
                        labels: Optional[Dict[str, str]] = None,
                        preemptible_tpu: bool = True) -> Optional(V1Job):
    """submit a simple kubernetes job (1 container, 1 pod) for this cluster

    Args:
    name (str): job name
    image (str): container image url (gcr.io/...)
    command (list of strings): command to execute, None = container entrypoint
    args (list of strings): args to pass to command
    env (dictionary (str, str)): environment vars for container
    accelerator (Accelerator): (optional) accelerator type, None=cpu only
    accelerator_count (int): accelerator count
    namespace (str): kubernetes namespace
    preemptible (bool): use preemptible instance
    labels (dict(str,str)): (optional) labels to add to job metadata
    preemptible_tpu (bool): use preemptible tpus

    Returns:
    V1Job on success, None otherwise
    """

    job = self.create_simple_job(
        name=name,
        image=image,
        command=command,
        args=args,
        env=env,
        accelerator=accelerator,
        accelerator_count=accelerator_count,
        namespace=namespace,
        preemptible=preemptible,
        labels=labels,
        preemptible_tpu=preemptible_tpu)

    if job is None:
      return None

    return self.submit_job(job)

  # --------------------------------------------------------------------------
  @connected(None)
  def create_simple_experiment_jobs(
      self,
      name: str,
      image: str,
      experiments: Iterable[conf.Experiment],
      command: Optional(List[str]) = None,
      args: Optional[List[str]] = None,
      env: Dict[str, str] = {},
      accelerator: Optional[Accelerator] = None,
      accelerator_count: int = 1,
      namespace: str = _DEFAULT_NAMESPACE,
      machine_type: Optional[MachineType] = None,
      preemptible: bool = True,
      labels: Optional[Dict[str, str]] = None,
      preemptible_tpu: bool = True) -> Iterable[V1Job]:
    """create an iterable of V1Job instances for a set of experiments for

    this cluster

    Args:
    name (str): job name
    image (str): container image url (gcr.io/...)
    experiments (iterable of conf.Experiment): experiment list
    command (list of strings): command to execute, None = container entrypoint
    args (list of strings): args to pass to command
    env (dictionary (str, str)): environment vars for container
    accelerator (Accelerator): (optional) accelerator type, None=cpu only
    accelerator_count (int): accelerator count
    namespace (str): kubernetes namespace
    machine_type (MachineType): (optional) machine type, None=default for mode
      (cpu/gpu)
    preemptible (bool): use preemptible instances
    labels (dict(str,str)): (optional) labels to add to job metadata
    preemptible_tpu (bool): use preemptible tpus

    Returns:
    V1Job iterable on success, None otherwise
    """

    for i, exp in enumerate(experiments, 1):
      complete_args = conf.experiment_to_args(exp, args)
      yield self.create_simple_job(
          name=name,
          image=image,
          command=command,
          args=complete_args,
          env=env,
          accelerator=accelerator,
          accelerator_count=accelerator_count,
          namespace=namespace,
          machine_type=machine_type,
          preemptible=preemptible,
          labels=labels,
          preemptible_tpu=preemptible_tpu)

  # --------------------------------------------------------------------------
  @staticmethod
  def convert_accel_spec(
      gpu_spec: Optional[GPUSpec],
      tpu_spec: Optional[TPUSpec]) -> Optional[Tuple[Accelerator, int]]:

    if gpu_spec is not None and tpu_spec is not None:
      logging.error('error: cannot specify both tpu and gpu')
      return None

    # gpu
    if gpu_spec is not None:
      return (gpu_spec.gpu, gpu_spec.count)

    # tpu
    if tpu_spec is not None:
      return (tpu_spec.tpu, tpu_spec.count)

    # cpu
    return (None, 1)

  # --------------------------------------------------------------------------
  @connected(None)
  def dashboard_url(self, job: V1Job) -> Optional[str]:
    """return dashboard url for given job"""

    md = job.metadata

    url = f'{_DASHBOARD_JOB_URL}/{self.zone}/{self.name}'
    url += f'/{md.namespace}/{md.name}'

    return url

  # --------------------------------------------------------------------------
  @connected(None)
  def get_tpu_types(self) -> Optional[List[TPUSpec]]:
    """get supported tpu types for cluster"""

    tpu_api = googleapiclient.discovery.build(
        'tpu', 'v1', credentials=self.credentials, cache_discovery=False)

    return _get_zone_tpu_types(self.project_id, self.zone, tpu_api)

  # --------------------------------------------------------------------------
  @connected(None)
  def get_gpu_types(self) -> Optional[List[GPUSpec]]:
    """get supported gpu types for cluster"""

    container_api = googleapiclient.discovery.build(
        'container', 'v1', credentials=self.credentials, cache_discovery=False)

    # for some reason, autoprovisioning data is not in the _gcp__cluster
    # instance, so we query using the container api here
    rsp = container_api.projects().locations().clusters().get(
        name=f'projects/{self.project_id}/locations/{self.zone}/clusters/{self.name}'
    ).execute()

    if rsp is None:
      logging.error('error getting cluster info')
      return None

    # for now we just return the gpu resource limits from the autoprovisioning
    # configuration for the cluster
    # todo: take node pool data into account here?
    if 'autoscaling' not in rsp:
      return None

    if 'resourceLimits' not in rsp['autoscaling']:
      return None

    limits = rsp['autoscaling']['resourceLimits']
    #print(pp.pformat(limits))

    gpu_re = re.compile('^nvidia-tesla-(?P<type>[a-z0-9]+)$')
    gpus = []

    for x in limits:
      match = gpu_re.match(x['resourceType'])
      if match is None:
        continue
      gd = match.groupdict()
      gpus.append(GPUSpec(GPU[gd['type'].upper()], int(x['maximum'])))

    return gpus

  # --------------------------------------------------------------------------
  def validate_gpu_spec(self, gpu_spec: Optional[GPUSpec]) -> bool:
    """validate gpu spec against zone and cluster contraints

    Args
    gpu_spec (GPUSpec): gpu spec

    """
    if gpu_spec is None:
      return True

    # ------------------------------------------------------------------------
    # validate against zone instance limits
    compute_api = googleapiclient.discovery.build(
        'compute', 'v1', credentials=self.credentials, cache_discovery=False)

    zone_gpus = _get_zone_gpu_types(self.project_id, self.zone, compute_api)

    if zone_gpus is None:
      return False

    gpu_limits = dict([(x.gpu, x.count) for x in zone_gpus])
    if not _validate_gpu_spec_against_limits(gpu_spec, gpu_limits, 'zone'):
      return False

    # ------------------------------------------------------------------------
    # validate against cluster limits
    available_gpu = self.get_gpu_types()
    if available_gpu is None:
      return False

    gpu_limits = dict([(x.gpu, x.count) for x in available_gpu])
    if not _validate_gpu_spec_against_limits(gpu_spec, gpu_limits, 'cluster'):
      return False

    return True

  # --------------------------------------------------------------------------
  @connected(None)
  def apply_daemonset(
      self,
      daemonset: V1DaemonSet,
      namespace: str = _DEFAULT_NAMESPACE) -> Optional(V1DaemonSet):
    """apply daemonset to cluster

    Args:
    daemonset (V1DaemonSet): daemonset
    namespace (str): kubernetes namespace

    Returns:
    V1DaemonSet on success, None otherwise
    """

    return _k(None)(self._apps_api.create_namespaced_daemon_set)(
        namespace=namespace, body=daemonset, async_req=False, pretty=True)

  # --------------------------------------------------------------------------
  @connected(None)
  def apply_daemonset_from_url(
      self, url: str, parser: Callable[[str], dict]) -> Optional(V1DaemonSet):
    """apply daemonset to cluster from file url

    Args:
    url (str): url for data
    parser (callable): parser for url data, must convert to dictionary or
      V1DaemonSet

    Returns:
    V1DaemonSet on success, None otherwise
    """

    response = requests.get(url)
    if response.status_code != requests.codes.ok:
      print(f'error getting data from {url}')
      return None

    body = parser(response.content)

    namespace = _DEFAULT_NAMESPACE
    if 'metadata' in body:
      namespace = body['metadata'].get('namespace', _DEFAULT_NAMESPACE)

    return self.apply_daemonset(daemonset=body, namespace=namespace)

  # --------------------------------------------------------------------------
  @connected(False)
  def delete(self):
    """delete this cluster

    Returns:
    True on success, False otherwise
    """

    op = self._cluster_client.delete_cluster(
        project_id=self.project_id, zone=self.zone, cluster_id=self.name)

    if op is None:
      logging.error(f'error deleting cluster {self.name}')
      return

    op_name = op.name
    op = _wait_for_operation(
        self._cluster_client,
        f'projects/{self.name}/locations/{self.zone}/operations/{op_name}')

    if rsp['status'] != 'DONE':
      logging.error(f'error deleting cluster {self.name}')
      return

    print(f'successfully deleted cluster {self.name}')

    return


# ----------------------------------------------------------------------------
# ----------------------------------------------------------------------------
def _project_and_creds(fn):
  """cannot specify only one of project_id and cloud_key"""

  def wrapper(args: dict):
    project_id = args.get('project_id', None)
    creds_file = args.get('cloud_key', None)

    if project_id is None and creds_file is None:
      default_creds = default_credentials()
      if default_creds is None:
        return
      creds, project_id = default_creds
    elif creds_file is None:
      creds = credentials_from_env()
    else:
      project_id = caliban.config.extract_project_id(args)
      creds = credentials_from_file(creds_file)

    return fn(args, project_id, creds)

  return wrapper


# ----------------------------------------------------------------------------
def _with_cluster(fn):
  """decorator for cluster methods to get cluster from args"""

  def wrapper(args: dict, project_id: str, creds: Credentials):
    cluster_name = args.get('cluster_name', None)

    cluster = Cluster.get(
        name=cluster_name,
        project_id=project_id,
        zone=_ZONE_DEFAULT,
        creds=creds)

    if cluster is None:
      return

    return fn(args, cluster=cluster)

  return wrapper


# ----------------------------------------------------------------------------
@_project_and_creds
def _cluster_create(args: dict, project_id: str, creds: Credentials) -> None:

  dry_run = args['dry_run']
  cluster_name = args['cluster_name'] or _DEFAULT_CLUSTER_NAME

  # --------------------------------------------------------------------------
  # see if cluster(s) already exist, and if so, check with the user before
  # creating another
  clusters = Cluster.list(project_id=project_id, creds=creds)

  if len(clusters):
    if cluster_name in clusters:
      logging.error(f'cluster {cluster_name} already exists')
      return

    print(f'{len(clusters)} clusters already exist for this project:')
    for c in clusters:
      print(c)

    if not _user_verify(
        'Do you really want to create a new cluster?', default=False):
      return

  # --------------------------------------------------------------------------
  zone = args['zone']
  rz = _parse_zone(zone)
  if rz is None:
    logging.error(f'invalid zone specified: {zone}')
    return

  region, _ = rz

  # --------------------------------------------------------------------------
  # create compute api client and get generate resource limits from quota
  # information
  compute_api = googleapiclient.discovery.build(
      'compute', 'v1', credentials=creds, cache_discovery=False)

  resource_limits = _generate_resource_limits(project_id, region, compute_api)

  # --------------------------------------------------------------------------
  # create the cluster
  # note that as of this writing (2020.01.06), there is a discrepancy between
  # the python/protobuf api and the current rest api, (in particular support
  # for TPUs), so we use the discovery api instead of the dedicated
  # ClusterManagerClient...
  # todo: revisit this
  #cluster_client = container_v1.ClusterManagerClient(credentials=creds)
  cluster_client = googleapiclient.discovery.build(
      'container', 'v1', credentials=creds, cache_discovery=False)

  if cluster_client is None:
    logging.error('error building cluster client')
    return

  # see https://cloud.google.com/container-engine/reference/rest/v1/projects.zones.clusters
  cluster_spec = {
      'name':
          cluster_name,
      'zone':
          zone,
      'ipAllocationPolicy': {
          'useIpAliases': 'true'
      },
      'enable_tpu':
          'true',
      'autoscaling': {
          'enableNodeAutoprovisioning': 'true',
          'autoprovisioningNodePoolDefaults': {
              'oauthScopes': [_COMPUTE_SCOPE_URL, _CLOUD_PLATFORM_SCOPE_URL],
          },
          'resourceLimits': resource_limits,
      },
      'nodePools': [{
          'name': 'default-pool',
          'initialNodeCount': '3',
          'config': {
              'oauthScopes': [
                  'https://www.googleapis.com/auth/devstorage.read_only',
                  'https://www.googleapis.com/auth/logging.write',
                  'https://www.googleapis.com/auth/monitoring',
                  'https://www.googleapis.com/auth/service.management.readonly',
                  'https://www.googleapis.com/auth/servicecontrol',
                  'https://www.googleapis.com/auth/trace.append'
              ],
          },
      }],
  }

  request = {
      'cluster': cluster_spec,
      'parent': f'projects/{project_id}/locations/{zone}'
  }

  if dry_run:
    print(f'\nrequest:\n{pp.pformat(request)}')
    return

  print(f'creating cluster {cluster_name} in project {project_id} in {zone}...')
  print(f'please be patient, this may take several minutes')

  #rsp = cluster_client.create_cluster(
  #    project_id=project_id, zone=zone, cluster=cluster_spec)

  # see https://cloud.google.com/kubernetes-engine/docs/reference/rest/v1/projects.zones.clusters/create
  rsp = cluster_client.projects().zones().clusters().create(
      projectId=project_id, zone=zone, body=request).execute()

  if rsp is None:
    logging.error('error: could not create cluster')
    return

  # wait for creation operation to complete
  operation_name = rsp['name']
  rsp = _wait_for_operation(
      cluster_client,
      f'projects/{project_id}/locations/{zone}/operations/{operation_name}')

  if rsp['status'] != 'DONE':
    logging.error(f'error creating cluster {cluster_name}!')
    return

  # get our newly-created cluster
  cluster = Cluster.get(
      name=cluster_name, project_id=project_id, zone=zone, creds=creds)

  if cluster is None:
    print(f'error: unable to connect to cluster {cluster_name}')
    print(f'nvidia-driver daemonset not applied, to do this manually:')
    print(f'kubectl apply -f {_NVIDIA_DRIVER_COS_DAEMONSET_URL}')
    return

  print(f'created cluster {cluster_name} successfully')
  print(f'applying nvidia driver daemonset...')

  # now apply the nvidia-driver daemonset
  rsp = cluster.apply_daemonset_from_url(
      _NVIDIA_DRIVER_COS_DAEMONSET_URL,
      lambda x: yaml.load(x, Loader=yaml.FullLoader))
  return


# ----------------------------------------------------------------------------
@_project_and_creds
@_with_cluster
def _cluster_delete(args: dict, cluster: Cluster) -> None:
  """delete given cluster

  Args:
  args (dict): args
  cluster (Cluster): cluster to delete

  Returns:
  None
  """

  if _user_verify(
      f'Are you sure you want to delete {cluster.name}?', default=False):
    cluster.delete()

  return


# ----------------------------------------------------------------------------
@_project_and_creds
def _cluster_ls(args: dict, project_id: str, creds: Credentials) -> None:

  clusters = Cluster.list(project_id=project_id, creds=creds)

  if clusters is None:
    return

  cluster_name = args.get('cluster_name', None)

  if cluster_name is not None:
    if cluster_name not in clusters:
      print(f'cluster {cluster_name} not found')
      return
    print(cluster_name)
    return

  print(f'{len(clusters)} clusters found')
  for c in clusters:
    print(c)

  return


# ----------------------------------------------------------------------------
@_project_and_creds
@_with_cluster
def _node_pool_ls(args: dict, cluster: Cluster) -> None:
  """list cluster node pools"""

  np = cluster.node_pools()

  if np is None:
    return

  if len(np) == 0:
    print('no node pools found')
    return

  FMT = '%-20s%-20s%-40s%-20s'
  print(FMT % ('NAME', 'MACHINE TYPE', 'ACCELERATORS', 'MAX NODES'))
  for p in np:
    accel = ','.join([
        '%s(%d)' % (a.accelerator_type, a.accelerator_count)
        for a in p.config.accelerators
    ])
    print(FMT %
          (p.name, p.config.machine_type, accel, p.autoscaling.max_node_count))

  cluster.apply_daemonset_from_url(
      _NVIDIA_DRIVER_COS_DAEMONSET_URL,
      lambda x: yaml.load(x, Loader=yaml.FullLoader))

  return


# ----------------------------------------------------------------------------
@_project_and_creds
@_with_cluster
def _pod_ls(args: dict, cluster: Cluster):

  pods = cluster.pods()
  if pods is None:
    return

  print(f'{len(pods)} pods found')
  for p in pods:
    print(p.metadata.name)

  return


# ----------------------------------------------------------------------------
@_project_and_creds
@_with_cluster
def _job_ls(args: dict, cluster: Cluster):
  jobs = cluster.jobs()

  if jobs is None:
    return

  print(f'{len(jobs)} jobs found')
  for j in jobs:
    print(j.metadata.name)

  return


# ----------------------------------------------------------------------------
@_project_and_creds
@_with_cluster
def _job_submit(args: dict, cluster: Cluster) -> Optional[List[V1Job]]:
  """submit job(s) to cluster

  Args:
  args (dict): argument dictionary
  cluster (Cluster): cluster instance

  Returns:
  list of V1Jobs submitted on success, None otherwise
  """

  script_args = conf.extract_script_args(args)
  job_mode = cli.resolve_job_mode(args)
  docker_args = cli.generate_docker_args(job_mode, args)
  docker_run_args = args.get('docker_run_args', []) or []
  dry_run = args['dry_run']
  package = args['module']
  job_name = args.get('name') or f"caliban_{u.current_user()}"
  gpu_spec = args.get('gpu_spec')
  preemptible = args['preemptible']

  # Arguments to internally build the image required to submit to Cloud.
  docker_m = {'job_mode': job_mode, 'package': package, **docker_args}

  # --------------------------------------------------------------------------
  # validatate gpu spec
  if job_mode == conf.JobMode.GPU and gpu_spec is None:
    gpu_spec = _DEFAULT_GPU_SPEC

  if not cluster.validate_gpu_spec(gpu_spec):
    return

  # --------------------------------------------------------------------------
  # validate tpu spec
  tpu_spec = args.get('tpu_spec')
  preemptible_tpu = args.get('preemptible_tpu')

  if tpu_spec is not None:
    available_tpu = cluster.get_tpu_types()
    if available_tpu is None:
      logging.error('error getting valid tpu types for cluster')
      return

    if tpu_spec not in available_tpu:
      logging.error(f'invalid tpu spec, cluster supports:')
      for t in available_tpu:
        print(f'{t.tpu.name}x{t.count}')
      return

  # --------------------------------------------------------------------------
  image_tag = (
      args.get('image_tag') or generate_image_tag(
          cluster.project_id, docker_args=docker_m, dry_run=dry_run))

  if args.get('machine_type') is None:
    machine_type = conf.DEFAULT_MACHINE_TYPE[job_mode]
  else:
    machine_type = parse_machine_type(args.get('machine_type'))

  experiments = conf.expand_experiment_config(
      args.get('experiment_config') or [{}])

  labels = args.get('label')
  if labels is not None:
    labels = dict(u.sanitize_labels(args.get('label')))

  # convert accelerator spec
  accel_spec = Cluster.convert_accel_spec(gpu_spec, tpu_spec)
  if accel_spec is None:
    return

  accel, accel_count = accel_spec

  # create V1 jobs
  jobs = cluster.create_simple_experiment_jobs(
      name=_sanitize_job_name(job_name),
      image=image_tag,
      experiments=experiments,
      args=script_args,
      accelerator=accel,
      accelerator_count=accel_count,
      machine_type=machine_type,
      preemptible=preemptible,
      labels=labels,
      preemptible_tpu=preemptible_tpu)

  if dry_run:
    print('jobs that would be submitted:')
    for j in jobs:
      print(f'{_job_str(j)}')
    return

  submitted = []
  for j in jobs:
    sj = cluster.submit_job(j)
    if sj is None:
      logging.error(f'error submitting job:\n {j}')
    else:
      submitted.append(sj)
      md = sj.metadata
      spec = sj.spec
      container = sj.spec.template.spec.containers[0]
      logging.info(
          f'submitted job:\n{md.name}: {" ".join(container.args or [])}\n' +
          f'{cluster.dashboard_url(sj)}')

  return submitted


# ----------------------------------------------------------------------------
def run_cli_command(args) -> None:
  """cli entrypoint for cluster commands"""

  invoke_command(args, _COMMAND_DICT)

  return


# ----------------------------------------------------------------------------
def parser(base) -> None:
  """configure parser for cluster commands"""

  parse_cmd_dict(base, _COMMAND_DICT)

  return


# ----------------------------------------------------------------------------
# cli data
# ----------------------------------------------------------------------------
_PROJECT_FLAG = {
    'args': ['--project_id'],
    'kwargs': {
        'help': 'project id, if not specified, uses $PROJECT_ID env variable',
        'type': str,
        'default': os.environ.get('PROJECT_ID', None)
    }
}

_CREDS_FILE_FLAG = {
    'args': ['--creds_file'],
    'kwargs': {
        'help': 'path to credentials file',
        'type': str
    }
}

_ZONE_FLAG = {  #
    'args': ['--zone'],
    'kwargs': {
        'help': 'zone',
        'type': str,
        'required': True
    }
}

_ZONE_WILDCARD_FLAG = {
    'args': ['--zone'],
    'kwargs': {
        'default': '-',
        'help': 'zone',
        'type': str
    }
}

_CLUSTER_NAME_FLAG = {
    'args': ['--cluster_name'],
    'kwargs': {
        'default': None,
        'help': 'cluster name',
        'type': str
    }
}

_NOGPU_FLAG = {
    'args': ['--nogpu'],
    'kwargs': {
        'dest': 'use_gpu',
        'help': 'Disable GPU mode and force CPU-only.',
        'action': 'store_false'
    }
}

_CLOUD_KEY_FLAG = {
    'args': ['--cloud_key'],
    'kwargs': {
        'type': u.validated_file,
        'help': (f'Path to GCloud service account key. ' +
                 f'If not specified, uses default key from ' +
                 f'{_CLOUD_PLATFORM_CREDENTIALS_ENV} environment variable.'),
        'default': f'{os.environ.get(_CLOUD_PLATFORM_CREDENTIALS_ENV, None)}'
    }
}

_EXTRAS_FLAG = {
    'args': ['--extras'],
    'kwargs': {
        'action': 'append',
        'help': 'setup.py dependency keys'
    }
}

_DIR_FLAG = {
    'args': ['-d', '--dir'],
    'kwargs': {
        'action':
            'append',
        'type':
            u.validated_directory,
        'help': ('Extra directories to include. List these from large to ' +
                 "small to take full advantage of Docker's build cache.")
    }
}

_IMAGE_TAG_FLAG = {
    'args': ['--image_tag'],
    'kwargs': {
        'type':
            str,
        'help': ('Docker image tag accessible via Container Registry. If ' +
                 'supplied, Caliban will skip the build and push steps ' +
                 'and use this image tag.')
    }
}

_MODULE_FLAG = {
    'args': ['module'],
    'kwargs': {
        'type':
            u.validated_package,
        'help': ('Code to execute, in either trainer.train or ' +
                 'trainer/train.py format.')
    }
}

_MACHINE_TYPE_FLAG = {
    'args': ['--machine_type'],
    'kwargs': {
        'type':
            str,
        'choices':
            u.enum_vals(MachineType),
        'help': (f"Cloud machine type to request. Default is " +
                 f"{_DEFAULT_MACHINE_TYPE_GPU} in GPU mode, or " +
                 f"{_DEFAULT_MACHINE_TYPE_CPU} in CPU mode")
    }
}

_GPU_SPEC_FLAG = {
    'args': ['--gpu_spec'],
    'kwargs': {
        'metavar':
            GPUSpec.METAVAR,
        'type':
            GPUSpec.parse_arg,
        'help': (f'Type and number of GPUs to use for each job. ' +
                 f'Defaults to 1x{conf.DEFAULT_GPU.name} for GPU mode, or ' +
                 f'None if --nogpu is passed')
    }
}

_TPU_SPEC_FLAG = {
    'args': ['--tpu_spec'],
    'kwargs': {
        'metavar': TPUSpec.METAVAR,
        'type': _parse_tpu_spec,
        'help': (f'Type and number of TPUs to request for each job.'),
        'default': None
    }
}

_PREEMPTIBLE_TPU_FLAG = {
    'args': ['--preemptible_tpu'],
    'kwargs': {
        'type': int,
        'choices': (0, 1),
        'help': ('use preemptible tpus: ' +
                 'note this only applies to v2-8 and v3-8 tpus, see: ' +
                 'https://cloud.google.com/tpu/docs/preemptible'),
        'default': 1
    }
}

_FORCE_FLAG = {
    'args': ['--force'],
    'kwargs': {
        'action': 'store_true',
        'help': 'Force past validations and submit the job as specified.'
    }
}

_JOB_NAME_FLAG = {
    'args': ['--name'],
    'kwargs': {
        'type': str,
        'help': 'job name'
    }
}

_EXPERIMENT_CONFIG_FLAG = {
    'args': ['--experiment_config'],
    'kwargs': {
        'type': conf.load_experiment_config,
        'help': "Path to an experiment config, or 'stdin' to read from stdin."
    }
}

_LABEL_FLAG = {
    'args': ['-l', '--label'],
    'kwargs': {
        'metavar': 'KEY=VALUE',
        'action': 'append',
        'type': u.parse_kv_pair,
        'help': 'Extra label k=v pair for job'
    }
}

_DRY_RUN_FLAG = {
    'args': ['--dry_run'],
    'kwargs': {
        'action': 'store_true',
        'help': "Don't actually submit; log everything that's going to happen."
    }
}

_PASSTHROUGH_ARGS = {
    'args': ['script_args'],
    'kwargs': {
        'nargs':
            REMAINDER,
        'default': [],
        'metavar':
            '-- YOUR_ARGS',
        'help': ('This is a catch-all for arguments you want to pass through' +
                 'to your script. Any args after -- will pass through')
    }
}

# todo: when this feature comes in, change the default here
_PREEMPTIBLE_FLAG = {
    'args': ['--preemptible'],
    'kwargs': {
        'type': int,
        'choices': (0, 1),
        'help': ('use preemptible vm instance: as of 2020.01.03 this is not ' +
                 'supported, but is being developed'),
        'default': 0
    }
}

# ----------------------------------------------------------------------------
_LS_FLAGS = [
    _PROJECT_FLAG, _CREDS_FILE_FLAG, _CLUSTER_NAME_FLAG, _ZONE_WILDCARD_FLAG
]
_CLUSTER_LS_FLAGS = [_PROJECT_FLAG, _CREDS_FILE_FLAG, _ZONE_WILDCARD_FLAG]

# ----------------------------------------------------------------------------
# job commands
_JOB_LS_CMD = {
    'parser_name': 'ls',
    'parser_kwargs': {
        'description': 'list jobs',
        'help': 'list jobs'
    },
    'add_arguments': (_LS_FLAGS),
    'callback': _job_ls
}

_JOB_SUBMIT_CMD = {
    'parser_name': 'submit',
    'parser_kwargs': {
        'description': 'submit cluster job',
        'help': 'submit cluster job'
    },
    'add_arguments': [
        _CLUSTER_NAME_FLAG, _MODULE_FLAG, _NOGPU_FLAG, _CLOUD_KEY_FLAG,
        _EXTRAS_FLAG, _DIR_FLAG, _IMAGE_TAG_FLAG, _PROJECT_FLAG,
        _MACHINE_TYPE_FLAG, _GPU_SPEC_FLAG, _TPU_SPEC_FLAG,
        _PREEMPTIBLE_TPU_FLAG, _FORCE_FLAG, _JOB_NAME_FLAG,
        _EXPERIMENT_CONFIG_FLAG, _LABEL_FLAG, _PREEMPTIBLE_FLAG, _DRY_RUN_FLAG,
        _PASSTHROUGH_ARGS
    ],
    'callback': _job_submit
}

_JOB_CMD_DICT = {
    'parser_name': 'job',
    'parser_kwargs': {
        'description': 'job commands',
        'help': 'job-related commands'
    },
    'subparser': {
        'kwargs': {
            'dest': 'job_cmd'
        },
        'parsers': [_JOB_LS_CMD, _JOB_SUBMIT_CMD]
    }
}

# ----------------------------------------------------------------------------
# pod commands
_POD_LS_CMD = {
    'parser_name': 'ls',
    'parser_kwargs': {
        'description': 'list pods',
        'help': 'list pods'
    },
    'add_arguments': (_LS_FLAGS),
    'callback': _pod_ls
}

_POD_CMD_DICT = {
    'parser_name': 'pod',
    'parser_kwargs': {
        'description': 'pod commands',
        'help': 'pod-related commands'
    },
    'subparser': {
        'kwargs': {
            'dest': 'pod_cmd'
        },
        'parsers': [_POD_LS_CMD]
    }
}

# ----------------------------------------------------------------------------
# node pool commands
_NODE_POOL_LS_CMD = {
    'parser_name': 'ls',
    'parser_kwargs': {
        'description': 'list node pools',
        'help': 'list node pools'
    },
    'add_arguments': (_LS_FLAGS),
    'callback': _node_pool_ls
}

_NODE_POOL_CMD_DICT = {
    'parser_name': 'node_pool',
    'parser_kwargs': {
        'description': 'node pool commands',
        'help': 'node pool-related commands'
    },
    'subparser': {
        'kwargs': {
            'dest': 'node_pool_cmd'
        },
        'parsers': [_NODE_POOL_LS_CMD]
    }
}

# ----------------------------------------------------------------------------
# cluster commands
_CLUSTER_LS_CMD = {
    'parser_name': 'ls',
    'parser_kwargs': {
        'description': 'list clusters',
        'help': 'list clusters'
    },
    'add_arguments': (_CLUSTER_LS_FLAGS),
    'callback': _cluster_ls
}

_CLUSTER_CREATE_FLAGS = [_ZONE_FLAG, _CLUSTER_NAME_FLAG, _DRY_RUN_FLAG]

_CLUSTER_CREATE_CMD = {
    'parser_name': 'create',
    'parser_kwargs': {
        'description': 'create cluster',
        'help': 'create cluster'
    },
    'add_arguments': (_CLUSTER_CREATE_FLAGS),
    'callback': _cluster_create
}

_CLUSTER_DELETE_CMD = {
    'parser_name': 'delete',
    'parser_kwargs': {
        'description': 'delete cluster',
        'help': 'delete cluster'
    },
    'add_arguments': [_CLUSTER_NAME_FLAG],
    'callback': _cluster_delete
}

# ----------------------------------------------------------------------------
# top-level cluster command dictionary
_COMMAND_DICT = {
    'parser_name': 'cluster',
    'parser_kwargs': {
        'description': 'cluster commands',
        'help': 'cluster-related commands'
    },
    'subparser': {
        'kwargs': {
            'dest': 'cluster_cmd'
        },
        'parsers': [
            _CLUSTER_LS_CMD, _POD_CMD_DICT, _JOB_CMD_DICT, _NODE_POOL_CMD_DICT,
            _CLUSTER_CREATE_CMD, _CLUSTER_DELETE_CMD
        ]
    }
}