import pytest
from pydantic import ValidationError

from fastcs_ximc import XimcConnectionSettings


class TestXimcConnectionSettings:
    def test_uri_is_used_verbatim(self):
        settings = XimcConnectionSettings(uri="xi-com:///dev/ttyACM0")
        assert settings.device_uri == "xi-com:///dev/ttyACM0"

    def test_port_expands_to_com_uri(self):
        settings = XimcConnectionSettings(port="/dev/ttyACM0")
        assert settings.device_uri == "xi-com:///dev/ttyACM0"

    def test_port_env_resolves(self, monkeypatch):
        monkeypatch.setenv("AXIS_PORT", "/dev/ttyACM0")
        settings = XimcConnectionSettings(port_env="AXIS_PORT")
        assert settings.port == "/dev/ttyACM0"
        assert settings.device_uri == "xi-com:///dev/ttyACM0"

    def test_port_env_missing_raises(self, monkeypatch):
        monkeypatch.delenv("MISSING_PORT", raising=False)
        with pytest.raises(ValidationError, match="MISSING_PORT"):
            XimcConnectionSettings(port_env="MISSING_PORT")

    def test_multiple_addresses_raise(self):
        with pytest.raises(ValidationError, match="only one of"):
            XimcConnectionSettings(uri="xi-com:///dev/ttyACM0", port="/dev/ttyACM0")

    def test_no_address_raises(self):
        with pytest.raises(ValidationError, match="Must specify one of"):
            XimcConnectionSettings()

    def test_unknown_scheme_raises(self):
        with pytest.raises(ValidationError, match="not a libximc URI"):
            XimcConnectionSettings(uri="http://example.com/axis")

    @pytest.mark.parametrize(
        "uri",
        [
            "xi-com:///dev/ttyACM0",
            "xi-net://192.168.0.1/00000001",
            "xi-udp://192.168.0.1",
            "xi-emu:///tmp/axis.bin",
        ],
    )
    def test_all_libximc_schemes_accepted(self, uri):
        assert XimcConnectionSettings(uri=uri).device_uri == uri

    def test_virtual_device_detected(self):
        assert XimcConnectionSettings(uri="xi-emu:///tmp/axis.bin").is_virtual
        assert not XimcConnectionSettings(port="/dev/ttyACM0").is_virtual

    def test_scheme(self):
        assert XimcConnectionSettings(uri="xi-net://192.168.0.1/1").scheme == "xi-net"
