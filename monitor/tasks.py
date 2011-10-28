
# ==============================
# Copyright 2011 Whamcloud, Inc.
# ==============================

from celery.task import task, periodic_task
from datetime import timedelta, datetime

from monitor.lib.lustre_audit import audit_log

import settings

@task()
def monitor_exec(monitor_id, counter):
    from configure.models import Monitor
    monitor = Monitor.objects.get(pk = monitor_id)

    # Conditions indicating that we've restarted or that 
    if not (monitor.state in ['tasked', 'tasking']):
        audit_log.warn("Host %s monitor %s (audit %s) found unfinished (crash recovery).  Ending task." % (monitor.host, monitor.id, monitor.counter))
        monitor.update(state = 'idle', task_id = None)
        return 
    elif monitor.counter != counter:
        audit_log.warn("Host %s monitor found bad counter %s != %s.  Ending task." % (monitor.host,  monitor.counter, counter))
        monitor.update(state = 'idle', task_id = None)
        return 

    monitor.update(state = 'started')
    audit_log.debug("Monitor %s started" % monitor.host)
    try:
        from monitor.lib.lustre_audit import UpdateScan
        raw_data = monitor.invoke('update-scan',
                settings.AUDIT_PERIOD * 2)
        success = UpdateScan().run(monitor.host.pk, raw_data)
        if success:
            monitor.update(last_success = datetime.datetime.now())
    except Exception:
        audit_log.error("Exception auditing host %s" % monitor.host)
        import sys
        import traceback
        exc_info = sys.exc_info()
        audit_log.error('\n'.join(traceback.format_exception(*(exc_info or sys.exc_info()))))

    monitor.update(state = 'idle', task_id = None)
    audit_log.debug("Monitor %s completed" % monitor.host)
    return None

from settings import AUDIT_PERIOD
@periodic_task(run_every=timedelta(seconds=AUDIT_PERIOD))
def audit_all():
    from configure.models import ManagedHost
    for host in ManagedHost.objects.all():
        if host.monitor:
            monitor = host.monitor
        else:
            continue

        tasked = monitor.try_schedule()
        if not tasked:
            audit_log.info("audit_all: host %s audit (%d) still in progress" % (monitor.host, monitor.counter))

@periodic_task(run_every=timedelta(seconds=AUDIT_PERIOD))
def parse_log_entries():
    from monitor.lib.systemevents import SystemEventsAudit
    audit_log.info("parse_log_entries: running")
    SystemEventsAudit().parse_log_entries()

@task()
def test_host_contact(host):
    import socket
    user, hostname, port = host.ssh_params()

    try:
        addresses = socket.getaddrinfo(hostname, "22", socket.AF_INET, socket.SOCK_STREAM, socket.SOL_TCP)
        resolve = True
        resolved_address = addresses[0][4][0]
    except socket.gaierror:
        resolve = False

    ping = False
    if resolve:
        from subprocess import call
        ping = (0 == call(['ping', '-c 1', resolved_address]))

    # Don't depend on ping to try invoking agent, could well have 
    # SSH but no ping
    agent = False
    if resolve:
        result = host.monitor.invoke('update-scan', timeout = settings.AUDIT_PERIOD * 2)
        if isinstance(result, Exception):
            audit_log.error("Error trying to invoke agent on '%s': %s" % (resolved_address, result))
            agent = False
        else:
            agent = True

    return {
            'address': host.address,
            'resolve': resolve,
            'ping': ping,
            'agent': agent,
            }
