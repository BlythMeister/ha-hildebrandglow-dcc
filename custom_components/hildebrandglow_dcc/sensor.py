"""Platform for sensor integration."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta
import logging

import requests

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


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: Callable
) -> bool:
    """Set up the sensor platform."""
    entities: list = []
    meters: dict = {}

    # Get API object from the config flow
    glowmarkt = hass.data[DOMAIN][entry.entry_id]

    # Gather all virtual entities on the account
    virtual_entities: dict = {}
    try:
        virtual_entities = await hass.async_add_executor_job(
            glowmarkt.get_virtual_entities
        )
        _LOGGER.debug("Successful GET to %svirtualentity", glowmarkt.url)
    except requests.Timeout as ex:
        _LOGGER.error("Timeout: %s", ex)
    except requests.exceptions.ConnectionError as ex:
        _LOGGER.error("Cannot connect: %s", ex)
    # Can't use the RuntimeError exception from the library as it's not a subclass of Exception
    except Exception as ex:  # pylint: disable=broad-except
        if "Request failed" in str(ex):
            _LOGGER.error(
                "Non-200 Status Code. The Glow API may be experiencing issues"
            )
        else:
            _LOGGER.exception("Unexpected exception: %s. Please open an issue", ex)

    for virtual_entity in virtual_entities:
        # Gather all resources for each virtual entity
        resources: dict = {}
        try:
            resources = await hass.async_add_executor_job(virtual_entity.get_resources)
            _LOGGER.debug(
                "Successful GET to %svirtualentity/%s/resources",
                glowmarkt.url,
                virtual_entity.id,
            )
        except requests.Timeout as ex:
            _LOGGER.error("Timeout: %s", ex)
        except requests.exceptions.ConnectionError as ex:
            _LOGGER.error("Cannot connect: %s", ex)
        # Can't use the RuntimeError exception from the library as it's not a subclass of Exception
        except Exception as ex:  # pylint: disable=broad-except
            if "Request failed" in str(ex):
                _LOGGER.error(
                    "Non-200 Status Code. The Glow API may be experiencing issues"
                )
            else:
                _LOGGER.exception("Unexpected exception: %s. Please open an issue", ex)

        _LOGGER.info("Found %i resources:", len(resources))
        for resource in resources:
            _LOGGER.info(" - %s", resource.classifier)
            
        # Loop through all resources and create sensors
        for resource in resources:
            if resource.classifier in ["electricity.consumption", "electricity.export", "gas.consumption"]:
                reading_sensor = Reading(hass, resource, virtual_entity)
                entities.append(reading_sensor)
                # Save the reading sensor as a meter so that the cost sensor can reference it
                meters[resource.classifier] = reading_sensor

        for resource in resources:
            if resource.classifier in ["electricity.consumption", "gas.consumption"]:
                # Standing and Rate sensors are handled by the coordinator
                coordinator = TariffCoordinator(hass, resource)
                standing_sensor = Standing(coordinator, resource, virtual_entity)
                entities.append(standing_sensor)
                rate_sensor = Rate(coordinator, resource, virtual_entity)
                entities.append(rate_sensor)

        # Cost sensors must be created after reading sensors as they reference them as a meter
        for resource in resources:
            if resource.classifier == "gas.consumption.cost":
                cost_sensor = Cost(hass, resource, virtual_entity)
                cost_sensor.meter = meters["gas.consumption"]
                entities.append(cost_sensor)
            elif resource.classifier == "electricity.consumption.cost":
                cost_sensor = Cost(hass, resource, virtual_entity)
                cost_sensor.meter = meters["electricity.consumption"]
                entities.append(cost_sensor)

    # Get data for all entities on initial startup
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
    _LOGGER.error("Unknown classifier: %s. Please open an issue", resource.classifier)
    return "unknown"


def device_name(resource, virtual_entity) -> str:
    """Return device name. Includes name of virtual entity if it exists."""
    supply = supply_type(resource)
    # First letter of device name should be capitalised
    if virtual_entity.name is not None:
        name = f"{virtual_entity.name} smart {supply} meter"
    else:
        name = f"Smart {supply} meter"
    return name

async def daily_data(hass: HomeAssistant, resource, lastValue) -> float:
    """Get daily reading from the API."""
    v = -1.0
    # If it's before 00:45, we need to fetch yesterday's data
    if datetime.now().time() <= time(3, 0):
        _LOGGER.debug("Fetching including yesterday's data until 3am")
        today = datetime.now()
        yesterday = today - timedelta(days=1)
        # Start of yesterday
        t_from = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        # End of yesterday
        t_to = today.replace(second=0, microsecond=0)
    else:
        today = datetime.now()
        # Start of today
        t_from = today.replace(hour=0, minute=0, second=0, microsecond=0)
        # Remove seconds/microseconds
        t_to = today.replace(second=0, microsecond=0)

    # Tell Hildebrand to pull latest DCC data
    try:
        await hass.async_add_executor_job(resource.catchup)
        _LOGGER.debug(
            "Successful GET to https://api.glowmarkt.com/api/v0-1/resource/%s/catchup",
            resource.id,
        )
    except requests.Timeout as ex:
        _LOGGER.error("Timeout: %s", ex)
    except requests.exceptions.ConnectionError as ex:
        _LOGGER.error("Cannot connect: %s", ex)
    # Can't use the RuntimeError exception from the library as it's not a subclass of Exception
    except Exception as ex:  # pylint: disable=broad-except
        if "Request failed" in str(ex):
            _LOGGER.warning(
                "Non-200 Status Code. The Glow API may be experiencing issues"
            )
        else:
            _LOGGER.exception("Unexpected exception: %s. Please open an issue", ex)

    try:
        _LOGGER.debug(
            "Get readings from %s to %s for %s", t_from, t_to, resource.classifier
        )
        readings = await hass.async_add_executor_job(
            resource.get_readings, t_from, t_to, "P1D", "sum", True
        )
        _LOGGER.debug("Successfully got daily reading for resource id %s", resource.id)
        _LOGGER.debug(
            "Readings for %s has %s entries ", resource.classifier, len(readings),
        )

        if len(readings) > 1:
           v = readings[1][1].value
           _LOGGER.debug("using 2nd reading %f:",v)
        elif len(readings) > 0:
           v = readings[0][1].value
           _LOGGER.debug("using 1st reading %f:",v)
        else:
           v= 0.0
           _LOGGER.debug("no readings, using 0.0:")

        if (not isinstance(v,float)):
           v= -1.0
           _LOGGER.debug("value invalid, using 0.0:")
            
        return v

    except requests.Timeout as ex:
        _LOGGER.error("Timeout: %s", ex)
    except requests.exceptions.ConnectionError as ex:
        _LOGGER.error("Cannot connect: %s", ex)
    # Can't use the RuntimeError exception from the library as it's not a subclass of Exception
    except Exception as ex:  # pylint: disable=broad-except
        if "Request failed" in str(ex):
            _LOGGER.warning(
                "Non-200 Status Code. The Glow API may be experiencing issues"
            )
        else:
            _LOGGER.exception("Unexpected exception: %s. Please open an issue", ex)
    
    if v < 0.0:
        return lastValue
    else:
        return v

async def tariff_data(hass: HomeAssistant, resource) -> float:
    """Get tariff data from the API."""
    try:
        tariff = await hass.async_add_executor_job(resource.get_tariff)
        _LOGGER.debug(
            "Successful GET to https://api.glowmarkt.com/api/v0-1/resource/%s/tariff",
            resource.id,
        )
        return tariff
    except UnboundLocalError:
        supply = supply_type(resource)
        _LOGGER.warning(
            "No tariff data found for %s meter (id: %s). If you don't see tariff data for this meter in the Bright app, please disable the associated rate and standing charge sensors",
            supply,
            resource.id,
        )
    except requests.Timeout as ex:
        _LOGGER.error("Timeout: %s", ex)
    except requests.exceptions.ConnectionError as ex:
        _LOGGER.error("Cannot connect: %s", ex)
    # Can't use the RuntimeError exception from the library as it's not a subclass of Exception
    except Exception as ex:  # pylint: disable=broad-except
        if "Request failed" in str(ex):
            _LOGGER.warning(
                "Non-200 Status Code. The Glow API may be experiencing issues"
            )
        else:
            _LOGGER.exception("Unexpected exception: %s. Please open an issue", ex)
    return None


class Reading(SensorEntity):
    """Sensor object for daily reading."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_has_entity_name = True
    _attr_name = "reading (today)"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, hass: HomeAssistant, resource, virtual_entity) -> None:
        """Initialize the sensor."""
        self._attr_unique_id = resource.id

        self.hass = hass
        self.initialised = False
        self.resource = resource
        self.virtual_entity = virtual_entity
        self.lastUpdate = 0
        self.lastValue = 0

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.resource.id)},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=device_name(self.resource, self.virtual_entity),
        )

    @property
    def icon(self) -> str | None:
        """Icon to use in the frontend."""
        # Only the gas reading sensor needs an icon as the others inherit from their device class
        if self.resource.classifier == "gas.consumption":
            return "mdi:fire"
        if self.resource.classifier == "electricity.export":
            return "mdi:transmission-tower-export"

    async def async_update(self) -> None:
        """Fetch new data for the sensor."""
        dt = datetime.now()
        ts = datetime.timestamp(dt)
        # Get data on initial startup
        if not self.initialised:
            value = await daily_data(self.hass, self.resource, self.lastValue)
            self._attr_native_value = round(value, 3)
            self.initialised = True
            self.lastUpdate = ts
            self.lastValue = value
        else:            
            if (self.lastUpdate + 900) < ts:
                value = await daily_data(self.hass, self.resource, self.lastValue)
                if value < self.lastValue:
                    self._attr_native_value = 0
                    self.lastValue = 0
                else:
                    self._attr_native_value = round(value, 3)
                    self.lastUpdate = ts
                    self.lastValue = value

class Cost(SensorEntity):
    """Sensor for daily cost."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_has_entity_name = True
    _attr_name = "Cost (today)"
    _attr_native_unit_of_measurement = "GBP"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, hass: HomeAssistant, resource, virtual_entity) -> None:
        """Initialize the sensor."""
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
        """Return device information."""
        return DeviceInfo(
            # Get the identifier from the meter so that the cost sensors have the same device
            identifiers={(DOMAIN, self.meter.resource.id)},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=device_name(self.resource, self.virtual_entity),
        )

    async def async_update(self) -> None:
        """Fetch new data for the sensor."""
        dt = datetime.now()
        ts = datetime.timestamp(dt)
        if not self.initialised:
            value = await daily_data(self.hass, self.resource, self.lastValue)
            self._attr_native_value = round(value / 100, 2)
            self.initialised = True
            self.lastUpdate = ts
            self.lastValue = value
        else:
            if (self.lastUpdate + 900) < ts:
                value = await daily_data(self.hass, self.resource, self.lastValue)
                if value < self.lastValue:
                    self._attr_native_value = 0
                    self.lastValue = 0
                else:
                    self._attr_native_value = round(value / 100, 2)
                    self.lastUpdate = ts
                    self.lastValue = value

class TariffCoordinator(DataUpdateCoordinator):
    """Data update coordinator for the tariff sensors."""

    def __init__(self, hass: HomeAssistant, resource) -> None:
        """Initialize tariff coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            # Name of the data. For logging purposes.
            name="tariff",
            # Polling interval. Will only be polled if there are subscribers.
            update_interval=timedelta(minutes=5),
        )

        self.rate_initialised = False
        self.standing_initialised = False
        self.resource = resource
        self.lastUpdate = 0

    async def _async_update_data(self):
        """Fetch data from tariff API endpoint."""
        # This needs 2 loops to ensure both the rate and the standing sensors get initial values
        if not self.standing_initialised:
            if not self.rate_initialised:
                self.rate_initialised = True
                return await tariff_data(self.hass, self.resource)
            self.standing_initialised = True
            return await tariff_data(self.hass, self.resource)

        dt = datetime.now()
        ts = datetime.timestamp(dt)
        if (self.lastUpdate + 900) < ts:
            tariff = await tariff_data(self.hass, self.resource)
            self.lastUpdate = ts
            return tariff


class Standing(CoordinatorEntity, SensorEntity):
    """An entity using CoordinatorEntity.

    The CoordinatorEntity class provides:
      should_poll
      async_update
      async_added_to_hass
      available

    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_has_entity_name = True
    _attr_name = "Standing charge"
    _attr_native_unit_of_measurement = "GBP"
    _attr_entity_registry_enabled_default = (
        False  # Don't enable by default as less commonly used
    )

    def __init__(self, coordinator, resource, virtual_entity) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)

        self._attr_unique_id = resource.id + "-tariff"

        self.resource = resource
        self.virtual_entity = virtual_entity

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data:
            value = (
                float(self.coordinator.data.current_rates.standing_charge.value) / 100
            )
            self._attr_native_value = round(value, 4)
            self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.resource.id)},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=device_name(self.resource, self.virtual_entity),
        )


class Rate(CoordinatorEntity, SensorEntity):
    """An entity using CoordinatorEntity.

    The CoordinatorEntity class provides:
      should_poll
      async_update
      async_added_to_hass
      available

    """

    _attr_device_class = None
    _attr_has_entity_name = True
    _attr_icon = (
        "mdi:cash-multiple"  # Need to provide an icon as doesn't have a device class
    )
    _attr_name = "Rate"
    _attr_native_unit_of_measurement = "GBP/kWh"
    _attr_entity_registry_enabled_default = (
        False  # Don't enable by default as less commonly used
    )

    def __init__(self, coordinator, resource, virtual_entity) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)

        self._attr_unique_id = resource.id + "-rate"

        self.resource = resource
        self.virtual_entity = virtual_entity

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data:
            value = float(self.coordinator.data.current_rates.rate.value) / 100
            self._attr_native_value = round(value, 4)
            self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.resource.id)},
            manufacturer="Hildebrand",
            model="Glow (DCC)",
            name=device_name(self.resource, self.virtual_entity),
        )
