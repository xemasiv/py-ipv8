from ....deprecated.community import _DEFAULT_ADDRESSES
from ....deprecated.payload_headers import BinMemberAuthenticationPayload, GlobalTimeDistributionPayload
from ...base import TestBase
from ...mocking.community import MockCommunity
from ....peerdiscovery.deprecated.discovery_payload import DiscoveryIntroductionRequestPayload
from ...util import twisted_wrapper


class TestDiscoveryCommunity(TestBase):

    def setUp(self):
        while _DEFAULT_ADDRESSES:
            _DEFAULT_ADDRESSES.pop()
        self.tracker = MockCommunity()
        _DEFAULT_ADDRESSES.append(self.tracker.endpoint.wan_address)

        node_count = 2
        self.overlays = [MockCommunity() for _ in range(node_count)]

    def tearDown(self):
        self.tracker.unload()
        for overlay in self.overlays:
            overlay.unload()

    @twisted_wrapper
    def test_deprecated_introduction(self):
        """
        Check if we can handle the deprecated Discovery introduction request as a normal one.
        """
        global_time = self.overlays[0].claim_global_time()
        payload = DiscoveryIntroductionRequestPayload("a" * 20,
                                                      self.overlays[1].endpoint.wan_address,
                                                      self.overlays[0].my_estimated_lan,
                                                      self.overlays[0].my_estimated_wan,
                                                      True,
                                                      u"unknown",
                                                      False,
                                                      global_time).to_pack_list()
        auth = BinMemberAuthenticationPayload(self.overlays[0].my_peer.public_key.key_to_bin()).to_pack_list()
        dist = GlobalTimeDistributionPayload(global_time).to_pack_list()

        packet = self.overlays[0]._ez_pack(self.overlays[0]._prefix, 246, [auth, dist, payload])
        self.overlays[1].on_introduction_request(self.overlays[0].endpoint.wan_address, packet)

        yield self.deliver_messages()

        self.assertEqual(1, len(self.overlays[1].network.verified_peers))

    @twisted_wrapper
    def test_bootstrap(self):
        """
        Check if we can bootstrap our peerdiscovery.
        """
        # Both other overlays contact the tracker
        self.overlays[0].bootstrap()
        self.overlays[1].bootstrap()
        yield self.deliver_messages()

        self.assertEqual(len(self.tracker.network.verified_peers), 2)

        # Now that the tracker knows both others, they should be introduced to each other
        self.overlays[0].bootstrap()
        self.overlays[1].bootstrap()
        yield self.deliver_messages()

        for overlay in self.overlays:
            intros = overlay.network.get_introductions_from(self.tracker.my_peer)
            self.assertEqual(len(intros), 1)
            self.assertNotIn(overlay.my_peer.mid, intros)
            self.assertNotIn(self.tracker.my_peer.mid, intros)
