"""Tests for daily bucket selection in the sensor platform."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
import sys
import types
import unittest


def load_sensor_module():
    """Load the sensor module with lightweight Home Assistant stubs."""
    sensor_module = types.ModuleType("homeassistant.components.sensor")
    sensor_module.SensorDeviceClass = types.SimpleNamespace(
        ENERGY="energy", MONETARY="monetary"
    )
    sensor_module.SensorEntity = type("SensorEntity", (), {})
    sensor_module.SensorStateClass = types.SimpleNamespace(TOTAL="total")

    config_entries_module = types.ModuleType("homeassistant.config_entries")
    config_entries_module.ConfigEntry = type("ConfigEntry", (), {})

    const_module = types.ModuleType("homeassistant.const")
    const_module.UnitOfEnergy = types.SimpleNamespace(KILO_WATT_HOUR="kWh")

    core_module = types.ModuleType("homeassistant.core")
    core_module.HomeAssistant = type("HomeAssistant", (), {})
    core_module.callback = lambda func: func

    entity_module = types.ModuleType("homeassistant.helpers.entity")
    entity_module.DeviceInfo = type("DeviceInfo", (dict,), {})

    update_coordinator_module = types.ModuleType(
        "homeassistant.helpers.update_coordinator"
    )
    update_coordinator_module.CoordinatorEntity = type("CoordinatorEntity", (), {})
    update_coordinator_module.DataUpdateCoordinator = type(
        "DataUpdateCoordinator", (), {}
    )

    sys.modules["homeassistant"] = types.ModuleType("homeassistant")
    sys.modules["homeassistant.components"] = types.ModuleType(
        "homeassistant.components"
    )
    sys.modules["homeassistant.components.sensor"] = sensor_module
    sys.modules["homeassistant.config_entries"] = config_entries_module
    sys.modules["homeassistant.const"] = const_module
    sys.modules["homeassistant.core"] = core_module
    sys.modules["homeassistant.helpers"] = types.ModuleType("homeassistant.helpers")
    sys.modules["homeassistant.helpers.entity"] = entity_module
    sys.modules["homeassistant.helpers.update_coordinator"] = update_coordinator_module

    sys.modules["custom_components"] = types.ModuleType("custom_components")
    package_module = types.ModuleType("custom_components.hildebrandglow_dcc")
    package_module.__path__ = []
    sys.modules["custom_components.hildebrandglow_dcc"] = package_module

    const_package_module = types.ModuleType(
        "custom_components.hildebrandglow_dcc.const"
    )
    const_package_module.DOMAIN = "hildebrandglow_dcc"
    sys.modules["custom_components.hildebrandglow_dcc.const"] = const_package_module

    sensor_path = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / "hildebrandglow_dcc"
        / "sensor.py"
    )
    spec = importlib.util.spec_from_file_location(
        "custom_components.hildebrandglow_dcc.sensor",
        sensor_path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class Value:
    """Simple value wrapper matching pyglowmarkt readings."""

    def __init__(self, value: float) -> None:
        self.value = value


class FakeHass:
    """Minimal Home Assistant stub for async executor calls."""

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeResource:
    """Resource stub returning canned reading sequences."""

    def __init__(self, today_readings, historical_readings) -> None:
        self.classifier = "electricity.consumption"
        self.today_readings = today_readings
        self.historical_readings = historical_readings

    def catchup(self) -> None:
        """No-op catchup stub."""

    def get_readings(self, start, end, period, func, nulls):
        """Return today's or historical readings based on the end boundary."""
        if end == self.today_readings_end:
            return self.today_readings
        return self.historical_readings


class DailyData_Should(unittest.TestCase):
    """Daily data selection behaviour."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sensor = load_sensor_module()

    def make_resource(self, today_readings, historical_readings, now):
        """Create a fake resource tied to the current daily query window."""
        resource = FakeResource(today_readings, historical_readings)
        resource.today_readings_end = now.replace(second=0, microsecond=0)
        return resource

    def test_daily_data_should_prefer_today_bucket_when_today_exists(self):
        now = datetime.now().replace(second=0, microsecond=0)
        today = now.replace(hour=1, minute=0)
        yesterday = today - timedelta(days=1)

        resource = self.make_resource(
            [(yesterday, Value(12.5)), (today, Value(3.2))],
            [(yesterday, Value(12.5))],
            now,
        )

        value, is_today_value = asyncio.run(
            self.sensor.daily_data(FakeHass(), resource, 7.5)
        )

        self.assertEqual(value, 3.2)
        self.assertTrue(is_today_value)

    def test_daily_data_should_fall_back_to_latest_prior_bucket_when_today_missing(
        self,
    ):
        now = datetime.now().replace(second=0, microsecond=0)
        today = now.replace(hour=1, minute=0)
        yesterday = today - timedelta(days=1)
        two_days_ago = today - timedelta(days=2)

        resource = self.make_resource(
            [(yesterday, Value(12.5)), (today, Value(0.0))],
            [(two_days_ago, Value(8.1)), (yesterday, Value(12.5))],
            now,
        )

        value, is_today_value = asyncio.run(
            self.sensor.daily_data(FakeHass(), resource, 7.5)
        )

        self.assertEqual(value, 12.5)
        self.assertFalse(is_today_value)

    def test_daily_data_should_keep_last_value_when_no_positive_data_exists(self):
        now = datetime.now().replace(second=0, microsecond=0)
        today = now.replace(hour=1, minute=0)
        yesterday = today - timedelta(days=1)

        resource = self.make_resource(
            [(yesterday, Value(4.5)), (today, Value(0.0))],
            [],
            now,
        )

        value, is_today_value = asyncio.run(
            self.sensor.daily_data(FakeHass(), resource, 9.9)
        )

        self.assertEqual(value, 9.9)
        self.assertFalse(is_today_value)


class FakeVirtualEntity:
    """Minimal virtual entity stub."""

    name = None


class TransitionHandling_Should(unittest.TestCase):
    """Transition handling between fallback and local-today values."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.sensor = load_sensor_module()

    def test_reading_async_update_should_accept_lower_today_value_after_fallback(self):
        values = iter([(12.5, False), (3.2, True)])

        async def fake_daily_data(hass, resource, last_value):
            return next(values)

        original_daily_data = self.sensor.daily_data
        self.sensor.daily_data = fake_daily_data

        try:
            resource = types.SimpleNamespace(
                id="resource-1", classifier="electricity.consumption"
            )
            entity = self.sensor.Reading(FakeHass(), resource, FakeVirtualEntity())

            asyncio.run(entity.async_update())
            entity.lastUpdate = 0
            asyncio.run(entity.async_update())

            self.assertEqual(entity._attr_native_value, 3.2)
            self.assertEqual(entity.lastValue, 3.2)
        finally:
            self.sensor.daily_data = original_daily_data

    def test_cost_async_update_should_accept_lower_today_value_after_fallback(self):
        values = iter([(1250.0, False), (320.0, True)])

        async def fake_daily_data(hass, resource, last_value):
            return next(values)

        original_daily_data = self.sensor.daily_data
        self.sensor.daily_data = fake_daily_data

        try:
            resource = types.SimpleNamespace(
                id="resource-2", classifier="electricity.consumption.cost"
            )
            meter = types.SimpleNamespace(
                resource=types.SimpleNamespace(
                    id="meter-1", classifier="electricity.consumption"
                )
            )
            entity = self.sensor.Cost(FakeHass(), resource, FakeVirtualEntity())
            entity.meter = meter

            asyncio.run(entity.async_update())
            entity.lastUpdate = 0
            asyncio.run(entity.async_update())

            self.assertEqual(entity._attr_native_value, 3.2)
            self.assertEqual(entity.lastValue, 320.0)
        finally:
            self.sensor.daily_data = original_daily_data


if __name__ == "__main__":
    unittest.main()
