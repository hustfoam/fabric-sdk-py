# Copyright IBM Corp. 2016 All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import sys
import logging
import rx
import random

from google.protobuf.message import DecodeError
from google.protobuf.timestamp_pb2 import Timestamp
from hfc.protos.common import common_pb2, configtx_pb2
from hfc.protos.msp import identities_pb2
from hfc.protos.peer import proposal_pb2
from hfc.protos.utils import create_tx_payload

_logger = logging.getLogger(__name__ + '.utils')


def proto_str(x):
    return proto_b(x).decode("utf-8")


proto_b = \
    sys.version_info[0] < 3 and (lambda x: x) or (
        lambda x: x.encode('latin1'))

if sys.version_info < (3, 0):
    from Queue import Queue
else:
    from queue import Queue


def create_serialized_identity(user):
    """Create serialized identity from user.

    Args:
        user (user object): The user object that should be serialized.

    Returns:
        serialized_identity: Protobuf SerializedIdentity of
            the given user object.

    """
    serialized_identity = identities_pb2.SerializedIdentity()
    serialized_identity.mspid = user.msp_id
    serialized_identity.id_bytes = user.enrollment.cert
    return serialized_identity.SerializeToString()


def build_header(creator, channel_header, nonce):
    """This function will build the common header.

    Args:
        creator (protobuf SerializedIdentity):
            Serialized identity of the creator.
        channel_header (protobuf ChannelHeader): ChannelHeader
        nonce (str): Nonce that has been used for the tx_id.

    Returns:
        header: Returns created protobuf common header.

    """
    signature_header = common_pb2.SignatureHeader()
    signature_header.creator = creator
    signature_header.nonce = nonce

    header = common_pb2.Header()
    header.signature_header = signature_header.SerializeToString()
    header.channel_header = channel_header.SerializeToString()

    return header


def build_channel_header(type, tx_id, channel_id,
                         timestamp, epoch=0, extension=None):
    """Build channel header.

    Args:
        type (common_pb2.HeaderType): type
        tx_id (str): transaction id
        channel_id (str): channel id
        timestamp (grpc.timestamp): timestamp
        epoch (int): epoch
        extension: extension

    Returns:
        common_proto.Header instance

    """
    channel_header = common_pb2.ChannelHeader()
    channel_header.type = type
    channel_header.version = 1
    channel_header.channel_id = proto_str(channel_id)
    channel_header.tx_id = proto_str(tx_id)
    channel_header.epoch = epoch
    channel_header.timestamp.CopyFrom(timestamp)
    if extension:
        channel_header.extension = extension

    return channel_header


def string_to_signature(string_signatures):
    """Check if signatures are already in protobuf format.

    Args:
        string_signatures (list): An list of protobuf ConfigSignatures either
            represented as or serialized as byte strings.

    Returns:
         list: List of protobuf ConfigSignatures.

    """
    signatures = []

    for signature in string_signatures:
        if signature and hasattr(signature, 'header') \
                and hasattr(signature, 'signature'):
            _logger.debug('_string_to_signature - signature is protobuf')
            config_signature = signature

        else:
            _logger.debug('_string_to_signature - signature is string')

            config_signature = configtx_pb2.ConfigSignature()
            config_signature.ParseFromString(signature)

        signatures.append(config_signature)

    return signatures


def current_timestamp():
    """Get current timestamp.

    Returns:
        Current timestamp.

    """
    timestamp = Timestamp()
    timestamp.GetCurrentTime()
    return timestamp


def extract_channel_config(configtx_proto_envelope):
    """ Extracts the protobuf 'ConfigUpdate' object out ouf the 'ConfigEnvelope'.

    Args:
        configtx_proto_envelope (common_pb2.Envelope): The encoded bytes of the
            ConfigEnvelope protofbuf.

    Returns:
        config_update (configtx_pb2.ConfigUpadeEnvelope.config_update):
            The encoded bytes of the ConfigUpdate protobuf, ready to be signed

    Raises:
        ValueError: If there is an error in protobuf_decode due to a wrong or
            not valid profobuf file a ValueError is raised.

    """
    _logger.debug('extract_channel_config - start')

    try:
        envelope = common_pb2.Envelope()
        envelope.ParseFromString(configtx_proto_envelope)

        payload = common_pb2.Payload()
        payload.ParseFromString(envelope.payload)

        configtx = configtx_pb2.ConfigUpdateEnvelope()
        configtx.ParseFromString(payload.data)

    except DecodeError as e:
        _logger.error('extract_channel_config - an error occurred decoding'
                      ' the configtx_proto_envelope: {}'.format(e))
        raise ValueError('The given configtx_proto_envelope was not valid: {}'
                         .format(e))

    return configtx.config_update


def build_cc_proposal(cci_spec, header, transient_map):
    """ Create an chaincode transaction proposal

    Args:
        transient_map: transient data map
        cci_spec: The spec
        header: header of the proposal

    Returns: The created proposal

    """
    cc_payload = proposal_pb2.ChaincodeProposalPayload()
    cc_payload.input = cci_spec.SerializeToString()
    if transient_map:
        cc_payload.TransientMap = transient_map

    proposal = proposal_pb2.Proposal()
    proposal.header = header.SerializeToString()
    proposal.payload = cc_payload.SerializeToString()

    return proposal


def sign_proposal(tx_context, proposal):
    """ Sign a proposal
    Args:
        tx_context: transaction context
        proposal: proposal to sign on

    Returns: Signed proposal

    """
    proposal_bytes = proposal.SerializeToString()
    sig = tx_context.sign(proposal_bytes)

    signed_proposal = proposal_pb2.SignedProposal()
    signed_proposal.signature = sig
    signed_proposal.proposal_bytes = proposal_bytes

    return signed_proposal


def send_transaction_proposal(proposal, header, tx_context,
                              peers, scheduler=None):
    """Send transaction proposal

    Args:
        header: header
        tx_context: transaction context
        proposal: transaction proposal
        peers: peers
        scheduler: see rx.Scheduler

    Returns: a list containing all the proposal response

    """
    signed_proposal = sign_proposal(
        tx_context, proposal)

    send_executions = [peer.send_proposal(signed_proposal, scheduler)[0]
                       for peer in peers]

    return send_executions


def send_transaction(orderers, tran_req, tx_context, scheduler=None):
    """Send a transaction to the chain's orderer service (one or more
    orderer endpoints) for consensus and committing to the ledger.

    This call is asynchronous and the successful transaction commit is
    notified via a BLOCK or CHAINCODE event. This method must provide a
    mechanism for applications to attach event listeners to handle
    'transaction submitted', 'transaction complete' and 'error' events.

    Args:
        scheduler: scheduler
        tx_context: transaction context
        orderers: orderers
        tran_req (TransactionRequest): The transaction object

    Returns:
        result (EventEmitter): an handle to allow the application to
        attach event handlers on 'submitted', 'complete', and 'error'.

    """
    if not tran_req:
        return rx.Observable.just(ValueError(
            "Missing input request object on the transaction request"
        ))

    if not tran_req.responses or len(tran_req.responses) < 1:
        return rx.Observable.just(ValueError(
            "Missing 'proposalResponses' parameter in transaction request"
        ))

    if not tran_req.proposal:
        return rx.Observable.just(ValueError(
            "Missing 'proposalResponses' parameter in transaction request"
        ))

    if len(orderers) < 1:
        return rx.Observable.just(ValueError(
            "Missing orderer objects on this chain"
        ))

    endorsements = map(lambda res: res[0].endorsement, tran_req.responses)

    tran_payload_bytes = create_tx_payload(endorsements, tran_req)
    envelope = sign_tran_payload(tx_context, tran_payload_bytes)

    if sys.version_info < (3, 0):
        orderer = random.choice(orderers.values())
    else:
        orderer = random.choice(list(orderers.values()))
    return orderer.broadcast(envelope, scheduler)


def sign_tran_payload(tx_context, tran_payload_bytes):
    """Sign a transaction payload

    Args:
        signing_identity: id to sign with
        tran_payload: transaction payload to sign on

    Returns: Envelope

    """
    sig = tx_context.sign(tran_payload_bytes)

    envelope = common_pb2.Envelope()
    envelope.signature = sig
    envelope.payload = tran_payload_bytes

    return envelope


def build_tx_req(responses):
    """ Check the endorsements from peers

    Args:
        reponses: rx.Oberservable instance from endorsers

    Return: transaction request or None for endorser failure
    """

    class TXRequest(object):

        def __init__(self, responses, proposal, header):
            self._responses = responses
            self._proposal = proposal
            self._header = header

        @property
        def responses(self):
            return self._responses

        @property
        def proposal(self):
            return self._proposal

        @property
        def header(self):
            return self._header

    q = Queue(1)
    responses.subscribe(on_next=lambda x: q.put(x),
                        on_error=lambda x: q.put(x))

    res = q.get(timeout=15)
    for r in res[0]:
        if r[0].response.status != 200:
            return None

    responses = res[0]
    proposal = res[1]
    header = res[2]
    return TXRequest(responses, proposal, header)
