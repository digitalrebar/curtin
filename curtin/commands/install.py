# This file is part of curtin. See LICENSE file for copyright and license info.

import argparse
from copy import deepcopy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

from curtin.block import iscsi, zfs
from curtin import config
from curtin import distro
from curtin import util
from curtin import paths
from curtin import version
from curtin.log import LOG, logged_time
from curtin.reporter.legacy import load_reporter
from curtin.reporter import events
from . import populate_one_subcmd

INSTALL_LOG = "/var/log/curtin/install.log"
# Upon error, curtin creates a tar of all related logs at ERROR_TARFILE
ERROR_TARFILE = '/var/log/curtin/curtin-error-logs.tar'
SAVE_INSTALL_LOG = '/root/curtin-install.log'
SAVE_INSTALL_CONFIG = '/root/curtin-install-cfg.yaml'

INSTALL_START_MSG = ("curtin: Installation started. (%s)" %
                     version.version_string())
INSTALL_PASS_MSG = "curtin: Installation finished."
INSTALL_FAIL_MSG = "curtin: Installation failed with exception: {exception}"

STAGE_DESCRIPTIONS = {
    'early': 'preparing for installation',
    'partitioning': 'configuring storage',
    'network': 'configuring network',
    'extract': 'writing install sources to disk',
    'curthooks': 'configuring installed system',
    'hook': 'finalizing installation',
    'late': 'executing late commands',
}

CONFIG_BUILTIN = {
    'sources': {},
    'stages': ['early', 'partitioning', 'network', 'extract', 'curthooks',
               'hook', 'late'],
    'extract_commands': {'builtin': ['curtin', 'extract']},
    'hook_commands': {'builtin': ['curtin', 'hook']},
    'partitioning_commands': {
        'builtin': ['curtin', 'block-meta', 'simple']},
    'curthooks_commands': {'builtin': ['curtin', 'curthooks']},
    'late_commands': {'builtin': []},
    'network_commands': {'builtin': ['curtin', 'net-meta', 'auto']},
    'apply_net_commands': {'builtin': []},
    'install': {'log_file': INSTALL_LOG,
                'error_tarfile': ERROR_TARFILE}
}


def clear_install_log(logfile):
    """Clear the installation log, so no previous installation is present."""
    util.ensure_dir(os.path.dirname(logfile))
    try:
        open(logfile, 'w').close()
    except Exception:
        pass


def copy_install_log(logfile, target, log_target_path):
    """Copy curtin install log file to target system"""
    basemsg = 'Cannot copy curtin install log "%s" to target.' % logfile
    if not logfile:
        LOG.warn(basemsg)
        return
    if not os.path.isfile(logfile):
        LOG.warn(basemsg + "  file does not exist.")
        return

    LOG.debug('Copying curtin install log from %s to target/%s',
              logfile, log_target_path)
    util.write_file(
        filename=paths.target_path(target, log_target_path),
        content=util.load_file(logfile, decode=False),
        mode=0o400, omode="wb")


def writeline_and_stdout(logfile, message):
    writeline(logfile, message)
    out = sys.stdout
    msg = message + "\n"
    if hasattr(out, 'buffer'):
        out = out.buffer  # pylint: disable=no-member
        msg = msg.encode()
    out.write(msg)
    out.flush()


def writeline(fname, output):
    """Write a line to a file."""
    if not output.endswith('\n'):
        output += '\n'
    try:
        with open(fname, 'a') as fp:
            fp.write(output)
    except IOError:
        pass


class WorkingDir(object):
    def __init__(self, config):
        top_d = tempfile.mkdtemp()
        state_d = os.path.join(top_d, 'state')
        scratch_d = os.path.join(top_d, 'scratch')
        for p in (state_d, scratch_d):
            os.mkdir(p)

        target_d = config.get('install', {}).get('target')
        if not target_d:
            target_d = os.path.join(top_d, 'target')
        try:
            util.ensure_dir(target_d)
        except OSError as e:
            raise ValueError(
                "Unable to create target directory '%s': %s" %
                (target_d, e))
        if os.listdir(target_d) != []:
            raise ValueError(
                "Provided target dir '%s' was not empty." % target_d)

        netconf_f = os.path.join(state_d, 'network_config')
        netstate_f = os.path.join(state_d, 'network_state')
        interfaces_f = os.path.join(state_d, 'interfaces')
        config_f = os.path.join(state_d, 'config')
        fstab_f = os.path.join(state_d, 'fstab')

        with open(config_f, "w") as fp:
            json.dump(config, fp)

        # just touch these files to make sure they exist
        for f in (config_f, fstab_f, netconf_f, netstate_f):
            with open(f, "ab") as fp:
                pass

        self.scratch = scratch_d
        self.target = target_d
        self.top = top_d
        self.interfaces = interfaces_f
        self.netconf = netconf_f
        self.netstate = netstate_f
        self.fstab = fstab_f
        self.config = config
        self.config_file = config_f

    def env(self):
        return ({'WORKING_DIR': self.scratch, 'OUTPUT_FSTAB': self.fstab,
                 'OUTPUT_INTERFACES': self.interfaces,
                 'OUTPUT_NETWORK_CONFIG': self.netconf,
                 'OUTPUT_NETWORK_STATE': self.netstate,
                 'TARGET_MOUNT_POINT': self.target,
                 'CONFIG': self.config_file})


class Stage(object):

    def __init__(self, name, commands, env, reportstack=None, logfile=None):
        self.name = name
        self.commands = commands
        self.env = env
        if logfile is None:
            logfile = INSTALL_LOG
        self.install_log = self._open_install_log(logfile)

        if hasattr(sys.stdout, 'buffer'):
            self.write_stdout = self._write_stdout3
        else:
            self.write_stdout = self._write_stdout2

        if reportstack is None:
            reportstack = events.ReportEventStack(
                name="stage-%s" % name, description="basic stage %s" % name,
                reporting_enabled=False)
        self.reportstack = reportstack

    def _open_install_log(self, logfile):
        """Open the install log."""
        if not logfile:
            return None
        try:
            return open(logfile, 'ab')
        except IOError:
            return None

    def _write_stdout3(self, data):
        sys.stdout.buffer.write(data)  # pylint: disable=no-member
        sys.stdout.flush()

    def _write_stdout2(self, data):
        sys.stdout.write(data)
        sys.stdout.flush()

    def write(self, data):
        """Write data to stdout and to the install_log."""
        self.write_stdout(data)
        if self.install_log is not None:
            self.install_log.write(data)
            self.install_log.flush()

    def run(self):
        for cmdname in sorted(self.commands.keys()):
            cmd = self.commands[cmdname]
            if not cmd:
                continue
            cur_res = events.ReportEventStack(
                name=cmdname, description="running '%s'" % ' '.join(cmd),
                parent=self.reportstack, level="DEBUG")

            env = self.env.copy()
            env['CURTIN_REPORTSTACK'] = cur_res.fullname

            shell = not isinstance(cmd, list)
            with util.LogTimer(LOG.debug, cmdname):
                with cur_res:
                    try:
                        sp = subprocess.Popen(
                            cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            env=env, shell=shell)
                    except OSError as e:
                        LOG.warn("%s command failed", cmdname)
                        raise util.ProcessExecutionError(cmd=cmd, reason=e)

                    output = b""
                    while True:
                        data = sp.stdout.read(1)
                        if not data and sp.poll() is not None:
                            break
                        self.write(data)
                        output += data

                    rc = sp.returncode
                    if rc != 0:
                        LOG.warn("%s command failed", cmdname)
                        raise util.ProcessExecutionError(
                            stdout=output, stderr="",
                            exit_code=rc, cmd=cmd)


def apply_power_state(pstate):
    """
    power_state:
     delay: 5
     mode: poweroff
     message: Bye Bye
    """
    cmd = load_power_state(pstate)
    if not cmd:
        return

    LOG.info("powering off with %s", cmd)
    fid = os.fork()
    if fid == 0:
        try:
            util.subp(cmd)
            os._exit(0)
        except Exception as e:
            LOG.warn("%s returned non-zero: %s" % (cmd, e))
            os._exit(1)
    return


def load_power_state(pstate):
    """Returns a command to reboot the system if power_state should."""
    if pstate is None:
        return None

    if not isinstance(pstate, dict):
        raise TypeError("power_state is not a dict.")

    opt_map = {'halt': '-H', 'poweroff': '-P', 'reboot': '-r'}

    mode = pstate.get("mode")
    if mode not in opt_map:
        raise TypeError("power_state[mode] required, must be one of: %s." %
                        ','.join(opt_map.keys()))

    delay = pstate.get("delay", "5")
    if delay == "now":
        delay = "0"
    elif re.match(r"\+[0-9]+", str(delay)):
        delay = "%sm" % delay[1:]
    else:
        delay = str(delay)

    args = ["shutdown", opt_map[mode], "now"]
    if pstate.get("message"):
        args.append(pstate.get("message"))

    shcmd = ('sleep "$1" && shift; '
             '[ -f /run/block-curtin-poweroff ] && exit 0; '
             'exec "$@"')

    return (['sh', '-c', shcmd, 'curtin-poweroff', delay] + args)


def apply_kexec(kexec, target):
    """
    load kexec kernel from target dir, similar to /etc/init.d/kexec-load
    kexec:
     mode: on
    """
    grubcfg = "boot/grub/grub.cfg"
    target_grubcfg = os.path.join(target, grubcfg)

    if kexec is None or kexec.get("mode") != "on":
        return False

    if not isinstance(kexec, dict):
        raise TypeError("kexec is not a dict.")

    if not util.which('kexec'):
        distro.install_packages('kexec-tools')

    if not os.path.isfile(target_grubcfg):
        raise ValueError("%s does not exist in target" % grubcfg)

    with open(target_grubcfg, "r") as fp:
        default = 0
        menu_lines = []

        # get the default grub boot entry number and menu entry line numbers
        for line_num, line in enumerate(fp, 1):
            if re.search(r"\bset default=\"[0-9]+\"\b", " %s " % line):
                default = int(re.sub(r"[^0-9]", '', line))
            if re.search(r"\bmenuentry\b", " %s " % line):
                menu_lines.append(line_num)

        if not menu_lines:
            LOG.error("grub config file does not have a menuentry\n")
            return False

        # get the begin and end line numbers for default menuentry section,
        # using end of file if it's the last menuentry section
        begin = menu_lines[default]
        if begin != menu_lines[-1]:
            end = menu_lines[default + 1] - 1
        else:
            end = line_num

        fp.seek(0)
        lines = fp.readlines()
        kernel = append = initrd = ""

        for i in range(begin, end):
            if 'linux' in lines[i].split():
                split_line = shlex.split(lines[i])
                kernel = os.path.join(target, split_line[1])
                append = "--append=" + ' '.join(split_line[2:])
            if 'initrd' in lines[i].split():
                split_line = shlex.split(lines[i])
                initrd = "--initrd=" + os.path.join(target, split_line[1])

        if not kernel:
            LOG.error("grub config file does not have a kernel\n")
            return False

        LOG.debug("kexec -l %s %s %s" % (kernel, append, initrd))
        util.subp(args=['kexec', '-l', kernel, append, initrd])
        return True


def migrate_proxy_settings(cfg):
    """Move the legacy proxy setting 'http_proxy' into cfg['proxy']."""
    proxy = cfg.get('proxy', {})
    if not isinstance(proxy, dict):
        raise ValueError("'proxy' in config is not a dictionary: %s" % proxy)

    if 'http_proxy' in cfg:
        hp = cfg['http_proxy']
        if hp:
            if proxy.get('http_proxy', hp) != hp:
                LOG.warn("legacy http_proxy setting (%s) differs from "
                         "proxy/http_proxy (%s), using %s",
                         hp, proxy['http_proxy'], proxy['http_proxy'])
            else:
                LOG.debug("legacy 'http_proxy' migrated to proxy/http_proxy")
                proxy['http_proxy'] = hp
        del cfg['http_proxy']

    cfg['proxy'] = proxy


@logged_time("INSTALL_COMMAND")
def cmd_install(args):
    from .collect_logs import create_log_tarfile
    cfg = deepcopy(CONFIG_BUILTIN)
    config.merge_config(cfg, args.config)

    for source in args.source:
        src = util.sanitize_source(source)
        cfg['sources']["%02d_cmdline" % len(cfg['sources'])] = src

    LOG.info(INSTALL_START_MSG)
    LOG.debug('LANG=%s', os.environ.get('LANG'))
    LOG.debug("merged config: %s" % cfg)
    if not len(cfg.get('sources', [])):
        raise util.BadUsage("no sources provided to install")

    migrate_proxy_settings(cfg)
    for k in ('http_proxy', 'https_proxy', 'no_proxy'):
        if k in cfg['proxy']:
            os.environ[k] = cfg['proxy'][k]

    instcfg = cfg.get('install', {})
    logfile = instcfg.get('log_file')
    error_tarfile = instcfg.get('error_tarfile')
    post_files = instcfg.get('post_files', [logfile])

    # Generate curtin configuration dump and add to write_files unless
    # installation config disables dump
    yaml_dump_file = instcfg.get('save_install_config', SAVE_INSTALL_CONFIG)
    if yaml_dump_file:
        write_files = cfg.get('write_files', {})
        write_files['curtin_install_cfg'] = {
            'path': yaml_dump_file,
            'permissions': '0400',
            'owner': 'root:root',
            'content': config.dump_config(cfg)
        }
        cfg['write_files'] = write_files

    # Load reporter
    clear_install_log(logfile)
    legacy_reporter = load_reporter(cfg)
    legacy_reporter.files = post_files

    writeline_and_stdout(logfile, INSTALL_START_MSG)
    args.reportstack.post_files = post_files
    workingd = None
    try:
        workingd = WorkingDir(cfg)
        dd_images = util.get_dd_images(cfg.get('sources', {}))
        if len(dd_images) > 1:
            raise ValueError("You may not use more than one disk image")

        LOG.debug(workingd.env())
        env = os.environ.copy()
        env.update(workingd.env())

        for name in cfg.get('stages'):
            desc = STAGE_DESCRIPTIONS.get(name, "stage %s" % name)
            reportstack = events.ReportEventStack(
                "stage-%s" % name, description=desc,
                parent=args.reportstack)
            env['CURTIN_REPORTSTACK'] = reportstack.fullname

            with reportstack:
                commands_name = '%s_commands' % name
                with util.LogTimer(LOG.debug, 'stage_%s' % name):
                    stage = Stage(name, cfg.get(commands_name, {}), env,
                                  reportstack=reportstack, logfile=logfile)
                    stage.run()

        if apply_kexec(cfg.get('kexec'), workingd.target):
            cfg['power_state'] = {'mode': 'reboot', 'delay': 'now',
                                  'message': "'rebooting with kexec'"}

        writeline_and_stdout(logfile, INSTALL_PASS_MSG)
        legacy_reporter.report_success()
    except Exception as e:
        exp_msg = INSTALL_FAIL_MSG.format(exception=e)
        writeline(logfile, exp_msg)
        LOG.error(exp_msg)
        legacy_reporter.report_failure(exp_msg)
        if error_tarfile:
            create_log_tarfile(error_tarfile, cfg)
        raise e
    finally:
        log_target_path = instcfg.get('save_install_log', SAVE_INSTALL_LOG)
        if log_target_path and workingd:
            copy_install_log(logfile, workingd.target, log_target_path)

        if instcfg.get('unmount', "") == "disabled":
            LOG.info('Skipping unmount: config disabled target unmounting')
        elif workingd:
            # unmount everything (including iscsi disks)
            util.do_umount(workingd.target, recursive=True)

            # The open-iscsi service in the ephemeral environment handles
            # disconnecting active sessions.  On Artful release the systemd
            # unit file has conditionals that are not met at boot time and
            # results in open-iscsi service not being started; This breaks
            # shutdown on Artful releases.
            # Additionally, in release < Artful, if the storage configuration
            # is layered, like RAID over iscsi volumes, then disconnecting
            # iscsi sessions before stopping the raid device hangs.
            # As it turns out, letting the open-iscsi service take down the
            # session last is the cleanest way to handle all releases
            # regardless of what may be layered on top of the iscsi disks.
            #
            # Check if storage configuration has iscsi volumes and if so ensure
            # iscsi service is active before exiting install
            if iscsi.get_iscsi_disks_from_config(cfg):
                iscsi.restart_iscsi_service()

            for pool in zfs.get_zpool_from_config(cfg):
                LOG.debug('Exporting ZFS zpool %s', pool)
                zfs.zpool_export(pool)

            shutil.rmtree(workingd.top)

    apply_power_state(cfg.get('power_state'))

    sys.exit(0)


# we explicitly accept config on install for backwards compatibility
CMD_ARGUMENTS = (
    ((('-c', '--config'),
      {'help': 'read configuration from cfg', 'action': util.MergedCmdAppend,
       'metavar': 'FILE', 'type': argparse.FileType("rb"),
       'dest': 'cfgopts', 'default': []}),
     ('--set', {'action': util.MergedCmdAppend,
                'help': ('define a config variable. key can be a "/" '
                         'delimited path ("early_commands/cmd1=a"). if '
                         'key starts with "json:" then val is loaded as '
                         'json (json:stages="[\'early\']")'),
                'metavar': 'key=val', 'dest': 'cfgopts'}),
     ('source', {'help': 'what to install', 'nargs': '*'}),
     )
)


def POPULATE_SUBCMD(parser):
    populate_one_subcmd(parser, CMD_ARGUMENTS, cmd_install)

# vi: ts=4 expandtab syntax=python
