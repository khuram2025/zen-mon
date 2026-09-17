import pytest
from app.services.mib_compiler import compile_sources, resolve_symbol

MIB = '''TEST-MIB DEFINITIONS ::= BEGIN
IMPORTS enterprises FROM SNMPv2-SMI;
testRoot OBJECT IDENTIFIER ::= { enterprises 55555 }
testValue OBJECT IDENTIFIER ::= { testRoot 1 }
END'''


def test_compiles_real_asn1_to_numeric_oid_without_network():
    result = compile_sources({'test.mib': MIB})
    assert result['errors'] == {}
    assert resolve_symbol(result, 'TEST-MIB::testValue')['oid'] == '1.3.6.1.4.1.55555.1'


def test_dependency_resolution_and_missing_dependency_report():
    child = '''CHILD-MIB DEFINITIONS ::= BEGIN
    IMPORTS testRoot FROM TEST-MIB;
    childValue OBJECT IDENTIFIER ::= { testRoot 2 }
    END'''
    result = compile_sources({'child.mib': child, 'parent.mib': MIB})
    assert resolve_symbol(result, 'CHILD-MIB::childValue')['oid'].endswith('.55555.2')
    missing = compile_sources({'child.mib': child})
    assert missing['errors'] and not missing['objects']


def test_invalid_mib_is_not_reported_compiled():
    result = compile_sources({'broken.mib': 'this is not ASN.1'})
    assert result['errors'] and not result['compiled_modules']


def test_object_type_compiles_syntax_and_description():
    mib = '''SENSOR-MIB DEFINITIONS ::= BEGIN
    IMPORTS enterprises, OBJECT-TYPE, Integer32 FROM SNMPv2-SMI;
    sensorRoot OBJECT IDENTIFIER ::= { enterprises 55556 }
    sensorTemperature OBJECT-TYPE
      SYNTAX Integer32
      MAX-ACCESS read-only
      STATUS current
      DESCRIPTION "Development temperature fixture."
      ::= { sensorRoot 1 }
    END'''
    result = compile_sources({'sensor.mib': mib})
    assert result['errors'] == {}
    obj = resolve_symbol(result, 'SENSOR-MIB::sensorTemperature')
    assert obj['oid'] == '1.3.6.1.4.1.55556.1'
    assert obj['class'] == 'objecttype'
    assert 'temperature' in obj['description'] and obj['syntax']
