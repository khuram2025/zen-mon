from app.core.snmp_oid import display_oid, normalize_oid


def test_equivalent_oid_spellings_normalise_identically():
    for raw in ("iso.3.6.1.4.1.9.1.1208", ".1.3.6.1.4.1.9.1.1208", "1.3.6.1.4.1.9.1.1208", " .1.3.6.1.4.1.9.1.1208 "):
        assert normalize_oid(raw) == "1.3.6.1.4.1.9.1.1208"
        assert display_oid(raw) == ".1.3.6.1.4.1.9.1.1208"


def test_empty_oid():
    assert normalize_oid(None) == ""
    assert display_oid("") is None
