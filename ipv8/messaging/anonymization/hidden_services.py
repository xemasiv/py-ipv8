"""
The hidden tunnel community.

Author(s): Egbert Bouman
"""
from collections import defaultdict
import hashlib
import os
import struct
import time

from .caches import *
from .community import TunnelCommunity, message_to_payload
from ...deprecated.payload_headers import GlobalTimeDistributionPayload
from ...messaging.deprecated.encoding import decode, encode
from .payload import *
from ...peer import Peer
from .tunnel import CIRCUIT_ID_PORT, CIRCUIT_TYPE_IP, CIRCUIT_TYPE_RENDEZVOUS, CIRCUIT_TYPE_RP, EXIT_NODE, EXIT_NODE_SALT, Hop, RelayRoute, RendezvousPoint, TunnelExitSocket

TUNNEL_PREFIX = "ffffffff".decode("HEX")


class HiddenTunnelCommunity(TunnelCommunity):

    def __init__(self, *args, **kwargs):
        self.dht_provider = kwargs.pop('dht_provider', None)
        self.service_callbacks = kwargs.pop('service_callbacks', {})
        super(HiddenTunnelCommunity, self).__init__(*args, **kwargs)

        self.session_keys = {}

        self.my_intro_points = defaultdict(list)
        self.my_download_points = {}

        self.intro_point_for = {}
        self.rendezvous_point_for = {}
        self.infohash_rp_circuits = defaultdict(list)
        self.infohash_ip_circuits = defaultdict(list)
        self.infohash_pex = defaultdict(set)

        self.dht_blacklist = defaultdict(list)
        self.last_dht_lookup = {}

        self.hops = {}

        self.decode_map_private.update({
            chr(13): self.on_key_request,
            chr(14): self.on_key_response,
            chr(17): self.on_create_e2e,
            chr(22): self.on_dht_response,

            chr(11): self.on_establish_intro,
            chr(12): self.on_intro_established,
            chr(15): self.on_establish_rendezvous,
            chr(16): self.on_rendezvous_established,
            chr(18): self.on_created_e2e,
            chr(19): self.on_link_e2e,
            chr(20): self.on_linked_e2e,
            chr(21): self.on_dht_request
        })

    def register_service(self, service, hops, callback, create_intros=1):
        """
        Register a hidden service by assigning a callback to a service identifier.

        :param service: the service identifier
        :type service: str
        :param hops: the amount of hops for our introduction circuit
        :type hops: int
        :param callback: the callback function to call when we receive data for our service
        :param create_intros: the amount of introduction circuits to create for our service
        """
        lookup_service = self.get_lookup_info_hash(service)

        self.hops[lookup_service] = hops
        self.service_callbacks[lookup_service] = callback

        if create_intros:
            self.create_introduction_point(lookup_service, create_intros)

    def ip_to_circuit_id(self, ip_str):
        return struct.unpack("!I", socket.inet_aton(ip_str))[0]

    def circuit_id_to_ip(self, circuit_id):
        return socket.inet_ntoa(struct.pack("!I", circuit_id))

    def tunnel_data(self, circuit, destination, message_type, payload):
        message_id, payload_cls = message_to_payload[message_type]
        dist = GlobalTimeDistributionPayload(self.global_time).to_pack_list()
        payload_pack_list = payload.to_pack_list()

        packet = self._ez_pack(self._prefix, message_id, [dist, payload_pack_list], False)
        pre = ('0.0.0.0', 0)
        post = ('0.0.0.0', 0)
        if isinstance(circuit, TunnelExitSocket):
            post = destination
        else:
            pre = destination
        self.send_data([circuit.sock_addr], circuit.circuit_id, pre, post, TUNNEL_PREFIX + packet)

    def remove_circuit(self, circuit_id, additional_info='', remove_now=False, destroy=False):
        destroy_deferred = super(HiddenTunnelCommunity, self)\
            .remove_circuit(circuit_id, additional_info, remove_now, destroy)

        circuit = self.my_intro_points.pop(circuit_id, None)
        if circuit:
            self.logger.info("removed introduction point %d" % circuit_id)

        circuit = self.my_download_points.pop(circuit_id, None)
        if circuit:
            self.logger.info("removed rendezvous point %d" % circuit_id)

        return destroy_deferred

    def do_dht_lookup(self, info_hash):
        self.do_raw_dht_lookup(self.get_lookup_info_hash(info_hash))

    def do_raw_dht_lookup(self, lookup_info_hash):
        # Select a circuit from the pool of exit circuits
        self.logger.info("Do DHT request: select circuit")
        circuit = self.selection_strategy.select(None, self.hops[lookup_info_hash])
        if not circuit:
            self.logger.info("No circuit for dht-request")
            return False

        # Send a dht-request message over this circuit
        self.logger.info("Do DHT request: send dht request")
        self.last_dht_lookup[lookup_info_hash] = time.time()
        cache = self.request_cache.add(DHTRequestCache(self, circuit, lookup_info_hash))
        self.send_cell([circuit.sock_addr],
                       u"dht-request",
                       DHTRequestPayload(circuit.circuit_id, cache.number, lookup_info_hash))

    def on_dht_request(self, source_address, data, circuit_id):
        dist, payload = self._ez_unpack_noauth(DHTRequestPayload, data)
        info_hash = payload.info_hash

        def dht_callback(info_hash, peers, _):
            if not peers:
                peers = []
            circuit_id = payload.circuit_id

            if circuit_id in self.exit_sockets:
                circuit = self.exit_sockets[circuit_id]
                self.tunnel_data(circuit, source_address, u'dht-response',
                                 DHTResponsePayload(payload.circuit_id, payload.identifier,
                                                    payload.info_hash, encode(peers)))
            else:
                self.logger.info("Circuit %d is not existing anymore, can't send back dht-response" %
                                        circuit_id)

        self.logger.info("Doing dht hidden seeders lookup for info_hash %s" % info_hash.encode('HEX'))
        self.dht_lookup(info_hash, dht_callback)

    def check_dht_response(self, payload):
        if not self.is_relay(payload.circuit_id):
            request = self.request_cache.get(u"dht-request", payload.identifier)
            return request
        return True

    def on_dht_response(self, source_address, data, circuit_id=''):
        dist, payload = self._ez_unpack_noauth(DHTResponsePayload, data)

        if not self.check_dht_response(payload):
            return

        self.request_cache.pop(u"dht-request", payload.identifier)

        info_hash = payload.info_hash
        _, peers = decode(payload.peers)
        peers = set(peers)
        self.logger.info("Received dht response containing %d peers" % len(peers))

        blacklist = self.dht_blacklist[info_hash]

        # cleanup dht_blacklist
        for i in xrange(len(blacklist) - 1, -1, -1):
            if time.time() - blacklist[i][0] > 60:
                blacklist.pop(i)
        exclude = [rp[2] for rp in self.my_download_points.values()] + [sock_addr for _, sock_addr in blacklist]
        for peer in peers:
            if peer not in exclude:
                self.logger.info("Requesting key from dht peer %s", peer)
                # Blacklist this sock_addr for a period of at least 60s
                self.dht_blacklist[info_hash].append((time.time(), peer))
                self.create_key_request(info_hash, peer)

    def create_key_request(self, info_hash, sock_addr):
        # 1. Select a circuit
        self.logger.info("Create key request: select circuit")
        circuit = self.selection_strategy.select(None, self.hops[info_hash])
        if not circuit:
            self.logger.error("No circuit for key-request")
            return

        # 2. Send a key-request message
        self.logger.info("Create key request: send key request")
        cache = self.request_cache.add(KeyRequestCache(self, circuit, sock_addr, info_hash))

        self.tunnel_data(circuit, sock_addr, u"key-request", KeyRequestPayload(cache.number, info_hash))

    def check_key_request(self, payload, circuit_id):
        self.logger.info("Check key request, circuit_id: %s", circuit_id)
        info_hash = payload.info_hash

        if not circuit_id.startswith(u"circuit_"):
            if info_hash not in self.intro_point_for:
                self.logger.warning("not an intro point for this infohash")
                return False
        else:
            if info_hash not in self.session_keys:
                self.logger.warning("not seeding this infohash")
                return False
        return True

    def on_key_request(self, source_address, data, circuit_id=''):
        dist, payload = self._ez_unpack_noauth(KeyRequestPayload, data)

        if not self.check_key_request(payload, circuit_id):
            return

        if not circuit_id.startswith(u"circuit_"):
            # The intropoint receives the message over a socket, and forwards it to the seeder
            self.logger.info("On key request: relay key request")
            relay_circuit = self.intro_point_for[payload.info_hash]

            cache = self.request_cache.add(KeyRelayCache(self,
                                                         relay_circuit,
                                                         payload.identifier,
                                                         source_address,
                                                         payload.info_hash))

            self.tunnel_data(relay_circuit, self.my_estimated_wan, u"key-request",
                             KeyRequestPayload(cache.number, payload.info_hash))
        else:
            # The seeder responds with keys back to the intropoint
            info_hash = payload.info_hash
            key = self.session_keys[info_hash]
            circuit = self.circuits[int(circuit_id[8:])]
            self.logger.info("On key request: respond with keys to %s" % repr(source_address))
            pex_peers = self.infohash_pex.get(info_hash, set())

            self.tunnel_data(circuit, source_address, u'key-response',
                             KeyResponsePayload(payload.identifier, key.pub().key_to_bin(),
                                                encode(list(pex_peers)[:50])))

    def check_key_response(self, payload):
        self.logger.info("Check key response")
        request = self.request_cache.get(u"key-request", payload.identifier)
        return not not request

    def on_key_response(self, source_address, data, circuit_id=''):
        dist, payload = self._ez_unpack_noauth(KeyResponsePayload, data)

        if not self.check_key_response(payload):
            self.logger.error("Key response packet invalid!")
            return

        if not circuit_id.startswith(u"circuit_"):
            cache = self.request_cache.pop(u"key-request", payload.identifier)
            self.logger.info('On key response: forward message because received over socket')

            dist = GlobalTimeDistributionPayload(self.global_time).to_pack_list()
            payload = KeyResponsePayload(cache.identifier, payload.public_key, payload.pex_peers).to_pack_list()

            packet = self._ez_pack(self._prefix, 14, [dist, payload], False)
            self.send_packet([cache.return_sock_addr], u"key-response", TUNNEL_PREFIX + packet)
        else:
            # pop key-request cache and notify gui
            self.logger.info("On key response: received keys")
            cache = self.request_cache.pop(u"key-request", payload.identifier)
            _, pex_peers = decode(payload.pex_peers)

            # Cache this peer and key for pex via key-response
            self.logger.info("Added key to peer exchange cache")
            self.infohash_pex[cache.info_hash].add((cache.sock_addr, payload.public_key))

            # Add received pex_peers to own list of known peers for this infohash
            for pex_peer in pex_peers:
                pex_peer_sock, pex_peer_key = pex_peer
                self.infohash_pex[cache.info_hash].add((pex_peer_sock, pex_peer_key))

            # Initate end-to-end circuits for all known peers in the pex list
            for peer in self.infohash_pex[cache.info_hash]:
                peer_sock, peer_key = peer
                if cache.info_hash not in self.infohash_ip_circuits:
                    self.logger.info("Create end-to-end on pex_peer %s" % repr(peer_sock))
                    self.create_e2e(cache.circuit, peer_sock, cache.info_hash, peer_key)

    def create_e2e(self, circuit, sock_addr, info_hash, public_key):
        hop = Hop(self.crypto.key_from_public_bin(public_key))
        hop.dh_secret, hop.dh_first_part = self.crypto.generate_diffie_secret()
        self.logger.info("Create end to end initiated here")
        cache = self.request_cache.add(E2ERequestCache(self, info_hash, circuit, hop, sock_addr))

        self.tunnel_data(circuit, sock_addr, u'create-e2e', CreateE2EPayload(cache.number, info_hash, hop.node_id,
                                                                             hop.node_public_key, hop.dh_first_part))

    def on_create_e2e(self, source_address, data, circuit_id=''):
        dist, payload = self._ez_unpack_noauth(CreateE2EPayload, data)

        # if we have received this message over a socket, we need to forward it
        if not circuit_id.startswith(u"circuit_"):
            self.logger.info('On create e2e: forward message because received over socket')
            relay_circuit = self.intro_point_for[payload.info_hash]

            self.tunnel_data(relay_circuit, source_address, u'create-e2e', payload)
        else:
            self.logger.info('On create e2e: create rendezvous point')
            self.create_rendezvous_point(self.hops[payload.info_hash],
                                         lambda rendezvous_point:
                                         self.create_created_e2e(rendezvous_point, source_address, payload, circuit_id),
                                         payload.info_hash)

    def create_created_e2e(self, rendezvous_point, source_address, payload, circuit_id):
        info_hash = payload.info_hash
        key = self.session_keys[info_hash]

        circuit = self.circuits[int(circuit_id[8:])]
        shared_secret, Y, AUTH = self.crypto.generate_diffie_shared_secret(payload.key, key)
        rendezvous_point.circuit.hs_session_keys = self.crypto.generate_session_keys(shared_secret)
        rp_info_enc = self.crypto.encrypt_str(
            encode((rendezvous_point.rp_info, rendezvous_point.cookie)),
            *self.get_session_keys(rendezvous_point.circuit.hs_session_keys, EXIT_NODE))

        self.tunnel_data(circuit, source_address, u'created-e2e',
                         CreatedE2EPayload(payload.identifier, Y, AUTH, rp_info_enc))

    def check_created_e2e(self, payload, circuit_id):
        if not circuit_id.startswith(u"circuit_"):
            self.logger.warning("must be received from a circuit")
            return False
        request = self.request_cache.get(u"e2e-request", payload.identifier)
        if not request:
            self.logger.warning("invalid created-e2e identifier")
            return False
        return True

    def on_created_e2e(self, source_address, data, circuit_id):
        dist, payload = self._ez_unpack_noauth(CreatedE2EPayload, data)

        if not self.check_created_e2e(payload, circuit_id):
            return

        cache = self.request_cache.pop(u"e2e-request", payload.identifier)
        shared_secret = self.crypto.verify_and_generate_shared_secret(cache.hop.dh_secret,
                                                                      payload.key,
                                                                      payload.auth,
                                                                      cache.hop.public_key.key.pk)
        session_keys = self.crypto.generate_session_keys(shared_secret)

        _, decoded = decode(self.crypto.decrypt_str(payload.rp_sock_addr,
                                                    session_keys[EXIT_NODE],
                                                    session_keys[EXIT_NODE_SALT]))
        rp_info, cookie = decoded

        # Since it is the seeder that chose the rendezvous_point, we're essentially losing 1 hop of anonymity
        # at the downloader end. To compensate we add an extra hop.
        required_exit = Peer(rp_info[2], rp_info[:2])
        self.create_circuit(self.hops[cache.info_hash] + 1,
                            CIRCUIT_TYPE_RENDEZVOUS,
                            callback=lambda circuit, cookie=cookie, session_keys=session_keys,
                            info_hash=cache.info_hash, sock_addr=cache.sock_addr: self.create_link_e2e(circuit,
                                                                                                       cookie,
                                                                                                       session_keys,
                                                                                                       info_hash,
                                                                                                       sock_addr),
                            required_exit=required_exit,
                            info_hash=cache.info_hash)

    def create_link_e2e(self, circuit, cookie, session_keys, info_hash, sock_addr):
        self.my_download_points[circuit.circuit_id] = (info_hash, circuit.goal_hops, sock_addr)
        circuit.hs_session_keys = session_keys

        cache = self.request_cache.add(LinkRequestCache(self, circuit, info_hash))
        self.send_cell([circuit.sock_addr], u'link-e2e', LinkE2EPayload(circuit.circuit_id, cache.number, cookie))

    def check_link_e2e(self, payload, circuit_id):
        if not circuit_id.startswith(u"circuit_"):
            self.logger.warning("must be received from a circuit")
            return False
        if payload.cookie not in self.rendezvous_point_for:
            self.logger.warning("not a rendezvous point for this cookie")
            return False

        circuit_id = int(circuit_id[8:])
        if self.exit_sockets[circuit_id].enabled:
            self.logger.warning("exit socket for circuit is enabled, cannot link")
            return False

        relay_circuit = self.rendezvous_point_for[payload.cookie]
        if self.exit_sockets[relay_circuit.circuit_id].enabled:
            self.logger.warning("exit socket for relay_circuit is enabled, cannot link")
        return True

    def on_link_e2e(self, source_address, data, circuit_id):
        dist, payload = self._ez_unpack_noauth(LinkE2EPayload, data)

        if not self.check_link_e2e(payload, circuit_id):
            return

        circuit = self.exit_sockets[int(circuit_id[8:])]
        relay_circuit = self.rendezvous_point_for[payload.cookie]

        self.remove_exit_socket(circuit.circuit_id, 'linking circuit')
        self.remove_exit_socket(relay_circuit.circuit_id, 'linking circuit')

        self.relay_from_to[circuit.circuit_id] = RelayRoute(relay_circuit.circuit_id, relay_circuit.sock_addr, True,
                                                            mid=relay_circuit.mid)
        self.relay_from_to[relay_circuit.circuit_id] = RelayRoute(circuit.circuit_id, circuit.sock_addr, True,
                                                                  mid=circuit.mid)

        self.send_cell([source_address], u"linked-e2e", LinkedE2EPayload(circuit.circuit_id, payload.identifier))

    def check_linked_e2e(self, payload, circuit_id):
        if not circuit_id.startswith(u"circuit_"):
            self.logger.warning("must be received from a circuit")
            return False

        request = self.request_cache.get(u"link-request", payload.identifier)
        if not request:
            self.logger.warning("invalid linked-e2e identifier")
            return False

        return True

    def on_linked_e2e(self, source_address, data, circuit_id):
        dist, payload = self._ez_unpack_noauth(LinkedE2EPayload, data)

        if not self.check_linked_e2e(payload, circuit_id):
            return

        cache = self.request_cache.pop(u"link-request", payload.identifier)
        download = self.find_download(cache.info_hash)
        if download:
            download((self.circuit_id_to_ip(cache.circuit.circuit_id), CIRCUIT_ID_PORT))
        else:
            self.logger.error('On linked e2e: could not find download for %s!', cache.info_hash)

    def find_download(self, lookup_info_hash):
        return self.service_callbacks.get(lookup_info_hash, None)

    def create_introduction_point(self, info_hash, amount=1):
        self.logger.info("Creating %d introduction point(s)", amount)

        # Create a separate key per infohash
        if info_hash not in self.session_keys:
            self.session_keys[info_hash] = self.crypto.generate_key(u"curve25519")

        def callback(circuit):
            # We got a circuit, now let's create an introduction point
            circuit_id = circuit.circuit_id
            self.my_intro_points[circuit_id].append((info_hash))

            cache = self.request_cache.add(IPRequestCache(self, circuit))
            self.send_cell([circuit.sock_addr],
                           u'establish-intro', EstablishIntroPayload(circuit_id, cache.number, info_hash))
            self.logger.info("Established introduction tunnel %s", circuit_id)

        for _ in range(amount):
            # Create a circuit to the introduction point + 1 hop, to prevent the introduction
            # point from knowing what the seeder is seeding
            circuit_id = self.create_circuit(self.hops[info_hash] + 1,
                                             CIRCUIT_TYPE_IP,
                                             callback,
                                             info_hash=info_hash)
            self.infohash_ip_circuits[info_hash].append((circuit_id, time.time()))

    def check_establish_intro(self, circuit_id):
        if not circuit_id.startswith(u"circuit_"):
            self.logger.warning("did not receive this message from a circuit")
            return False

        return True

    def on_establish_intro(self, source_address, data, circuit_id):
        dist, payload = self._ez_unpack_noauth(EstablishIntroPayload, data)

        if not self.check_establish_intro(circuit_id):
            return

        circuit = self.exit_sockets[int(circuit_id[8:])]
        self.intro_point_for[payload.info_hash] = circuit

        self.send_cell([source_address], u"intro-established", IntroEstablishedPayload(circuit.circuit_id, payload.identifier))
        self.dht_announce(payload.info_hash)

    def check_intro_established(self, payload):
        request = self.request_cache.get(u"establish-intro", payload.identifier)
        if not request:
            self.logger.warning("invalid intro-established request identifier")
            return False

        return True

    def on_intro_established(self, source_address, data, circuit_id):
        dist, payload = self._ez_unpack_noauth(IntroEstablishedPayload, data)

        if not self.check_intro_established(payload):
            return

        self.request_cache.pop(u"establish-intro", payload.identifier)
        self.logger.info("Got intro-established from %s", source_address)

    def create_rendezvous_point(self, hops, finished_callback, info_hash):
        def callback(circuit):
            # We got a circuit, now let's create a rendezvous point
            circuit_id = circuit.circuit_id
            rp = RendezvousPoint(circuit, os.urandom(20), finished_callback)

            cache = self.request_cache.add(RPRequestCache(self, rp))

            self.send_cell([circuit.sock_addr],
                           u'establish-rendezvous', EstablishRendezvousPayload(circuit_id, cache.number, rp.cookie))

        # create a new circuit to be used for transferring data
        circuit_id = self.create_circuit(hops,
                                         CIRCUIT_TYPE_RP,
                                         callback,
                                         info_hash=info_hash)
        self.infohash_rp_circuits[info_hash].append(circuit_id)

    def check_establish_rendezvous(self, circuit_id):
        if not circuit_id.startswith(u"circuit_"):
            self.logger.warning("did not receive this message from a circuit")
            return False

        return True

    def on_establish_rendezvous(self, source_address, data, circuit_id):
        dist, payload = self._ez_unpack_noauth(EstablishRendezvousPayload, data)

        if not self.check_establish_rendezvous(circuit_id):
            return

        circuit = self.exit_sockets[int(circuit_id[8:])]
        self.rendezvous_point_for[payload.cookie] = circuit

        self.send_cell([source_address], u"rendezvous-established", RendezvousEstablishedPayload(
            circuit.circuit_id, payload.identifier, self.my_estimated_wan))

    def check_rendezvous_established(self, payload):
        request = self.request_cache.get(u"establish-rendezvous", payload.identifier)
        if not request:
            self.logger.warning("invalid rendezvous-established request identifier")
            return False

        return True

    def on_rendezvous_established(self, source_address, data, circuit_id):
        dist, payload = self._ez_unpack_noauth(RendezvousEstablishedPayload, data)

        if not self.check_rendezvous_established(payload):
            return

        rp = self.request_cache.pop(u"establish-rendezvous", payload.identifier).rp

        sock_addr = payload.rendezvous_point_addr
        rp.rp_info = (sock_addr[0], sock_addr[1], self.crypto.key_to_bin(rp.circuit.hops[-1].public_key))
        rp.finished_callback(rp)

    def dht_lookup(self, info_hash, cb):
        if self.dht_provider:
            self.dht_provider.lookup(info_hash, cb)
        else:
            self.logger.error("Need a DHT provider to lookup to the DHT")

    def dht_announce(self, info_hash):
        if self.dht_provider:
            self.dht_provider.announce(info_hash)
        else:
            self.logger.error("Need a DHT provider to announce to the DHT")

    def get_lookup_info_hash(self, info_hash):
        return hashlib.sha1('tribler anonymous download' + info_hash.encode('hex')).digest()
