from . import VMBaseClass
from .releases import base_vm_classes as relbase
from unittest import SkipTest

import textwrap


class TestZfsRootAbs(VMBaseClass):
    interactive = False
    nr_cpus = 2
    dirty_disks = True
    conf_file = "examples/tests/zfsroot.yaml"
    extra_disks = []
    collect_scripts = VMBaseClass.collect_scripts + [
        textwrap.dedent("""
            cd OUTPUT_COLLECT_D
            blkid -o export /dev/vda > blkid_output_vda
            blkid -o export /dev/vda1 > blkid_output_vda1
            blkid -o export /dev/vda2 > blkid_output_vda2
            zfs list > zfs_list
            zpool list > zpool_list
            zpool status > zpool_status
            cat /proc/partitions > proc_partitions
            cat /proc/mounts > proc_mounts
            cat /proc/cmdline > proc_cmdline
            ls -al /dev/disk/by-uuid/ > ls_uuid
            cat /etc/fstab > fstab
            mkdir -p /dev/disk/by-dname
            ls /dev/disk/by-dname/ > ls_dname
            find /etc/network/interfaces.d > find_interfacesd
            v=""
            out=$(apt-config shell v Acquire::HTTP::Proxy)
            eval "$out"
            echo "$v" > apt-proxy
            cp /etc/environment etc_environment
        """)]

    def test_output_files_exist(self):
        self.output_files_exist(
            ["blkid_output_vda", "blkid_output_vda1", "blkid_output_vda2",
             "fstab", "ls_dname", "ls_uuid",
             "proc_partitions",
             "root/curtin-install.log", "root/curtin-install-cfg.yaml"])

    def test_ptable(self):
        blkid_info = self.get_blkid_data("blkid_output_vda")
        self.assertEquals(blkid_info["PTTYPE"], "gpt")

    def test_zfs_list(self):
        """Check rpoot/ROOT/ubuntu is mounted at slash"""
        self.output_files_exist(['zfs_list'])
        self.check_file_regex('zfs_list', r"rpool/ROOT/ubuntu.*/\n")

    def test_env_has_zpool_vdev_name_path(self):
        """Target env has ZPOOL_VDEV_NAME_PATH=1 set"""
        # this is a Xenial only check
        if self.release != ['xenial']:
            raise SkipTest("skip test_env_has_zpool_vdev %s:" % self.release)
        self.output_files_exist(['etc_environment'])
        self.check_file_regex('etc_environment', r'ZPOOL_VDEV_NAME_PATH="1"')

    def test_proc_cmdline_has_root_zfs(self):
        """Check /proc/cmdline has root=ZFS=<pool>"""
        self.output_files_exist(['proc_cmdline'])
        self.check_file_regex('proc_cmdline', r"root=ZFS=rpool/ROOT/ubuntu")


class XenialGATestZfsRoot(relbase.xenial_ga, TestZfsRootAbs):
    __test__ = True


class XenialHWETestZfsRoot(relbase.xenial_hwe, TestZfsRootAbs):
    __test__ = True


class XenialEdgeTestZfsRoot(relbase.xenial_edge, TestZfsRootAbs):
    __test__ = True


class ArtfulTestZfsRoot(relbase.artful, TestZfsRootAbs):
    __test__ = True


class BionicTestZfsRoot(relbase.bionic, TestZfsRootAbs):
    __test__ = True