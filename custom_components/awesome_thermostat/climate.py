"""Adds support for awesome thermostat units."""
import asyncio
import logging
import math
from datetime import datetime, timedelta
from pytz import UTC as utc

import voluptuous as vol

from homeassistant.components.climate import PLATFORM_SCHEMA, ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_PRESET_MODE,
    CURRENT_HVAC_COOL,
    CURRENT_HVAC_HEAT,
    CURRENT_HVAC_IDLE,
    CURRENT_HVAC_OFF,
    HVAC_MODE_COOL,
    HVAC_MODE_HEAT,
    HVAC_MODE_OFF,
    PRESET_ACTIVITY,
    PRESET_AWAY,
    PRESET_BOOST,
    PRESET_COMFORT,
    PRESET_ECO,
    PRESET_HOME,
    PRESET_NONE,
    PRESET_SLEEP,
    SUPPORT_PRESET_MODE,
    SUPPORT_TARGET_TEMPERATURE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    CONF_NAME,
    CONF_UNIQUE_ID,
    EVENT_HOMEASSISTANT_START,
    PRECISION_HALVES,
    PRECISION_TENTHS,
    PRECISION_WHOLE,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_OFF,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import DOMAIN as HA_DOMAIN, CoreState, callback
from homeassistant.exceptions import ConditionError
from homeassistant.helpers import condition
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
    async_call_later,
)
from homeassistant.helpers.reload import async_setup_reload_service
from homeassistant.helpers.restore_state import RestoreEntity
from voluptuous.schema_builder import Self

from . import DOMAIN, PLATFORMS

_LOGGER = logging.getLogger(__name__)

DEFAULT_TOLERANCE = 0.3
DEFAULT_NAME = "Awesome Thermostat"
DEFAULT_PROPORTIONAL_BIAS = 0.25
DEFAULT_PROPORTIONAL_CYCLE_MIN = 5

ALGO_THRESHOLD = "Threshold"
ALGO_PROPROTIONAL = "Proportional"

PROPORTIONAL_FUNCTION_LINEAR = "Linear"
PROPORTIONAL_FUNCTION_ATAN = "Atan"

CONF_HEATER = "heater"
CONF_SENSOR = "target_sensor"
CONF_WINDOWS_SENSOR = "window_sensor"
CONF_MOTION_SENSOR = "motion_sensor"
CONF_MOTION_MODE = "motion_mode"
CONF_NO_MOTION_MODE = "no_motion_mode"
CONF_MOTION_DELAY = "motion_delay"
CONF_MIN_TEMP = "min_temp"
CONF_MAX_TEMP = "max_temp"
CONF_TARGET_TEMP = "target_temp"
CONF_AC_MODE = "ac_mode"
CONF_MIN_DUR = "min_cycle_duration"
CONF_COLD_TOLERANCE = "cold_tolerance"
CONF_HOT_TOLERANCE = "hot_tolerance"
CONF_KEEP_ALIVE = "keep_alive"
CONF_INITIAL_HVAC_MODE = "initial_hvac_mode"
CONF_PRECISION = "precision"
CONF_ALGORITHM = "algorithm"
## For PROPOTIONAL ALGO ONLY
# The bias in calculation
CONF_PROPORTIONAL_BIAS = "prop_bias"
# The function used to convert delta temp to percentage
CONF_PROPORTIONAL_FUNCTION = "prop_function"
# The cycle in minutes
CONF_PROPORTIONAL_CYCLE_MIN = "prop_cycle_min"
# The power max management config
CONF_PMAX_POWER_SENSOR = "power_sensor"
CONF_PMAX_MAX_POWER_SENSOR = "max_power_sensor"
CONF_PMAX_DEVICE_POWER = "device_power"


## The proportiional Phases
# No phases are running
PROP_PHASE_NONE = "None"
# The radiator (or hvac) is ON and is waiting for the end of the ON cycle
PROP_PHASE_ON = "On"
# The radiator (or hvac) is OFF and is waiting for the end of the OFF cycle
PROP_PHASE_OFF = "Off"
# The minimal duration in sec a radiateur can be On or Off. Else the radiator stays OFF.
PROP_MIN_DURATION_SEC = 10


SUPPORT_FLAGS = SUPPORT_TARGET_TEMPERATURE

CONF_PRESETS = {
    p: f"{p}_temp"
    for p in (
        PRESET_ECO,
        PRESET_AWAY,
        PRESET_BOOST,
        PRESET_COMFORT,
        PRESET_HOME,
        PRESET_SLEEP,
    )
}

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HEATER): cv.entity_id,
        vol.Required(CONF_SENSOR): cv.entity_id,
        vol.Optional(CONF_WINDOWS_SENSOR): cv.entity_id,
        vol.Optional(CONF_MOTION_SENSOR): cv.entity_id,
        vol.Optional(CONF_MOTION_MODE): cv.string,
        vol.Optional(CONF_NO_MOTION_MODE): cv.string,
        vol.Optional(CONF_MOTION_DELAY): cv.positive_time_period,
        vol.Optional(CONF_AC_MODE): cv.boolean,
        vol.Optional(CONF_MAX_TEMP): vol.Coerce(float),
        vol.Optional(CONF_MIN_DUR): cv.positive_time_period,
        vol.Optional(CONF_MIN_TEMP): vol.Coerce(float),
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_COLD_TOLERANCE, default=DEFAULT_TOLERANCE): vol.Coerce(float),
        vol.Optional(CONF_HOT_TOLERANCE, default=DEFAULT_TOLERANCE): vol.Coerce(float),
        vol.Optional(CONF_TARGET_TEMP): vol.Coerce(float),
        vol.Optional(CONF_KEEP_ALIVE): cv.positive_time_period,
        vol.Optional(CONF_INITIAL_HVAC_MODE): vol.In(
            [HVAC_MODE_COOL, HVAC_MODE_HEAT, HVAC_MODE_OFF]
        ),
        vol.Optional(CONF_PRECISION): vol.In(
            [PRECISION_TENTHS, PRECISION_HALVES, PRECISION_WHOLE]
        ),
        vol.Optional(CONF_UNIQUE_ID): cv.string,
        vol.Optional(CONF_ALGORITHM, default=ALGO_THRESHOLD): cv.string,
        # Proportional attributes
        vol.Optional(
            CONF_PROPORTIONAL_BIAS, default=DEFAULT_PROPORTIONAL_BIAS
        ): vol.Coerce(float),
        vol.Optional(
            CONF_PROPORTIONAL_FUNCTION, default=PROPORTIONAL_FUNCTION_LINEAR
        ): cv.string,
        vol.Optional(
            CONF_PROPORTIONAL_CYCLE_MIN, default=DEFAULT_PROPORTIONAL_CYCLE_MIN
        ): vol.Coerce(float),
        # Power max management attributes
        vol.Optional(CONF_PMAX_MAX_POWER_SENSOR): cv.entity_id,
        vol.Optional(CONF_PMAX_POWER_SENSOR): cv.entity_id,
        vol.Optional(CONF_PMAX_DEVICE_POWER): vol.Coerce(float),
    }
).extend({vol.Optional(v): vol.Coerce(float) for (k, v) in CONF_PRESETS.items()})


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the awesome thermostat platform."""

    await async_setup_reload_service(hass, DOMAIN, PLATFORMS)

    name = config.get(CONF_NAME)
    heater_entity_id = config.get(CONF_HEATER)
    temperature_entity_id = config.get(CONF_SENSOR)
    windows_entity_id = config.get(CONF_WINDOWS_SENSOR)
    min_temp = config.get(CONF_MIN_TEMP)
    max_temp = config.get(CONF_MAX_TEMP)
    target_temp = config.get(CONF_TARGET_TEMP)
    ac_mode = config.get(CONF_AC_MODE)
    min_cycle_duration = config.get(CONF_MIN_DUR)
    cold_tolerance = config.get(CONF_COLD_TOLERANCE)
    hot_tolerance = config.get(CONF_HOT_TOLERANCE)
    keep_alive = config.get(CONF_KEEP_ALIVE)
    initial_hvac_mode = config.get(CONF_INITIAL_HVAC_MODE)
    presets = {}
    for (key, value) in CONF_PRESETS.items():
        if value in config:
            presets[key] = config.get(value)
    motion_entity_id = config.get(CONF_MOTION_SENSOR)
    motion_mode = config.get(CONF_MOTION_MODE)
    no_motion_mode = config.get(CONF_NO_MOTION_MODE)
    motion_delay = config.get(CONF_MOTION_DELAY)
    precision = config.get(CONF_PRECISION)
    unit = hass.config.units.temperature_unit
    unique_id = config.get(CONF_UNIQUE_ID)
    algorithm = config.get(CONF_ALGORITHM)
    prop_bias = config.get(CONF_PROPORTIONAL_BIAS)
    prop_function = config.get(CONF_PROPORTIONAL_FUNCTION)
    prop_cycle_min = config.get(CONF_PROPORTIONAL_CYCLE_MIN)
    pmax_max_power_sensor_entity_id = config.get(CONF_PMAX_MAX_POWER_SENSOR)
    pmax_power_sensor_entity_id = config.get(CONF_PMAX_POWER_SENSOR)
    pmax_device_power = config.get(CONF_PMAX_DEVICE_POWER)

    async_add_entities(
        [
            AwesomeThermostat(
                name,
                heater_entity_id,
                temperature_entity_id,
                windows_entity_id,
                motion_entity_id,
                motion_mode,
                no_motion_mode,
                motion_delay,
                min_temp,
                max_temp,
                target_temp,
                ac_mode,
                min_cycle_duration,
                cold_tolerance,
                hot_tolerance,
                keep_alive,
                initial_hvac_mode,
                presets,
                precision,
                unit,
                unique_id,
                algorithm,
                prop_bias,
                prop_function,
                prop_cycle_min,
                pmax_max_power_sensor_entity_id,
                pmax_power_sensor_entity_id,
                pmax_device_power,
            )
        ]
    )


class AwesomeThermostat(ClimateEntity, RestoreEntity):
    """Representation of a Awesome Thermostat device."""

    def __init__(
        self,
        name,
        heater_entity_id,
        temperature_entity_id,
        windows_entity_id,
        motion_entity_id,
        motion_mode,
        no_motion_mode,
        motion_delay,
        min_temp,
        max_temp,
        target_temp,
        ac_mode,
        min_cycle_duration,
        cold_tolerance,
        hot_tolerance,
        keep_alive,
        initial_hvac_mode,
        presets,
        precision,
        unit,
        unique_id,
        algorithm,
        prop_bias,
        prop_function,
        prop_cycle_min,
        pmax_max_power_sensor_entity_id,
        pmax_power_sensor_entity_id,
        pmax_device_power,
    ):
        """Initialize the thermostat."""
        self._name = name
        self.heater_entity_id = heater_entity_id
        self.temperature_entity_id = temperature_entity_id
        self.windows_entity_id = windows_entity_id
        self.motion_entity_id = motion_entity_id
        self.motion_mode = motion_mode
        self.no_motion_mode = no_motion_mode
        self.motion_delay = motion_delay
        self.support_motion_control = False
        if (
            self.motion_entity_id
            and self.motion_mode
            and self.motion_mode in presets.keys()
            and self.no_motion_mode
            and self.no_motion_mode in presets.keys()
            and self.motion_delay
        ):
            self.support_motion_control = True
            presets[PRESET_ACTIVITY] = presets[no_motion_mode]
        self.ac_mode = ac_mode
        self.min_cycle_duration = min_cycle_duration
        self._cold_tolerance = cold_tolerance
        self._hot_tolerance = hot_tolerance
        self._keep_alive = keep_alive
        self._hvac_mode = initial_hvac_mode
        self._saved_hvac_mode = self._hvac_mode
        self._saved_target_temp = target_temp or next(iter(presets.values()), None)
        self._temp_precision = precision
        if self.ac_mode:
            self._hvac_list = [HVAC_MODE_COOL, HVAC_MODE_OFF]
        else:
            self._hvac_list = [HVAC_MODE_HEAT, HVAC_MODE_OFF]
        self._active = False
        self._cur_temp = None
        self._temp_lock = asyncio.Lock()
        self._min_temp = min_temp
        self._max_temp = max_temp
        self._attr_preset_mode = PRESET_NONE
        self._target_temp = target_temp
        self._unit = unit
        self._unique_id = unique_id
        self._support_flags = SUPPORT_FLAGS
        if len(presets):
            self._support_flags = SUPPORT_FLAGS | SUPPORT_PRESET_MODE
            self._attr_preset_modes = [PRESET_NONE] + list(presets.keys())
        else:
            self._attr_preset_modes = [PRESET_NONE]
        self._presets = presets

        self.algorithm = algorithm
        self.prop_bias = prop_bias
        self.prop_function = prop_function
        self.prop_cycle_min = prop_cycle_min
        if self.algorithm == ALGO_PROPROTIONAL:
            self.prop_current_phase = PROP_PHASE_NONE
            self.prop_end_phase_time = None
            self.prop_on_time_sec = None
            self.prop_off_time_sec = None

            _LOGGER.info(
                "Used algorithm for %s is %s with bias %f, function %s and cycle %f minutes",
                self._name,
                self.algorithm,
                self.prop_bias,
                self.prop_function,
                self.prop_cycle_min,
            )
        self.pmax_max_power_sensor_entity_id = pmax_max_power_sensor_entity_id
        self.pmax_power_sensor_entity_id = pmax_power_sensor_entity_id
        self.pmax_device_power = pmax_device_power
        if (
            self.pmax_max_power_sensor_entity_id
            and self.pmax_power_sensor_entity_id
            and self.pmax_device_power
        ):
            self._pmax_on = True
            self._current_power = -1
            self._current_power_max = -1
        else:
            self._pmax_on = False

    async def async_added_to_hass(self):
        """Run when entity about to be added."""
        await super().async_added_to_hass()

        # Add listener
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.temperature_entity_id], self._async_temperature_changed
            )
        )
        if self.windows_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self.windows_entity_id], self._async_windows_changed
                )
            )
        if self.support_motion_control:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, [self.motion_entity_id], self._async_motion_changed
                )
            )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass, [self.heater_entity_id], self._async_switch_changed
            )
        )

        if self._keep_alive:
            self.async_on_remove(
                async_track_time_interval(
                    self.hass, self._async_control_heating, self._keep_alive
                )
            )

        if self.pmax_max_power_sensor_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self.pmax_max_power_sensor_entity_id],
                    self._async_pmax_max_power_changed,
                )
            )

        if self.pmax_power_sensor_entity_id:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass,
                    [self.pmax_power_sensor_entity_id],
                    self._async_pmax_power_changed,
                )
            )

        @callback
        def _async_startup(*_):
            """Init on startup."""
            temperature_state = self.hass.states.get(self.temperature_entity_id)
            if temperature_state and temperature_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                self._async_update_temp(temperature_state)
                self.async_write_ha_state()
            switch_state = self.hass.states.get(self.heater_entity_id)
            if switch_state and switch_state.state not in (
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                self.hass.create_task(self._check_switch_initial_state())

            if self._pmax_on:
                # try to acquire current power and power max
                current_power_state = self.hass.states.get(
                    self.pmax_power_sensor_entity_id
                )
                if current_power_state and current_power_state.state not in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                ):
                    self._current_power = float(current_power_state.state)
                    _LOGGER.debug(
                        "Current power have been retrieved: %f", self._current_power
                    )
                    self.async_write_ha_state()
                current_power_max_state = self.hass.states.get(
                    self.pmax_max_power_sensor_entity_id
                )
                if current_power_max_state and current_power_max_state.state not in (
                    STATE_UNAVAILABLE,
                    STATE_UNKNOWN,
                ):
                    self._current_power_max = float(current_power_max_state.state)
                    _LOGGER.debug(
                        "Current power max have been retrieved: %f",
                        self._current_power_max,
                    )
                    self.async_write_ha_state()

            self.hass.create_task(self._async_control_heating())

        if self.hass.state == CoreState.running:
            _async_startup()
        else:
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, _async_startup)

        # Check If we have an old state
        old_state = await self.async_get_last_state()
        if old_state is not None:
            # If we have no initial temperature, restore
            if self._target_temp is None:
                # If we have a previously saved temperature
                if old_state.attributes.get(ATTR_TEMPERATURE) is None:
                    if self.ac_mode:
                        self._target_temp = self.max_temp
                    else:
                        self._target_temp = self.min_temp
                    _LOGGER.warning(
                        "Undefined target temperature, falling back to %s",
                        self._target_temp,
                    )
                else:
                    self._target_temp = float(old_state.attributes[ATTR_TEMPERATURE])
            if old_state.attributes.get(ATTR_PRESET_MODE) in self._attr_preset_modes:
                self._attr_preset_mode = old_state.attributes.get(ATTR_PRESET_MODE)
            if not self._hvac_mode and old_state.state:
                self._hvac_mode = old_state.state

        else:
            # No previous state, try and restore defaults
            if self._target_temp is None:
                if self.ac_mode:
                    self._target_temp = self.max_temp
                else:
                    self._target_temp = self.min_temp
            _LOGGER.warning(
                "No previously saved temperature, setting to %s", self._target_temp
            )

        # Set default state to off
        if not self._hvac_mode:
            self._hvac_mode = HVAC_MODE_OFF

    @property
    def should_poll(self):
        """Return the polling state."""
        return False

    @property
    def name(self):
        """Return the name of the thermostat."""
        return self._name

    @property
    def unique_id(self):
        """Return the unique id of this thermostat."""
        return self._unique_id

    @property
    def precision(self):
        """Return the precision of the system."""
        if self._temp_precision is not None:
            return self._temp_precision
        return super().precision

    @property
    def target_temperature_step(self):
        """Return the supported step of target temperature."""
        # Since this integration does not yet have a step size parameter
        # we have to re-use the precision as the step size for now.
        return self.precision

    @property
    def temperature_unit(self):
        """Return the unit of measurement."""
        return self._unit

    @property
    def current_temperature(self):
        """Return the sensor temperature."""
        return self._cur_temp

    @property
    def hvac_mode(self):
        """Return current operation."""
        return self._hvac_mode

    @property
    def hvac_action(self):
        """Return the current running hvac operation if supported.

        Need to be one of CURRENT_HVAC_*.
        """
        if self._hvac_mode == HVAC_MODE_OFF:
            return CURRENT_HVAC_OFF
        if not self._is_device_active:
            return CURRENT_HVAC_IDLE
        if self.ac_mode:
            return CURRENT_HVAC_COOL
        return CURRENT_HVAC_HEAT

    @property
    def target_temperature(self):
        """Return the temperature we try to reach."""
        return self._target_temp

    @property
    def hvac_modes(self):
        """List of available operation modes."""
        return self._hvac_list

    async def async_set_hvac_mode(self, hvac_mode):
        """Set hvac mode."""
        _LOGGER.info("Thermostat %s - Set hvac mode: %s", self.name, hvac_mode)
        if hvac_mode == HVAC_MODE_HEAT:
            self._hvac_mode = HVAC_MODE_HEAT
            await self._async_control_heating(force=True)
        elif hvac_mode == HVAC_MODE_COOL:
            self._hvac_mode = HVAC_MODE_COOL
            await self._async_control_heating(force=True)
        elif hvac_mode == HVAC_MODE_OFF:
            self._hvac_mode = HVAC_MODE_OFF
            self.prop_current_phase = PROP_PHASE_NONE
            if self._is_device_active:
                await self._async_heater_turn_off()
        else:
            _LOGGER.error("Unrecognized hvac mode: %s", hvac_mode)
            return
        # Ensure we update the current operation after changing the mode
        self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs):
        """Set new target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        self._target_temp = temperature
        self._attr_preset_mode = PRESET_NONE
        await self._async_control_heating(force=True)
        self.async_write_ha_state()

    @property
    def min_temp(self):
        """Return the minimum temperature."""
        if self._min_temp is not None:
            return self._min_temp

        # get default temp from super class
        return super().min_temp

    @property
    def max_temp(self):
        """Return the maximum temperature."""
        if self._max_temp is not None:
            return self._max_temp

        # Get default temp from super class
        return super().max_temp

    @callback
    async def _async_temperature_changed(self, event):
        """Handle temperature changes."""
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return

        self._async_update_temp(new_state)
        await self._async_control_heating()
        self.async_write_ha_state()

    @callback
    async def _async_windows_changed(self, event):
        """Handle window changes."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None or old_state is None or new_state.state == old_state.state:
            return
        if not self._saved_hvac_mode:
            self._saved_hvac_mode = self._hvac_mode
        if new_state.state == STATE_OFF:
            await self.async_set_hvac_mode(self._saved_hvac_mode)
        elif new_state.state == STATE_ON:
            self._saved_hvac_mode = self._hvac_mode
            await self.async_set_hvac_mode(HVAC_MODE_OFF)
        else:
            return

    @callback
    async def _async_motion_changed(self, event):
        """Handle motion changes."""
        _LOGGER.info(
            "Motion changed. Event.new_state is %s, _attr_preset_mode=%s, activity=%s",
            event.data.get("new_state"),
            self._attr_preset_mode,
            PRESET_ACTIVITY,
        )
        if self._attr_preset_mode != PRESET_ACTIVITY:
            return
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state not in (STATE_OFF, STATE_ON):
            return

        if self.motion_delay:
            if new_state.state == STATE_ON:
                self._target_temp = self._presets[self.motion_mode]
                await self._async_control_heating()
                self.async_write_ha_state()
            else:

                async def try_no_motion_condition(_):
                    if self._attr_preset_mode != PRESET_ACTIVITY:
                        return
                    try:
                        long_enough = condition.state(
                            self.hass,
                            self.motion_entity_id,
                            STATE_OFF,
                            self.motion_delay,
                        )
                    except ConditionError:
                        long_enough = False
                    if long_enough:
                        self._target_temp = self._presets[self.no_motion_mode]
                        await self._async_control_heating()
                        self.async_write_ha_state()

                async_call_later(self.hass, self.motion_delay, try_no_motion_condition)

    @callback
    async def _check_switch_initial_state(self):
        """Prevent the device from keep running if HVAC_MODE_OFF."""
        if self._hvac_mode == HVAC_MODE_OFF and self._is_device_active:
            _LOGGER.warning(
                "The climate mode is OFF, but the switch device is ON. Turning off device %s",
                self.heater_entity_id,
            )
            await self._async_heater_turn_off()

    @callback
    def _async_switch_changed(self, event):
        """Handle heater switch state changes."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None:
            return
        if old_state is None:
            self.hass.create_task(self._check_switch_initial_state())
        self.async_write_ha_state()

    @callback
    def _async_update_temp(self, state):
        """Update thermostat with latest state from sensor."""
        try:
            cur_temp = float(state.state)
            if math.isnan(cur_temp) or math.isinf(cur_temp):
                raise ValueError(f"Sensor has illegal state {state.state}")
            self._cur_temp = cur_temp
        except ValueError as ex:
            _LOGGER.error("Unable to update temperature from sensor: %s", ex)

    @callback
    async def _async_pmax_power_changed(self, event):
        """Handle power changes."""
        _LOGGER.debug("Thermostat %s - Receive new Power event", self.name)
        _LOGGER.debug(event)
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None or new_state.state == old_state.state:
            return

        try:
            current_power = float(new_state.state)
            if math.isnan(current_power) or math.isinf(current_power):
                raise ValueError(f"Sensor has illegal state {new_state.state}")
            self._current_power = current_power

        except ValueError as ex:
            _LOGGER.error("Unable to update current_power from sensor: %s", ex)
        # To avoid starting all heaters at the same time we don't force and we wait for next cycle
        # await self._async_control_heating(force=False)
        # self.async_write_ha_state()

    async def _async_pmax_max_power_changed(self, event):
        """Handle power max changes."""
        _LOGGER.debug("Thermostat %s - Receive new Power Max event", self.name)
        _LOGGER.debug(event)
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None or new_state.state == old_state.state:
            return

        try:
            current_power_max = float(new_state.state)
            if math.isnan(current_power_max) or math.isinf(current_power_max):
                raise ValueError(f"Sensor has illegal state {new_state.state}")
            self._current_power_max = current_power_max

        except ValueError as ex:
            _LOGGER.error("Unable to update current_power from sensor: %s", ex)
        # To avoid starting all heaters at the same time we don't force and we wait for next cycle
        # await self._async_control_heating(force=False)
        # self.async_write_ha_state()

    async def _async_control_heating(self, time=None, force=False):
        """Check if we need to turn heating on or off."""
        async with self._temp_lock:
            if not self._active and None not in (
                self._cur_temp,
                self._target_temp,
            ):
                self._active = True
                _LOGGER.info(
                    "Thermostat %s - Obtained current and target temperature. "
                    "Awesome thermostat active. %s, %s",
                    self.name,
                    self._cur_temp,
                    self._target_temp,
                )

            if not self._active or self._hvac_mode == HVAC_MODE_OFF:
                _LOGGER.info("Thermostat %s - Mode is OFF or inactive", self.name)
                return

            # If the `force` argument is True, we
            # ignore `min_cycle_duration`.
            # If the `time` argument is not none, we were invoked for
            # keep-alive purposes, and `min_cycle_duration` is irrelevant.
            if not force and time is None and self.min_cycle_duration:
                if self._is_device_active:
                    current_state = STATE_ON
                else:
                    current_state = HVAC_MODE_OFF
                try:
                    long_enough = condition.state(
                        self.hass,
                        self.heater_entity_id,
                        current_state,
                        self.min_cycle_duration,
                    )
                except ConditionError:
                    long_enough = False

                if not long_enough:
                    return

            if self.algorithm == ALGO_THRESHOLD:
                await self._async_control_heating_threshold(time, force)

            elif self.algorithm == ALGO_PROPROTIONAL:
                await self._async_control_heating_proportional(time, force)

    def check_power_exceeded(self):
        power_exceeded = False
        if (
            self._pmax_on
            and self._current_power != -1
            and self._current_power_max != -1
        ):
            _LOGGER.debug(
                "Thermostat %s - Power management is on. current_power: %f, power_max=%f, heater_power=%f",
                self.name,
                self._current_power,
                self._current_power_max,
                self.pmax_device_power,
            )
            if self._current_power + self.pmax_device_power >= self._current_power_max:
                _LOGGER.warning(
                    "Thermostat %s - Max power will be exceeded. No heating period calculated",
                    self.name,
                )
                power_exceeded = True
        return power_exceeded

    async def _async_control_heating_threshold(self, time, force):
        """Handle control heating in Threshold mode"""
        _LOGGER.debug("Threshold mode")
        power_exceeded = self.check_power_exceeded()
        too_cold = self._target_temp >= self._cur_temp + self._cold_tolerance
        too_hot = self._cur_temp >= self._target_temp + self._hot_tolerance
        if self._is_device_active:
            if (
                (self.ac_mode and too_cold)
                or (not self.ac_mode and too_hot)
                or power_exceeded
            ):
                _LOGGER.info("Turning off heater %s", self.heater_entity_id)
                await self._async_heater_turn_off()
            elif time is not None:
                # The time argument is passed only in keep-alive case
                _LOGGER.info(
                    "Keep-alive - Thermostat %s - Turning on heater heater %s",
                    self.name,
                    self.heater_entity_id,
                )
                await self._async_heater_turn_on()
        else:
            if not power_exceeded and (
                (self.ac_mode and too_hot) or (not self.ac_mode and too_cold)
            ):
                _LOGGER.info("Turning on heater %s", self.heater_entity_id)
                await self._async_heater_turn_on()
            elif time is not None:
                # The time argument is passed only in keep-alive case
                _LOGGER.info(
                    "Keep-alive - Thermostat %s - Turning off heater %s",
                    self.name,
                    self.heater_entity_id,
                )
                await self._async_heater_turn_off()

    def calculate_proportional(self):
        """Calculate the proportional parameters value (for Proportional algorithm"""
        _LOGGER.debug("Calculate proportional parameters")
        power_exceeded = self.check_power_exceeded()

        if not power_exceeded:
            delta_temp = self._target_temp - self._cur_temp
            if self.prop_function == PROPORTIONAL_FUNCTION_LINEAR:
                on_percent = 0.25 * delta_temp + self.prop_bias
            elif self.prop_function == PROPORTIONAL_FUNCTION_ATAN:
                on_percent = math.atan(delta_temp + self.prop_bias) / 1.4
            else:
                _LOGGER.warning(
                    "Thermostat %s - Proportional algorithm: unknown %s function. Heating is disabled",
                    self.name,
                    self.prop_function,
                )
                on_percent = 0
        else:
            _LOGGER.info("Thermostat %s - power is exceeded", self.name)
            on_percent = 0

        # calculated on_time duration in seconds
        if on_percent > 1:
            on_percent = 1
        self.prop_on_time_sec = on_percent * self.prop_cycle_min * 60

        # Do not heat for less than xx sec
        if self.prop_on_time_sec < PROP_MIN_DURATION_SEC:
            _LOGGER.info(
                "Thermostat %s - no heating period due to heating period too small (%f < %f)",
                self.name,
                self.prop_on_time_sec,
                PROP_MIN_DURATION_SEC,
            )
            self.prop_on_time_sec = 0

        self.prop_off_time_sec = (1.0 - on_percent) * self.prop_cycle_min * 60

        _LOGGER.info(
            "Thermostat %s - Proportional algorithm: heating percent calculated is %f, on_time is %f (sec), off_time is %s (sec)"
            self.name,
            on_percent,
            self.prop_on_time_sec,
            self.prop_off_time_sec
        )

    async def _async_control_heating_proportional(self, time, force):
        """Handle control heating in Proportional mode"""

        _LOGGER.debug(
            "Thermostat %s - Proportional mode. Current phase is %s, time is %s, force is %s",
            self.name,
            self.prop_current_phase,
            time,
            force,
        )

        # Remove timezone of time if any to be able to compare
        # if time:
        #    keep_alive = True
        # else:
        #    keep_alive = False

        async def start_off_cycle(_):
            """Local function to start the Off cycle"""
            _LOGGER.debug("Thermostat %s - Into start_off_cycle", self.name)
            now = datetime.now(utc)

            await self._async_heater_turn_off()
            self.prop_current_phase = PROP_PHASE_OFF

            self.prop_end_phase_time = now + timedelta(seconds=self.prop_off_time_sec)
            _LOGGER.info(
                "Thermostat %s - End of heating period for %f sec until %s (phase=%s)",
                self.name,
                self.prop_off_time_sec,
                self.prop_end_phase_time,
                self.prop_current_phase,
            )

            async_call_later(
                self.hass,
                self.prop_off_time_sec,
                start_cycle,
            )

        async def start_on_cycle(_):
            """Local function to start the On cycle"""
            _LOGGER.debug("Thermostat %s - Into start_on_cycle", self.name)

            now = datetime.now(utc)

            await self._async_heater_turn_on()
            self.prop_current_phase = PROP_PHASE_ON

            self.prop_end_phase_time = now + timedelta(seconds=self.prop_on_time_sec)
            _LOGGER.info(
                "Thermostat %s - Start of heating period for %f sec until %s (phase=%s)",
                self.name,
                self.prop_on_time_sec,
                self.prop_end_phase_time,
                self.prop_current_phase,
            )

            async_call_later(
                self.hass,
                self.prop_on_time_sec,
                start_cycle,
            )

        async def start_cycle(_):
            _LOGGER.debug("Thermostat %s - Into start_cycle", self.name)

            time = datetime.now(utc)

            if not self._active or self._hvac_mode == HVAC_MODE_OFF:
                _LOGGER.info("Thermostat %s - Thermostat is stopped. Stopping radiator", self.name)
                await self._async_heater_turn_off()
                self.prop_current_phase = PROP_PHASE_NONE
                return

            if self.prop_current_phase == PROP_PHASE_NONE:
                _LOGGER.debug("Into PHASE_NONE or force")
                self.calculate_proportional()
                # Do not heat for less than 30 sec
                if self.prop_on_time_sec <= 0:
                    if self._is_device_active:
                        _LOGGER.info("Thermostat %s - Stopping radiator", self.name)
                        await self._async_heater_turn_off()
                    else:
                        _LOGGER.debug("Radiator is not active. Nothing to do")
                else:
                    await start_on_cycle(None)
            elif (
                self.prop_current_phase == PROP_PHASE_ON
                and time >= self.prop_end_phase_time
            ):
                _LOGGER.debug("Into PHASE_ON")
                await start_off_cycle(None)
            elif (
                self.prop_current_phase == PROP_PHASE_NONE or
                (self.prop_current_phase == PROP_PHASE_OFF
                and time >= self.prop_end_phase_time)
                and self._active
                and self._hvac_mode != HVAC_MODE_OFF
            ):
                _LOGGER.debug("Into PHASE_OFF")
                self.calculate_proportional()
                if self.prop_on_time_sec > 0:
                    await start_on_cycle(None)
                else:
                    await start_off_cycle(None)
            else:
                _LOGGER.debug(
                    "Thermostat %s - nothing to do. Waiting %s",
                    self.name,
                    self.prop_end_phase_time,
                )

        await start_cycle(None)

    @property
    def _is_device_active(self):
        """If the toggleable device is currently active."""
        if not self.hass.states.get(self.heater_entity_id):
            return None

        return self.hass.states.is_state(self.heater_entity_id, STATE_ON)

    @property
    def supported_features(self):
        """Return the list of supported features."""
        return self._support_flags

    async def _async_heater_turn_on(self):
        """Turn heater toggleable device on."""
        data = {ATTR_ENTITY_ID: self.heater_entity_id}
        await self.hass.services.async_call(
            HA_DOMAIN, SERVICE_TURN_ON, data, context=self._context
        )

    async def _async_heater_turn_off(self):
        """Turn heater toggleable device off."""
        data = {ATTR_ENTITY_ID: self.heater_entity_id}
        await self.hass.services.async_call(
            HA_DOMAIN, SERVICE_TURN_OFF, data, context=self._context
        )

    async def async_set_preset_mode(self, preset_mode: str):
        """Set new preset mode."""
        if preset_mode not in (self._attr_preset_modes or []):
            raise ValueError(
                f"Got unsupported preset_mode {preset_mode}. Must be one of {self._attr_preset_modes}"
            )
        if preset_mode == self._attr_preset_mode:
            # I don't think we need to call async_write_ha_state if we didn't change the state
            return
        if preset_mode == PRESET_NONE:
            self._attr_preset_mode = PRESET_NONE
            self._target_temp = self._saved_target_temp
            await self._async_control_heating(force=True)
        elif preset_mode == PRESET_ACTIVITY:
            self._attr_preset_mode = PRESET_ACTIVITY
            self._target_temp = self._presets[self.no_motion_mode]
            await self._async_control_heating(force=True)
        else:
            if self._attr_preset_mode == PRESET_NONE:
                self._saved_target_temp = self._target_temp
            self._attr_preset_mode = preset_mode
            self._target_temp = self._presets[preset_mode]
            await self._async_control_heating(force=True)

        self.async_write_ha_state()
