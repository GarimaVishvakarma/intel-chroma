#
# INTEL CONFIDENTIAL
#
# Copyright 2013 Intel Corporation All Rights Reserved.
#
# The source code contained or described herein and all documents related
# to the source code ("Material") are owned by Intel Corporation or its
# suppliers or licensors. Title to the Material remains with Intel Corporation
# or its suppliers and licensors. The Material contains trade secrets and
# proprietary and confidential information of Intel or its suppliers and
# licensors. The Material is protected by worldwide copyright and trade secret
# laws and treaty provisions. No part of the Material may be used, copied,
# reproduced, modified, published, uploaded, posted, transmitted, distributed,
# or disclosed in any way without Intel's prior express written permission.
#
# No license under any patent, copyright, trade secret or other intellectual
# property right is granted to or conferred upon you by disclosure or delivery
# of the Materials, either expressly, by implication, inducement, estoppel or
# otherwise. Any license under such intellectual property rights must be
# express and approved by Intel in writing.


import traceback
import threading
import Queue

from chroma_core.services.log import log_register
from chroma_core.models import PowerControlDevice, PowerControlDeviceUnavailableAlert


log = log_register(__name__.split('.')[-1])


class PowerDeviceMonitor(threading.Thread):
    """
    Instances of this class do double-duty: Their primary mission in life is
    to watch an assigned PDU and raise Alerts if the PDU becomes unmonitorable.
    As a secondary duty, they handle asynchronous tasks for the manager:
    slowish, fiddly things like querying a PDU's outlet states, etc.
    """
    def __init__(self, device, power_control_manager):
        super(PowerDeviceMonitor, self).__init__()
        self.device = device
        self._manager = power_control_manager
        self._stopping = threading.Event()

    def run(self):
        try:
            self._run()
        finally:
            # Make sure we always clean up.
            # HYD-1918: Refactor this kludgy mess so that these threads don't
            # get DB access and therefore don't need to clean up.
            import django.db
            if django.db.connection.connection:
                django.db.connection.close()

    def _run(self):
        log.info("Starting monitor for %s" % self.device)

        while not self._stopping.is_set():
            # Check to see if the manager has scheduled something for
            # us to do besides monitoring.
            try:
                log.debug("Checking for tasks for %s:%s" % self.device.sockaddr)
                task, kwargs = self._manager.get_monitor_tasks(self.device.sockaddr).get_nowait()
                log.debug("Found task for %s:%s: %s" % (self.device.sockaddr + tuple([task])))
                if task == "stop":
                    self.stop()
                else:
                    getattr(self._manager, task)(**kwargs)
                log.debug("Ran %s for %s:%s" % (tuple([task]) + self.device.sockaddr))
            except Queue.Empty:
                pass
            except PowerControlDevice.DoesNotExist:
                log.error("Attempted to run %s on %s, but it no longer exists" % (task, self.device))
                self.stop()
            except Exception, e:
                log.error("Caught and re-raising exception: %s" % traceback.format_exc())
                raise e

            # Check to see if we can log into the PDU and that it's
            # responsive to commands.
            available = self._manager.check_device_availability(self.device)
            if available or PowerControlDevice.objects.filter(id=self.device.id, not_deleted=True).exists():
                PowerControlDeviceUnavailableAlert.notify(self.device, not available)
            log.debug("Checked on %s:%s: %s" % (self.device.sockaddr + tuple(["available" if available else "unavailable"])))
            self._stopping.wait(timeout = 10)

    def stop(self):
        log.info("Stopping monitor for %s" % self.device)
        self._stopping.set()


class PowerMonitorDaemon(object):
    def __init__(self, power_control_manager):
        self._manager = power_control_manager
        self._stopping = threading.Event()

        self.device_monitors = {}

        for sockaddr, device in self._manager.power_devices.items():
            self.device_monitors[sockaddr] = PowerDeviceMonitor(device,
                                                                self._manager)

        log.info("Found %d power devices to monitor" % len(self.device_monitors))

    def run(self):
        log.info("entering main loop")

        for monitor in self.device_monitors.values():
            monitor.start()

        while not self._stopping.is_set():
            # Check for new devices to monitor, or dead threads. A thread
            # may suicide if the manager has enqueued a 'stop' task.
            for sockaddr, device in self._manager.power_devices.items():
                if (sockaddr in self.device_monitors
                        and not self.device_monitors[sockaddr].is_alive()):
                    log.warn("Monitor for %s:%s died, restarting" % sockaddr)
                elif not sockaddr in self.device_monitors:
                    log.info("Found new power device: %s:%s" % sockaddr)
                else:
                    continue
                monitor = PowerDeviceMonitor(device, self._manager)
                self.device_monitors[sockaddr] = monitor
                monitor.start()

            # Check for old devices to stop monitoring
            for sockaddr, monitor in self.device_monitors.items():
                if sockaddr not in self._manager.power_devices:
                    log.info("Reaping monitor for old power device: %s:%s" % sockaddr)
                    monitor.stop()
                    monitor.join()
                    del self.device_monitors[sockaddr]

            self._stopping.wait(timeout = 10)

        for monitor in self.device_monitors.values():
            monitor.stop()

        log.info("leaving main loop")

    def stop(self):
        log.info("Stopping...")
        self._stopping.set()

    def join(self):
        log.info("Joining...")
        for monitor in self.device_monitors.values():
            monitor.join()
