"""Provider test doubles for core tests (re-exported from ``backend.providers.testing``).

Plugin tests import ``backend.providers.testing`` directly; each plugin runs the
provider contract for its own providers in ``<plugin>/tests/test_contract.py``.
"""

from backend.providers.testing import ContractFixture, DeadHTTP, FakeHTTP, FakeResponse  # noqa: F401
