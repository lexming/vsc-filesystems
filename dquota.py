#!/usr/bin/env python
##
#
# Copyright 2012-2013 Ghent University
#
# This file is part of the tools originally by the HPC team of
# Ghent University (http://ugent.be/hpc).
#
# All rights reserved.
#
"""Script to check for quota transgressions and notify the offending users.

- relies on mmrepquota to get a quick estimate of user quota
- checks all known GPFS mounted file systems

Created Mar 8, 2012

@author Andy Georges
"""

import copy
import os
import pwd
import sys
import time

## FIXME: deprecated in >= 2.7
from lockfile import LockFailed

from vsc.exceptions import VscError
from vsc.filesystem.gpfs import GpfsOperations
from vsc.gpfs.quota.entities import QuotaUser, QuotaFileset
from vsc.gpfs.quota.fs_store import UserFsQuotaStorage, VoFsQuotaStorage
from vsc.gpfs.quota.report import GpfsQuotaMailReporter
from vsc.gpfs.utils.exceptions import CriticalException
from vsc.ldap.configuration import VscConfiguration
from vsc.ldap.utils import LdapQuery
from vsc.utils import fancylogger
from vsc.utils.availability import proceed_on_ha_service
from vsc.utils.generaloption import simple_option
from vsc.utils.lock import lock_or_bork, release_or_bork
from vsc.utils.nagios import NagiosReporter, NagiosResult, NAGIOS_EXIT_OK, NAGIOS_EXIT_WARNING, NAGIOS_EXIT_CRITICAL
from vsc.utils.timestamp_pid_lockfile import TimestampedPidLockfile

## Constants
NAGIOS_CHECK_FILENAME = '/var/log/pickles/gpfs_quota_checker.nagios.pickle'
NAGIOS_HEADER = 'quota_check'
NAGIOS_CHECK_INTERVAL_THRESHOLD = 30 * 60  # 30 minutes

QUOTA_CHECK_LOG_FILE = '/var/log/quota/gpfs_quota_checker.log'
QUOTA_CHECK_REMINDER_CACHE_FILENAME = '/var/log/quota/gpfs_quota_checker.report.reminderCache.pickle'
QUOTA_CHECK_LOCK_FILE = '/var/run/gpfs_quota_checker_tpid.lock'

VSC_INSTALL_USER_NAME = 'vsc40003'


# log setup
fancylogger.logToFile(QUOTA_CHECK_LOG_FILE)
fancylogger.logToScreen(False)
fancylogger.setLogLevelInfo()
logger = fancylogger.getLogger('gpfs_quota_checker')


def get_mmrepquota_maps():
    """Obtain the quota information and rearrange it according to users and filesets.

    This function uses vsc.filesystem.gpfs.GpfsOperations to obtain
    quota information for all filesystems known to the storage.

    The returned dictionaries contain all information on a per user
    and per fileset basis across all filesystems. Users with multiple
    quota settings across different filesets are processed correctly.

    Returns (user dictionary, fileset dictionary).
    """
    user_map = {}
    fs_map = {}

    gpfs_operations = GpfsOperations()
    devices = gpfs_operations.list_filesystems().keys()
    logger.debug("Found the following GPFS filesystems: %s" % (devices))

    filesets = gpfs_operations.list_filesets()
    logger.debug("Found the following GPFS filesets: %s" % (filesets))

    quota_map = gpfs_operations.list_quota(devices)

    timestamp = int(time.time())

    for device in devices:

        # Iterate over a list of named tuples -- GpfsQuota
        for gpfs_quota in quota_map[device]['USR'].values():
            user_quota = user_map.get(gpfs_quota.name, QuotaUser(gpfs_quota.name))
            fileset_name = filesets[device][gpfs_quota.filesetname]['filesetName']
            user_map[gpfs_quota.name] = _update_quota_entity(user_quota,
                                                             device,
                                                             fileset_name,
                                                             gpfs_quota,
                                                             timestamp)

        # Iterate over a list of named tuples -- GpfsQuota
        for gpfs_quota in quota_map[device]['FILESET'].values():
            fileset_quota = fs_map.get(gpfs_quota.name, QuotaFileset(gpfs_quota.name))
            fileset_name = filesets[device][gpfs_quota.filesetname]['filesetName']
            fs_map[gpfs_quota.name] = _update_quota_entity(fileset_quota,
                                                           device,
                                                           fileset_name,
                                                           gpfs_quota,
                                                           timestamp)

    return (user_map, fs_map)



GPFS_GRACE_REGEX = re.compile(r"(?P<days>\d+)days|(?P<hours>\d+hours)|(?P<expired>expired)")

def _update_quota_entity(entity, filesystem, fileset, gpfs_quota, timestamp):
    """
    Update the quota information for an entity (user or fileset).

    @type entity: QuotaEntity instance
    @type filesystem: string
    @type fileset: string
    @type gpfs_quota: GpfsQuota namedtuple instance
    """
    grace = GPFS_GRACE_REGEX.search(gpfs_quota.grace).groupdict()
    if grace.get('days', None):
        expired = (True, grace['days'] * 86400)
    elif grace.get('hours', None):
        expired = (True, grace['hours'] * 3600)
    elif grace.get('expired', None):
        expired = (True, 0)
    else:
        expired = (False, None)

    entity.update(device,
                  fileset_name,
                  quota.blockUsage,
                  quota.blockSoft,
                  quota.blockHard,
                  quota.blockDoubt,
                  grace,
                  timestamp)

    return entity


def nagios_analyse_data(ex_users, ex_vos, user_count, vo_count):
    """Analyse the data blobs we gathered and build a summary for nagios.

    @type ex_users: [ quota.entities.User ]
    @type ex_vos: [ quota.entities.VO ]
    @type user_count: int
    @type vo_count: int

    Returns a tuple with two elements:
        - the exit code to be provided when the script runs as a nagios check
        - the message to be printed when the script runs as a nagios check
    """
    ex_u = len(ex_users)
    ex_v = len(ex_vos)
    if ex_u == 0 and ex_v == 0:
        return (NAGIOS_EXIT_OK, NagiosResult("No quota exceeded", ex_u=0, ex_v=0, pU=0, pV=0))
    else:
        pU = float(ex_u) / user_count
        pV = float(ex_v) / vo_count
        return (NAGIOS_EXIT_OK, NagiosResult("Quota exceeded", ex_u=ex_u, ex_v=ex_v, pU=pU, pV=pV))


def map_uids_to_names():
    """Determine the mapping between user ids and user names."""
    ul = pwd.getpwall()
    d = {}
    for u in ul:
        d[u[2]] = u[0]
    return d


def main():

    options = {
        'nagios': ('print out nagios information', None, 'store_true', False, 'n'),
        'nagios-check-filename': ('filename of where the nagios check data is stored', str, 'store', NAGIOS_CHECK_FILENAME),
        'nagios-check-interval-threshold': ('threshold of nagios checks timing out', None, 'store', NAGIOS_CHECK_INTERVAL_THRESHOLD),
        'storage': ('the VSC filesystems that are checked by this script', None, 'store', None),
        'dry-run': ('do not make any updates whatsoever', None, 'store_true', False),
    }
    opts = simple_option(options)

    logger.info('started GPFS quota check run.')

    nagios_reporter = NagiosReporter(NAGIOS_HEADER,
                                     opts.options.nagios_check_filename,
                                     opts.options.nagios_check_interval_threshold)

    if opts.options.nagios:
        nagios_reporter.report_and_exit()
        sys.exit(0)  # not reached

    lockfile = TimestampedPidLockfile(QUOTA_CHECK_LOCK_FILE)
    lock_or_bork(lockfile, nagios_reporter)

    try:
        user_id_map = map_uids_to_names() # is this really necessary?
        (mm_rep_quota_map_users, mm_rep_quota_map_filesets) = get_mmrepquota_maps()

        mm_rep_quota_map_vos = dict((id, q) for (id, q) in mm_rep_quota_map_filesets.items() if id.startswith('gvo'))

        if not mm_rep_quota_map_users or not mm_rep_quota_map_vos:
            raise CriticalException('no usable data was found in the mmrepquota output')

        # figure out which users are crossing their softlimits
        ex_users = filter(lambda u: u.exceeds(), mm_rep_quota_map_users.values())
        logger.warning("found %s users who are exceeding their quota: %s" % (len(ex_users), [u.user_id for u in ex_users]))

        # figure out which VO's are exceeding their softlimits
        # currently, we're not using this, VO's should have plenty of space
        ex_vos = filter(lambda v: v.exceeds(), mm_rep_quota_map_vos.values())
        logger.warning("found %s VOs who are exceeding their quota: %s" % (len(ex_vos), [v.fileset_id for v in ex_vos]))

        # force mounting the home directories for the ghent users
        # FIXME: this works for the current setup, might be an issue if we change things.
        #        see ticket #987
        vsc_install_user_home = None
        try:
            vsc_install_user_home = pwd.getpwnam(VSC_INSTALL_USER_NAME)[5]
            cmd = "sudo -u %s stat %s" % (VSC_INSTALL_USER_NAME, vsc_install_user_home)
            os.system(cmd)
        except Exception, err:
            raise CriticalException('Cannot stat the VSC install user (%s) home at (%s).' % (VSC_INSTALL_USER_NAME, vsc_install_user_home))

        # FIXME: cache the storage quota information (test for exceeding users)
        u_storage = UserFsQuotaStorage()
        for user in mm_rep_quota_map_users.values():
            try:
                if not opts.options.dry_run:
                    u_storage.store_quota(user)
            except VscError, err:
                logger.error("Could not store data for user %s" % (user.user_id))
                pass  # we're just moving on, trying the rest of the users. The error will have been logged anyway.

        v_storage = VoFsQuotaStorage()
        for vo in mm_rep_quota_map_vos.values():
            try:
                if not opts.options.dry_run:
                    v_storage.store_quota(vo)
            except VscError, err:
                log.error("Could not store vo data for vo %s" % (vo.fileset_id))
                pass  # we're just moving on, trying the rest of the VOs. The error will have been logged anyway.

        if not opts.options.dry_run:
            # Report to the users who are exceeding their quota
            LdapQuery(VscConfiguration())  # Initialise here, the mailreporter will use it.
            reporter = GpfsQuotaMailReporter(QUOTA_CHECK_REMINDER_CACHE_FILENAME)
            for user in ex_users:
                reporter.report_user(user)
            log.info("Done reporting users.")
            reporter.close()

    except CriticalException, err:
        log.critical("critical exception caught: %s" % (err.message))
        if not opts.options.dry_run:
            nagios_reporter.cache(NAGIOS_EXIT_CRITICAL, NagiosResult("CRITICAL script failed - %s" % (err.message)))
        if not opts.options.dry_run:
            lockfile.release()
        sys.exit(1)
    except Exception, err:
        log.critical("exception caught: %s" % (err))
        if not opts.options.dry_run:
            lockfile.release()
        sys.exit(1)

    (nagios_exit_code, nagios_result) = nagios_analyse_data(ex_users,
                                                            ex_vos,
                                                            user_count=len(mm_rep_quota_map_users.values()),
                                                            vo_count=len(mm_rep_quota_map_vos.values()))

    bork_result = copy.deepcopy(nagios_result)
    bork_result.message = "lock release failed"
    release_or_bork(lockfile, nagios_reporter, bork_result)

    nagios_reporter.cache(nagios_exit_code, "%s" % (nagios_result,))
    log.info("Nagios exit: (%s, %s)" % (nagios_exit_code, nagios_result))

if __name__ == '__main__':
    main()
