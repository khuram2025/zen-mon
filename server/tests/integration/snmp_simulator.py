"""Minimal loopback-only SNMPv2c GET fixture, never a vendor emulator."""
import asyncio
import ipaddress
from pyasn1.codec.ber import decoder, encoder
from pyasn1.type import namedtype, tag, univ


class VarBind(univ.Sequence):
    componentType = namedtype.NamedTypes(namedtype.NamedType('oid', univ.ObjectIdentifier()),
                                        namedtype.NamedType('value', univ.Any()))


class Bindings(univ.SequenceOf):
    componentType = VarBind()


class PDU(univ.Sequence):
    componentType = namedtype.NamedTypes(namedtype.NamedType('request', univ.Integer()),
        namedtype.NamedType('error', univ.Integer()), namedtype.NamedType('index', univ.Integer()),
        namedtype.NamedType('bindings', Bindings()))


class Message(univ.Sequence):
    componentType = namedtype.NamedTypes(namedtype.NamedType('version', univ.Integer()),
        namedtype.NamedType('community', univ.OctetString()), namedtype.NamedType('pdu', univ.Any()))


class SimulatedSNMP(asyncio.DatagramProtocol):
    community = 'development-fixture-only'

    def __init__(self):
        self.requests = 0
        self.values = {
            '1.3.6.1.2.1.1.1.0': univ.OctetString('Cisco IOS Software development fixture'),
            '1.3.6.1.2.1.1.2.0': univ.ObjectIdentifier('1.3.6.1.4.1.9.1.1'),
            '1.3.6.1.2.1.1.5.0': univ.OctetString('simulated-network-device'),
            '1.3.6.1.2.1.1.6.0': univ.OctetString('isolated-test-lab'),
            '1.3.6.1.4.1.2021.11.11.0': univ.Integer(99),
        }

    def connection_made(self, transport):
        assert ipaddress.ip_address(transport.get_extra_info('sockname')[0]).is_loopback
        self.transport = transport

    def datagram_received(self, data, address):
        if not ipaddress.ip_address(address[0]).is_loopback: return
        message, rest = decoder.decode(data, asn1Spec=Message())
        if rest or int(message['version']) != 1 or str(message['community']) != self.community: return
        request, rest = decoder.decode(message['pdu'].asOctets(),
            asn1Spec=PDU().subtype(implicitTag=tag.Tag(tag.tagClassContext,tag.tagFormatConstructed,0)))
        if rest: return
        response=PDU().subtype(implicitTag=tag.Tag(tag.tagClassContext,tag.tagFormatConstructed,2))
        response['request']=request['request']; response['error']=0; response['index']=0
        bindings=Bindings()
        for i, binding in enumerate(request['bindings']):
            oid=str(binding['oid']); entry=VarBind(); entry['oid']=binding['oid']
            value=self.values.get(oid,univ.Null('').subtype(implicitTag=tag.Tag(tag.tagClassContext,tag.tagFormatSimple,0)))
            entry['value']=encoder.encode(value); bindings[i]=entry
        response['bindings']=bindings
        result=Message(); result['version']=1; result['community']=self.community; result['pdu']=encoder.encode(response)
        self.transport.sendto(encoder.encode(result),address)
        self.requests+=1
