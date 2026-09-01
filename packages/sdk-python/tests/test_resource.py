from __future__ import annotations

from vigil import __version__


def test_resource_includes_sdk_name_and_version(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op"):
        pass
    vigil.flush()
    resource = recording_transport.bodies[0]["resource"]
    assert resource["sdk.name"] == "vigil-python"
    assert resource["sdk.version"] == __version__


def test_resource_includes_configured_service_name(vigil_factory, recording_transport) -> None:
    vigil = vigil_factory(service_name="checkout-service")
    with vigil.start_span("op"):
        pass
    vigil.flush()
    resource = recording_transport.bodies[0]["resource"]
    assert resource["service.name"] == "checkout-service"


def test_resource_omits_service_name_when_not_configured(
    vigil_factory, recording_transport
) -> None:
    vigil = vigil_factory()
    with vigil.start_span("op"):
        pass
    vigil.flush()
    resource = recording_transport.bodies[0]["resource"]
    assert "service.name" not in resource


def test_resource_is_shared_across_every_span_in_a_batch(
    vigil_factory, recording_transport
) -> None:
    vigil = vigil_factory(service_name="svc", max_batch_size=10)
    with vigil.start_span("a"):
        pass
    with vigil.start_span("b"):
        pass
    vigil.flush()
    body = recording_transport.bodies[0]
    assert len(body["spans"]) == 2
    assert body["resource"]["service.name"] == "svc"
