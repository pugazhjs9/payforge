from payforge import webhook_signing as sig

SECRET = "whsec_test"
BODY = b'{"id":"evt_1","type":"payment.succeeded"}'
NOW = 1_763_870_000


def test_a_signed_body_verifies() -> None:
    header = sig.header_value(SECRET, BODY, now=NOW)
    assert header.startswith(f"t={NOW},v1=")
    assert sig.verify(SECRET, header, BODY, now=NOW + 10)


def test_changed_body_wrong_secret_or_garbage_fail() -> None:
    header = sig.header_value(SECRET, BODY, now=NOW)
    assert not sig.verify(SECRET, header, BODY + b" ", now=NOW)
    assert not sig.verify("whsec_other", header, BODY, now=NOW)
    assert not sig.verify(SECRET, "nonsense", BODY, now=NOW)
    assert not sig.verify(SECRET, f"t={NOW}", BODY, now=NOW)


def test_old_signatures_are_rejected_so_replays_fail() -> None:
    header = sig.header_value(SECRET, BODY, now=NOW)
    assert sig.verify(SECRET, header, BODY, now=NOW + 300)
    assert not sig.verify(SECRET, header, BODY, now=NOW + 301)
    assert not sig.verify(SECRET, header, BODY, now=NOW - 301)  # from the future


def test_any_of_several_signatures_may_match_during_rotation() -> None:
    old = sig.compute("whsec_old", NOW, BODY)
    new = sig.compute(SECRET, NOW, BODY)
    assert sig.verify(SECRET, f"t={NOW},v1={old},v1={new}", BODY, now=NOW)
