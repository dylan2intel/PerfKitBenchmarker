# Copyright 2014 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Module containing classes related to GCE disks.

Disks can be created, deleted, attached to VMs, and detached from VMs.
Use 'gcloud compute disk-types list' to determine valid disk types.
"""

import json

from absl import flags
from perfkitbenchmarker import disk
from perfkitbenchmarker import errors
from perfkitbenchmarker import providers
from perfkitbenchmarker import vm_util
from perfkitbenchmarker.providers.gcp import util

FLAGS = flags.FLAGS

PD_STANDARD = 'pd-standard'
PD_SSD = 'pd-ssd'
PD_BALANCED = 'pd-balanced'
PD_EXTREME = 'pd-extreme'

DISK_TYPE = {disk.STANDARD: PD_STANDARD, disk.REMOTE_SSD: PD_SSD}

REGIONAL_DISK_SCOPE = 'regional'

DISK_METADATA = {
    PD_STANDARD: {
        disk.MEDIA: disk.HDD,
        disk.REPLICATION: disk.ZONE,
    },
    PD_BALANCED: {
        disk.MEDIA: disk.SSD,
        disk.REPLICATION: disk.ZONE,
    },
    PD_SSD: {
        disk.MEDIA: disk.SSD,
        disk.REPLICATION: disk.ZONE,
    },
    PD_EXTREME: {
        disk.MEDIA: disk.SSD,
        disk.REPLICATION: disk.ZONE,
    },
    disk.LOCAL: {
        disk.MEDIA: disk.SSD,
        disk.REPLICATION: disk.NONE,
    },
}

SCSI = 'SCSI'
NVME = 'NVME'

disk.RegisterDiskTypeMap(providers.GCP, DISK_TYPE)


class GceDisk(disk.BaseDisk):
  """Object representing an GCE Disk."""

  def __init__(self,
               disk_spec,
               name,
               zone,
               project,
               image=None,
               image_project=None,
               replica_zones=None):
    super(GceDisk, self).__init__(disk_spec)
    self.attached_vm_name = None
    self.image = image
    self.image_project = image_project
    self.name = name
    self.zone = zone
    self.project = project
    self.replica_zones = replica_zones
    self.region = util.GetRegionFromZone(self.zone)
    self.provisioned_iops = None
    if self.disk_type == PD_EXTREME:
      self.provisioned_iops = FLAGS.gcp_provisioned_iops

    disk_metadata = DISK_METADATA[disk_spec.disk_type]
    if self.replica_zones:
      disk_metadata[disk.REPLICATION] = disk.REGION
      self.metadata['replica_zones'] = replica_zones
    self.metadata.update(DISK_METADATA[disk_spec.disk_type])
    if self.disk_type == disk.LOCAL:
      self.metadata['interface'] = FLAGS.gce_ssd_interface
    if self.provisioned_iops and self.disk_type == PD_EXTREME:
      self.metadata['provisioned_iops'] = self.provisioned_iops

  def _Create(self):
    """Creates the disk."""
    cmd = util.GcloudCommand(self, 'compute', 'disks', 'create', self.name)
    cmd.flags['size'] = self.disk_size
    cmd.flags['type'] = self.disk_type
    if self.provisioned_iops and self.disk_type == PD_EXTREME:
      cmd.flags['provisioned-iops'] = self.provisioned_iops
    cmd.flags['labels'] = util.MakeFormattedDefaultTags()
    if self.image:
      cmd.flags['image'] = self.image
    if self.image_project:
      cmd.flags['image-project'] = self.image_project

    if self.replica_zones:
      cmd.flags['region'] = self.region
      cmd.flags['replica-zones'] = ','.join(self.replica_zones)
      del cmd.flags['zone']

    _, stderr, retcode = cmd.Issue(raise_on_failure=False)
    util.CheckGcloudResponseKnownFailures(stderr, retcode)

  def _Delete(self):
    """Deletes the disk."""
    cmd = util.GcloudCommand(self, 'compute', 'disks', 'delete', self.name)
    if self.replica_zones:
      cmd.flags['region'] = self.region
      del cmd.flags['zone']
    cmd.Issue(raise_on_failure=False)

  def _Describe(self):
    """Returns json describing the disk or None on failure."""
    cmd = util.GcloudCommand(self, 'compute', 'disks', 'describe', self.name)

    if self.replica_zones:
      cmd.flags['region'] = self.region
      del cmd.flags['zone']

    stdout, _, _ = cmd.Issue(suppress_warning=True, raise_on_failure=False)
    try:
      result = json.loads(stdout)
    except ValueError:
      return None
    return result

  def _Ready(self):
    """Returns true if the disk is ready."""
    result = self._Describe()
    return result['status'] == 'READY'

  def _Exists(self):
    """Returns true if the disk exists."""
    result = self._Describe()
    return bool(result)

  @vm_util.Retry()
  def Attach(self, vm):
    """Attaches the disk to a VM.

    Args:
      vm: The GceVirtualMachine instance to which the disk will be attached.
    """
    self.attached_vm_name = vm.name
    cmd = util.GcloudCommand(self, 'compute', 'instances', 'attach-disk',
                             self.attached_vm_name)
    cmd.flags['device-name'] = self.name
    cmd.flags['disk'] = self.name

    if self.replica_zones:
      cmd.flags['disk-scope'] = REGIONAL_DISK_SCOPE

    stdout, stderr, retcode = cmd.Issue(raise_on_failure=False)
    # Gcloud attach-disk commands may still attach disks despite being rate
    # limited.
    if retcode:
      if (cmd.rate_limited and 'is already being used' in stderr and
          FLAGS.retry_on_rate_limited):
        return
      debug_text = ('Ran: {%s}\nReturnCode:%s\nSTDOUT: %s\nSTDERR: %s' %
                    (' '.join(cmd.GetCommand()), retcode, stdout, stderr))
      raise errors.VmUtil.CalledProcessException(
          'Command returned a non-zero exit code:\n{}'.format(debug_text))

  def Detach(self):
    """Detaches the disk from a VM."""
    cmd = util.GcloudCommand(self, 'compute', 'instances', 'detach-disk',
                             self.attached_vm_name)
    cmd.flags['device-name'] = self.name

    if self.replica_zones:
      cmd.flags['disk-scope'] = REGIONAL_DISK_SCOPE
    cmd.IssueRetryable()
    self.attached_vm_name = None

  def GetDevicePath(self):
    """Returns the path to the device inside the VM."""
    if self.disk_type == disk.LOCAL and FLAGS.gce_ssd_interface == NVME:
      return '/dev/%s' % self.name
    else:
      # by default, gce_ssd_interface == SCSI and returns this name id
      return '/dev/disk/by-id/google-%s' % self.name
