"""Platform for sensor integration."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from numbers import Number

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(minutes=5)


def classifier_has(resource, token: str) -> bool:
    """Return True when a resource classifier contains the expected token."""
    return token in resource.classifier


def is_reading_resource(resource) -> bool:
    """Return True for reading resources (not cost resources)."""
    return (
        classifier_has(resource, "electricity.consumption")
        or classifier_has(resource, "electricity.export")
        or classifier_has(resource, "gas.consumption")
    ) and not classifier_has(resource, ".cost")


def cost_meter_key(resource) -> str | None:
    """Map a cost resource to its corresponding reading meter key."""
    if classifier_has(resource, "gas.consumption.cost"):
        return "gas.consumption"
    if classifier_has(resource, "electricity.consumption.cost"):
        return "electricity.consumption"
    return None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: Callable
) -> bool:
    """Set up the sensor platform."""
    entities: list = []
    meters: dict = {}
    processed_ids: set = set()

    # Get API object from the config flow
    glowmarkt = hass.data[DOMAIN][entry.entry_id]

    # Gather all virtual entities on the account
    virtual_entities: dict = {}
    try:
        virtual_entities = await hass.async_add_executor_job(
            glowmarkt.get_virtual_entities
        )
        _LOGGER.debug("Successful GET to %svirtualentity", glowmarkt.url)
    except Exception as ex:
        _LOGGER.error("Error fetching virtual entities: %s", ex)

    for virtual_entity in virtual_entities:
        resources: dict = {}
        try:
            resources = await hass.async_add_executor_job(virtual_entity.get_resources)
        except Exception as ex:
            _LOGGER.error("Error fetching resources: %s", ex)

        # Loop through all resources and create sensors
        for resource in resources:
            # Skip if we have already created entities for this resource ID
            if resource.id in processed_ids:
                continue

            if is_reading_resource(resource):
                reading_sensor = Reading(hass, resource, virtual_entity)
                entities.append(reading_sensor)
                processed_ids.add(resource.id)

                # Save the reading sensor as a meter so that the cost sensor can reference it
                if classifier_has(resource, "gas.consumption"):
                    meters["gas.consumption"] = reading_sensor
                elif classifier_has(resource, "electricity.consumption"):
                    meters["electricity.consumption"] = reading_sensor
                elif classifier_has(resource, "electricity.export"):
                    meters["electricity.export"] = reading_sensor

            # Tariff/Rate sensors (Not for export)
            if is_reading_resource(resource) and not classifier_has(
                resource, "electricity.export"
            ):
                coordinator = TariffCoordinator(hass, resource)
                entities.append(Standing(coordinator, resource, virtual_entity))
                entities.append(Rate(coordinator, resource, virtual_entity))

        # Cost sensors must be created after reading sensors
        for resource in resources:
            meter_key = cost_meter_key(resource)
            if meter_key is None:
                continue

            meter_sensor = meters.get(meter_key)
            if meter_sensor is None:
                continue

            cost_sensor = Cost(hass, resource, virtual_entity)
            cost_sensor.meter = meter_sensor
            entities.append(cost_sensor)

    async_add_entities(entities, update_before_add=True)
    return True


def supply_type(resource) -> str:
    """Return supply type."""
    if "electricity.consumption" in resource.classifier:
        return "electricity"
    if "electricity.export" in resource.classifier:
        return "electricity export"
    if "gas.consumption" in resource.classifier:
        return "gas"
    return "unknown"


def device_name(resource, virtual_entity) -> str:
    """Return device name."""
    supply = supply_type(resource)
    if virtual_entity.name is not None:
        return f"{virtual_entity.name} smart {supply} meter"
    return f"Smart {supply} meter"


def daily_reading_value(reading) -> float | None:
    """Return a numeric value for a reading tuple when possible."""
    value = reading[1].value
    if not isinstance(value, Number):
        return None
    return float(value)


def latest_positive_reading_for_date(readings, target_date) -> float | None:
    """Return the latest positive reading whose local date matches the target date."""
    matching_values: list[float] = []
    for reading in readings:
        if reading[0].date() != target_date:
            continue

        value = daily_reading_value(reading)
        if value is None or value <= 0:
            continue

        matching_values.append(value)

    if not matching_values:
        return None

    return matching_values[-1]


def latest_positive_reading_before_date(readings, target_date) -> float | None:
    """Return the latest positive reading whose local date is before the target date."""
    prior_values: list[float] = []
    for reading in readings:
        if reading[0].date() >= target_date:
            continue

        value = daily_reading_value(reading)
        if value is None or value <= 0:
            continue

        prior_values.append(value)

    if not prior_values:
        return None

    return prior_values[-1]


async def daily_data(hass: HomeAssistant, resource, lastValue) -> float:
    """Get the current local-day reading, falling back to the latest prior day with data."""
    v = lastValue
    now = datetime.now()

    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = now.replace(second=0, microsecond=0)
    today_date = today_start.date()

    # Wake up DCC
    try:
        await hass.async_add_executor_job(resource.catchup)
    except Exception:
        pass

    try:
        # Check Today
        today_readings = await hass.async_add_executor_job(
            resource.get_readings, today_start, today_end, "P1D", "sum", True
        )

        today_v = latest_positive_reading_for_date(today_readings, today_date)

        # If there is no positive reading bucket for the current local day, reuse the
        # latest prior day with data until the API publishes a local-today bucket.
        if today_v is None:
            search_start = today_start - timedelta(days=3)
            historical = await hass.async_add_executor_job(
                resource.get_readings, search_start, today_start, "P1D", "sum", True
            )
            historical_v = latest_positive_reading_before_date(historical, today_date)
            v = historical_v if historical_v is not None else lastValue
        else:
            v = today_v

        return float(v) if isinstance(v, Number) and v >= 0 else lastValue

    except Exception as ex:
        _LOGGER.error("Error updating %s: %s", resource.classifier, ex)
        return lastValue


async def tariff_data(hass: HomeAssistant, resource) -> float:
    """Get tariff data from the API."""
    try:
        return await hass.async_add_executor_job(resource.get_tariff)
    except Exception as ex:
        _LOGGER.debug("Tariff data fetch failed: %s", ex)
    return None


class Reading(SensorEntity):
    """Sensor object for daily reading."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_has_entity_name = True
    _attr_name = "reading (today)"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, hass: HomeAssistant, resource, virtual_entity) -> None:
        self._attr_unique_id = resource.id
        self.hass = hass
        self.initialised = False
        self.resource = resource
        self.virtual_entity = virtual_entity
        self.lastUpdate = 0
        self.lastValue = 0

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.resource.id)},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=device_name(self.resource, self.virtual_entity),
        )

    @property
    def icon(self) -> str | None:
        if "gas.consumption" in self.resource.classifier:
            return "mdi:fire"
        if "electricity.export" in self.resource.classifier:
            return "mdi:transmission-tower-export"
        return None

    async def async_update(self) -> None:
        dt = datetime.now()
        ts = datetime.timestamp(dt)
        if not self.initialised or (self.lastUpdate + 900) < ts:
            value = await daily_data(self.hass, self.resource, self.lastValue)
            if self.initialised and value < self.lastValue:
                self._attr_native_value = 0
                self.lastValue = 0
            else:
                self._attr_native_value = round(value, 3)
                self.lastValue = value
            self.initialised = True
            self.lastUpdate = ts


class Cost(SensorEntity):
    """Sensor for daily cost."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_has_entity_name = True
    _attr_name = "Cost (today)"
    _attr_native_unit_of_measurement = "GBP"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, hass: HomeAssistant, resource, virtual_entity) -> None:
        self._attr_unique_id = resource.id
        self.hass = hass
        self.initialised = False
        self.meter = None
        self.resource = resource
        self.virtual_entity = virtual_entity
        self.lastUpdate = 0
        self.lastValue = 0

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.meter.resource.id)},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=device_name(self.resource, self.virtual_entity),
        )

    async def async_update(self) -> None:
        dt = datetime.now()
        ts = datetime.timestamp(dt)
        if not self.initialised or (self.lastUpdate + 900) < ts:
            value = await daily_data(self.hass, self.resource, self.lastValue)
            if self.initialised and value < self.lastValue:
                self._attr_native_value = 0
                self.lastValue = 0
            else:
                self._attr_native_value = round(value / 100, 2)
                self.lastValue = value
            self.initialised = True
            self.lastUpdate = ts


class TariffCoordinator(DataUpdateCoordinator):
    """Data update coordinator for the tariff sensors."""

    def __init__(self, hass: HomeAssistant, resource) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="tariff",
            update_interval=timedelta(minutes=5),
        )
        self.resource = resource
        self.lastUpdate = 0

    async def _async_update_data(self):
        dt = datetime.now()
        ts = datetime.timestamp(dt)
        if (self.lastUpdate + 900) < ts:
            tariff = await tariff_data(self.hass, self.resource)
            self.lastUpdate = ts
            return tariff
        return self.data


class Standing(CoordinatorEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_has_entity_name = True
    _attr_name = "Standing charge"
    _attr_native_unit_of_measurement = "GBP"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, resource, virtual_entity) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = resource.id + "-tariff"
        self.resource = resource
        self.virtual_entity = virtual_entity

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.data:
            try:
                value = (
                    float(self.coordinator.data.current_rates.standing_charge.value)
                    / 100
                )
                self._attr_native_value = round(value, 4)
                self.async_write_ha_state()
            except Exception:
                pass

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.resource.id)},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=device_name(self.resource, self.virtual_entity),
        )


class Rate(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_icon = "mdi:cash-multiple"
    _attr_name = "Rate"
    _attr_native_unit_of_measurement = "GBP/kWh"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, resource, virtual_entity) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = resource.id + "-rate"
        self.resource = resource
        self.virtual_entity = virtual_entity

    @callback
    def _handle_coordinator_update(self) -> None:
        if self.coordinator.data:
            try:
                value = float(self.coordinator.data.current_rates.rate.value) / 100
                self._attr_native_value = round(value, 4)
                self.async_write_ha_state()
            except Exception:
                pass

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.resource.id)},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=device_name(self.resource, self.virtual_entity),
        )
