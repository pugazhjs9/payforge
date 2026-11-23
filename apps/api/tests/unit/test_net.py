import pytest

from payforge import net
from payforge.config import settings


@pytest.fixture(autouse=True)
def production_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "allow_insecure_webhooks", False)


def resolves_to(monkeypatch: pytest.MonkeyPatch, *addresses: str) -> None:
    async def fake(_host: str) -> list[str]:
        return list(addresses)

    monkeypatch.setattr(net, "_resolve", fake)


async def test_public_https_url_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    resolves_to(monkeypatch, "93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c")
    await net.check_webhook_url("https://shop.example/webhooks")


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://shop.example/webhooks", "https"),
        ("https://user:pw@shop.example/", "user name"),
        ("not a url at all", "https"),
        ("https:///no-host", "host"),
    ],
)
async def test_url_shape_rules(url: str, reason: str) -> None:
    with pytest.raises(net.UnsafeUrl, match=reason):
        await net.check_webhook_url(url)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # this machine
        "10.1.2.3",  # private network
        "192.168.1.10",
        "169.254.169.254",  # cloud metadata service: hands out credentials
        "100.64.0.1",  # carrier-grade NAT
        "0.0.0.0",  # noqa: S104 (a test value, not a bind address)
        "::1",
        "fd00::1",  # IPv6 private
        "::ffff:127.0.0.1",  # IPv4 hidden inside IPv6
    ],
)
async def test_private_and_internal_addresses_are_refused(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    resolves_to(monkeypatch, address)
    with pytest.raises(net.UnsafeUrl, match="public"):
        await net.check_webhook_url("https://looks-innocent.example/")


async def test_one_bad_address_among_good_ones_is_enough_to_refuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolves_to(monkeypatch, "93.184.215.14", "10.0.0.5")
    with pytest.raises(net.UnsafeUrl):
        await net.check_webhook_url("https://shop.example/")


async def test_ip_literals_are_checked_too() -> None:
    with pytest.raises(net.UnsafeUrl):
        await net.check_webhook_url("https://169.254.169.254/latest/meta-data/")


async def test_development_mode_allows_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "allow_insecure_webhooks", True)
    await net.check_webhook_url("http://localhost:8000/webhooks")
