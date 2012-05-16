import datetime
import logging
from django.test import TestCase
import mock
from chroma_core.models.jobs import Command
from chroma_core.models import Volume, VolumeNode


def freshen(obj):
    return obj.__class__.objects.get(pk=obj.pk)


class MockAgent(object):
    label_counter = 0
    mock_servers = {}
    calls = []

    @classmethod
    def clear_calls(cls):
        cls.calls = []

    @classmethod
    def last_call(cls):
        return cls.calls[-1]

    succeed = True

    def __init__(self, host, log = None, console_callback = None, timeout = None):
        self.host = host

    def invoke(self, cmdline, args = None):
        self.calls.append((cmdline, args))
        if not self.succeed:
            raise RuntimeError("Test-generated failure")

        logging.getLogger('mock_agent').info("invoke_agent %s %s %s" % (self.host, cmdline, args))
        if cmdline == "get-fqdn":
            return self.mock_servers[self.host.address]['fqdn']
        if cmdline == "get-nodename":
            return self.mock_servers[self.host.address]['nodename']
        elif cmdline == "lnet-scan":
            return self.mock_servers[self.host.address]['nids']
        elif cmdline == 'get-time':
            return datetime.datetime.utcnow().isoformat() + "Z"
        elif cmdline.startswith("format-target"):
            import uuid
            return {'uuid': uuid.uuid1().__str__(), 'inode_count': 666, 'inode_size': 777}
        elif cmdline.startswith('start-target'):
            import re
            from chroma_core.models import ManagedTarget
            ha_label = re.search("--ha_label ([^\s]+)", cmdline).group(1)
            target = ManagedTarget.objects.get(ha_label = ha_label)
            return {'location': target.primary_server().nodename}
        elif cmdline.startswith('register-target'):
            MockAgent.label_counter += 1
            return {'label': "foofs-TTT%04d" % self.label_counter}


class MockDaemonRpc():
    def start_session(self, resource_id):
        return

    def remove_resource(self, resource_id):
        return


class JobTestCase(TestCase):
    mock_servers = None
    hosts = None

    def _test_lun(self, primary_host, *args):
        volume = Volume.objects.create()
        VolumeNode.objects.create(volume = volume, host = primary_host, path = "/fake/path/%s" % volume.id, primary = True)
        for host in args:
            VolumeNode.objects.create(volume = volume, host = host, path = "/fake/path/%s" % volume.id, primary = False)

        return volume

    def set_state(self, obj, state, check = True):
        Command.set_state([(obj, state)], "Unit test transition %s to %s" % (obj, state))
        if check:
            try:
                self.assertState(obj, state)
            except obj.__class__.DoesNotExist:
                pass

    def assertState(self, obj, state):
        self.assertEqual(freshen(obj).state, state)

    def setUp(self):
        # FIXME: have to do this before every test because otherwise
        # one test will get all the setup of StoragePluginClass records,
        # the in-memory instance of storage_plugin_manager will expect
        # them to still be there but they'll have been cleaned
        # out of the database.  Setting up this stuff should be done
        # as part of the initial DB setup before any test is started
        # so that it's part of the baseline that's rolled back to
        # after each test.
        import chroma_core.lib.storage_plugin.manager
        chroma_core.lib.storage_plugin.manager.storage_plugin_manager = chroma_core.lib.storage_plugin.manager.StoragePluginManager()

        # NB by this stage celery has already read in its settings, so we have to update
        # ALWAYS_EAGER inside celery instead of in settings.*
        from celery.app import app_or_default
        self.old_celery_always_eager = app_or_default().conf.CELERY_ALWAYS_EAGER
        app_or_default().conf.CELERY_ALWAYS_EAGER = True
        self.old_celery_eager_propagates_exceptions = app_or_default().conf.CELERY_EAGER_PROPAGATES_EXCEPTIONS
        app_or_default().conf.CELERY_EAGER_PROPAGATES_EXCEPTIONS = True

        # Intercept attempts to call out to lustre servers
        import chroma_core.lib.agent
        self.old_agent = chroma_core.lib.agent.Agent
        MockAgent.mock_servers = self.mock_servers
        chroma_core.lib.agent.Agent = MockAgent

        # Override DaemonRPC
        import chroma_core.lib.storage_plugin.daemon
        self.old_daemon_rpc = chroma_core.lib.storage_plugin.daemon.DaemonRpc
        chroma_core.lib.storage_plugin.daemon.DaemonRpc = MockDaemonRpc

        # Override PluginRequest/PluginResponse
        import chroma_core.lib.storage_plugin.messaging
        self.old_plugin_request = chroma_core.lib.storage_plugin.messaging.PluginRequest
        self.old_plugin_response = chroma_core.lib.storage_plugin.messaging.PluginResponse
        chroma_core.lib.storage_plugin.messaging.PluginRequest = mock.Mock()
        chroma_core.lib.storage_plugin.messaging.PluginResponse = mock.Mock()

        # Override LearnDevicesStep.run so that we don't require storage plugin RPC
        from chroma_core.models.host import LearnDevicesStep
        LearnDevicesStep.run = mock.Mock()
        from chroma_core.lib.storage_plugin.daemon import AgentDaemonRpc

        def fake_remove_host_resources(host_id):
            from chroma_core.models.host import Volume, VolumeNode
            for vn in VolumeNode.objects.filter(host__id = host_id):
                VolumeNode.delete(vn.id)
            for volume in Volume.objects.all():
                if volume.volumenode_set.count() == 0:
                    Volume.delete(volume.id)

        AgentDaemonRpc.remove_host_resources = mock.Mock(side_effect = fake_remove_host_resources)

    def tearDown(self):
        import chroma_core.lib.agent
        chroma_core.lib.agent.Agent = self.old_agent

        from celery.app import app_or_default
        app_or_default().conf.CELERY_ALWAYS_EAGER = self.old_celery_always_eager
        app_or_default().conf.CELERY_ALWAYS_EAGER = self.old_celery_eager_propagates_exceptions

        import chroma_core.lib.storage_plugin.daemon
        chroma_core.lib.storage_plugin.daemon.DaemonRpc = self.old_daemon_rpc

        import chroma_core.lib.storage_plugin.messaging
        chroma_core.lib.storage_plugin.messaging.PluginRequest = self.old_plugin_request
        chroma_core.lib.storage_plugin.messaging.PluginResponse = self.old_plugin_response


class JobTestCaseWithHost(JobTestCase):
    mock_servers = {
            'myaddress': {
                'fqdn': 'myaddress.mycompany.com',
                'nodename': 'test01.myaddress.mycompany.com',
                'nids': ["192.168.0.1@tcp"]
            }
    }

    def setUp(self):
        super(JobTestCaseWithHost, self).setUp()

        from chroma_core.models import ManagedHost
        self.hosts = [ManagedHost.create_from_string(address)[0] for address, info in self.mock_servers.items()]

        # Handy if you're only using one
        self.host = self.hosts[0]
        self.assertEqual(ManagedHost.objects.get(pk = self.host.pk).state, 'lnet_up')
        self.assertEqual(ManagedHost.objects.get(pk = self.host.pk).lnetconfiguration.state, 'nids_known')

    def create_simple_filesystem(self, start = True):
        from chroma_core.models import ManagedMgs, ManagedMdt, ManagedOst, ManagedFilesystem
        self.mgt = ManagedMgs.create_for_volume(self._test_lun(self.host).id, name = "MGS")
        self.fs = ManagedFilesystem.objects.create(mgs = self.mgt, name = "testfs")
        self.mdt = ManagedMdt.create_for_volume(self._test_lun(self.host).id, filesystem = self.fs)
        self.ost = ManagedOst.create_for_volume(self._test_lun(self.host).id, filesystem = self.fs)
        if start:
            self.set_state(self.fs, 'available')
            self.mgt = freshen(self.mgt)
            self.fs = freshen(self.fs)
            self.mdt = freshen(self.mdt)
            self.ost = freshen(self.ost)
