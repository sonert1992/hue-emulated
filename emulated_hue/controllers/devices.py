"""Collection of devices controllable by Hue."""
import logging
from datetime import datetime

from emulated_hue import const
from emulated_hue.config import Config
from emulated_hue.utils import clamp

from .homeassistant import HomeAssistantController
from .models import ALL_STATES, DeviceState
from .scheduler import add_scheduler

LOGGER = logging.getLogger(__name__)

__device_cache = {}


async def async_get_hass_state(
    ctrl_hass: HomeAssistantController, entity_id: str
) -> dict:
    """Get Home Assistant state for entity."""
    return await ctrl_hass.async_get_entity_state(entity_id)


class OnOffDevice:
    """OnOffDevice class."""

    def __init__(
        self,
        ctrl_hass: HomeAssistantController,
        ctrl_config: Config,
        light_id: str,
        entity_id: str,
        config: dict,
        hass_state_dict: dict,
    ):
        """Initialize OnOffDevice."""
        self._ctrl_hass: HomeAssistantController = ctrl_hass
        self._ctrl_config: Config = ctrl_config
        self._light_id = light_id
        self._entity_id = entity_id

        self._hass_state_dict: dict = hass_state_dict  # state from Home Assistant

        self._config: dict = config
        self._name = self._config.get("name", "")

        # throttling
        self._throttle_ms: int | None = self._config.get("throttle")
        self._last_update: float = datetime.now().timestamp()
        self._default_transition: float = const.DEFAULT_TRANSITION_SECONDS
        if self._throttle_ms > self._default_transition:
            self._default_transition = self._throttle_ms / 1000

        self._hass_state: None | DeviceState = None  # DeviceState from Home Assistant
        self._control_state: None | DeviceState = None  # Control state
        self._config_state: None | DeviceState = (
            None  # Latest state and stored in config
        )

    @property
    def name(self) -> str:
        """Return device name, prioritizing local config."""
        return self._name or self._hass_state_dict.get("attributes", {}).get("friendly_name")

    @property
    def reachable(self) -> bool:
        """Return if device is reachable."""
        return self._config_state.reachable

    @property
    def power_state(self) -> bool:
        """Return power state."""
        return self._config_state.power_state

    @property
    def light_id(self) -> str:
        """Return light id."""
        return self._light_id

    @property
    def entity_id(self) -> str:
        """Return entity id."""
        return self._entity_id

    async def _async_save_config(self) -> None:
        """Save config to file."""
        await self._ctrl_config.async_set_storage_value(
            "lights", self._light_id, self._config
        )

    async def _async_update_config_states(self) -> None:
        """Update config states."""
        save_state = {}
        for state in ALL_STATES:
            # prioritize state from hass, then last command, then last saved state
            if self._hass_state and getattr(self._hass_state, state) is not None:
                best_value = getattr(self._hass_state, state)
            elif (
                self._control_state and getattr(self._control_state, state) is not None
            ):
                best_value = getattr(self._control_state, state)
            else:
                best_value = self._config.get("hass_state", {}).get(state, None)
            save_state[state] = best_value

        self._config["hass_state"] = save_state
        self._config_state = DeviceState(**save_state)
        await self._async_save_config()

    def _update_device_state(self, full_update: bool) -> None:
        """Update DeviceState object."""
        if full_update:
            self._hass_state = DeviceState(
                power_state=self._hass_state_dict["state"] == const.HASS_STATE_ON,
                reachable=self._hass_state_dict["state"]
                != const.HASS_STATE_UNAVAILABLE,
            )

    async def _async_update_allowed(self) -> bool:
        """Check if update is allowed using basic throttling, only update every throttle_ms."""
        if self._throttle_ms is None or self._throttle_ms == 0:
            return True
        # if wanted state is equal to the current state, dont change
        if self._config_state == self._control_state:
            return False
        # if the last update was less than the throttle time ago, dont change
        now_timestamp = datetime.now().timestamp()
        if now_timestamp - self._last_update < self._throttle_ms / 1000:
            return False

        self._last_update = now_timestamp
        return True

    def _new_control_state(self) -> DeviceState:
        """Create new control state based on last known power state."""
        return DeviceState(power_state=self._config_state.power_state)

    async def async_update_state(self, full_update: bool = True) -> None:
        """Update DeviceState object with Hass state."""
        self._hass_state_dict = await async_get_hass_state(
            self._ctrl_hass, self._entity_id
        )
        # Cascades up the inheritance chain to update the state
        self._update_device_state(full_update)
        await self._async_update_config_states()

    @property
    def transition_seconds(self) -> float:
        """Return transition seconds."""
        return self._config_state.transition_seconds

    def set_transition_ms(self, transition_ms: float) -> None:
        """Set transition in milliseconds."""
        if not self._control_state:
            self._control_state = self._new_control_state()
        if transition_ms < self._throttle_ms:
            transition_ms = self._throttle_ms
        self._config_state.transition_seconds = transition_ms / 1000

    def set_transition_seconds(self, transition_seconds: float) -> None:
        """Set transition in seconds."""
        self.set_transition_ms(transition_seconds * 1000)

    def turn_on(self) -> None:
        """Turn on light."""
        self._control_state = DeviceState(
            power_state=True, transition_seconds=self._default_transition
        )

    def turn_off(self) -> None:
        """Turn off light."""
        self._control_state = DeviceState(
            power_state=False, transition_seconds=self._default_transition
        )

    async def async_execute(self) -> None:
        """Execute control state."""
        if not await self._async_update_allowed():
            self._control_state = None
            return
        if self._control_state:
            if self._control_state.power_state:
                await self._ctrl_hass.async_turn_on(
                    self._entity_id, self._control_state.to_hass_data()
                )
            else:
                await self._ctrl_hass.async_turn_off(
                    self._entity_id, self._control_state.to_hass_data()
                )
        else:
            LOGGER.warning("No state to execute for device %s", self._entity_id)
        await self._async_update_config_states()
        self._control_state = None


class BrightnessDevice(OnOffDevice):
    """BrightnessDevice class."""

    def __init__(
        self,
        ctrl_hass: HomeAssistantController,
        ctrl_config: Config,
        light_id: str,
        entity_id: str,
        config: dict,
        hass_state_dict: dict,
    ):
        """Initialize BrightnessDevice."""
        super().__init__(
            ctrl_hass, ctrl_config, light_id, entity_id, config, hass_state_dict
        )

    def _update_device_state(self, full_update: bool) -> None:
        """Update DeviceState object."""
        super()._update_device_state(full_update)
        self._hass_state.brightness = self._hass_state_dict.get(
            const.HASS_ATTR, {}
        ).get(const.HASS_ATTR_BRIGHTNESS)

    @property
    def brightness(self) -> int:
        """Return brightness."""
        return self._config_state.brightness

    def set_brightness(self, brightness: int) -> None:
        """Set brightness."""
        if not self._control_state:
            self._control_state = self._new_control_state()
        self._control_state.brightness = clamp(brightness, 0, 255)

    @property
    def effect(self) -> str:
        """Return effect."""
        return self._config_state.effect

    def set_effect(self, effect: str) -> None:
        """Set effect."""
        if not self._control_state:
            self._control_state = self._new_control_state()
        self._control_state.effect = effect

    @property
    def flash_state(self) -> str:
        """Return flash state."""
        return self._config_state.flash_state

    def set_flash(self, flash: str) -> None:
        """Set flash."""
        if not self._control_state:
            self._control_state = self._new_control_state()
        self._control_state.flash = flash


class CTDevice(BrightnessDevice):
    """CTDevice class."""

    def __init__(
        self,
        ctrl_hass: HomeAssistantController,
        ctrl_config: Config,
        light_id: str,
        entity_id: str,
        config: dict,
        hass_state_dict: dict,
    ):
        """Initialize CTDevice."""
        super().__init__(
            ctrl_hass, ctrl_config, light_id, entity_id, config, hass_state_dict
        )

    def _update_device_state(self, full_update: bool) -> None:
        """Update DeviceState object."""
        super()._update_device_state(full_update)
        self._hass_state.color_temp = self._hass_state_dict.get(
            const.HASS_ATTR, {}
        ).get(const.HASS_ATTR_COLOR_TEMP)

    @property
    def color_temp(self) -> int:
        """Return color temp."""
        return self._config_state.color_temp

    def set_color_temperature(self, color_temperature: int) -> None:
        """Set color temperature."""
        if not self._control_state:
            self._control_state = self._new_control_state()
        self._control_state.color_temp = color_temperature


class RGBDevice(BrightnessDevice):
    """RGBDevice class."""

    def __init__(
        self,
        ctrl_hass: HomeAssistantController,
        ctrl_config: Config,
        light_id: str,
        entity_id: str,
        config: dict,
        hass_state_dict: dict,
    ):
        """Initialize RGBDevice."""
        super().__init__(
            ctrl_hass, ctrl_config, light_id, entity_id, config, hass_state_dict
        )

    def _update_device_state(self, full_update: bool = True) -> None:
        """Update DeviceState object."""
        super()._update_device_state(full_update)
        self._hass_state.hue_saturation = self._hass_state_dict.get(
            const.HASS_ATTR, {}
        ).get(const.HASS_ATTR_HS_COLOR)
        self._hass_state.xy_color = self._hass_state_dict.get(const.HASS_ATTR, {}).get(
            const.HASS_ATTR_XY_COLOR
        )
        self._hass_state.rgb_color = self._hass_state_dict.get(const.HASS_ATTR, {}).get(
            const.HASS_ATTR_RGB_COLOR
        )

    @property
    def hue_sat(self) -> list[int]:
        """Return hue_saturation."""
        return self._config_state.hue_saturation

    def set_hue_sat(self, hue: int | float, sat: int | float) -> None:
        """Set hue and saturation colors."""
        if not self._control_state:
            self._control_state = self._new_control_state()
        self._control_state.hue_saturation = [int(hue), int(sat)]

    @property
    def xy_color(self) -> list[float]:
        """Return xy_color."""
        return self._config_state.xy_color

    def set_xy(self, x: float, y: float) -> None:
        """Set xy colors."""
        if not self._control_state:
            self._control_state = self._new_control_state()
        self._control_state.xy_color = [float(x), float(y)]

    @property
    def rgb_color(self) -> list[int]:
        """Return rgb_color."""
        return self._config_state.rgb_color

    def set_rgb(self, r: int, g: int, b: int) -> None:
        """Set rgb colors."""
        if not self._control_state:
            self._control_state = self._new_control_state()
        self._control_state.rgb_color = [int(r), int(g), int(b)]

    def set_flash(self, flash: str) -> None:
        """Set flash."""
        super().set_flash(flash)
        # HASS now requires a color target to be sent when flashing
        # Use white color to indicate the light
        self.set_hue_sat(0, 0)


class RGBWDevice(CTDevice, RGBDevice):
    """RGBWDevice class."""

    def __init__(
        self,
        ctrl_hass: HomeAssistantController,
        ctrl_config: Config,
        light_id: str,
        entity_id: str,
        config: dict,
        hass_state_dict: dict,
    ):
        """Initialize RGBWDevice."""
        super().__init__(
            ctrl_hass, ctrl_config, light_id, entity_id, config, hass_state_dict
        )

    def _update_device_state(self, full_update: bool = True) -> None:
        """Update DeviceState object."""
        CTDevice._update_device_state(self, True)
        RGBDevice._update_device_state(self, False)


async def async_get_device(
    ctrl_hass: HomeAssistantController, ctrl_config: Config, entity_id: str
) -> OnOffDevice | BrightnessDevice | CTDevice | RGBDevice | RGBWDevice:
    """Infer light object type from Home Assistant state and returns corresponding object."""
    if entity_id in __device_cache.keys():
        return __device_cache[entity_id]

    light_id: str = await ctrl_config.async_entity_id_to_light_id(entity_id)
    config: dict = await ctrl_config.async_get_light_config(light_id)

    hass_state_dict = await async_get_hass_state(ctrl_hass, entity_id)
    entity_color_modes = hass_state_dict[const.HASS_ATTR].get(
        const.HASS_ATTR_SUPPORTED_COLOR_MODES, []
    )

    device_obj = None
    if any(
        color_mode
        in [
            const.HASS_COLOR_MODE_HS,
            const.HASS_COLOR_MODE_XY,
            const.HASS_COLOR_MODE_RGB,
            const.HASS_COLOR_MODE_RGBW,
            const.HASS_COLOR_MODE_RGBWW,
        ]
        for color_mode in entity_color_modes
    ) and any(
        color_mode
        in [
            const.HASS_COLOR_MODE_COLOR_TEMP,
            const.HASS_COLOR_MODE_RGBW,
            const.HASS_COLOR_MODE_RGBWW,
            const.HASS_COLOR_MODE_WHITE,
        ]
        for color_mode in entity_color_modes
    ):
        device_obj = RGBWDevice(
            ctrl_hass,
            ctrl_config,
            light_id,
            entity_id,
            config,
            hass_state_dict,
        )
    elif any(
        color_mode
        in [
            const.HASS_COLOR_MODE_HS,
            const.HASS_COLOR_MODE_XY,
            const.HASS_COLOR_MODE_RGB,
        ]
        for color_mode in entity_color_modes
    ):
        device_obj = RGBDevice(
            ctrl_hass,
            ctrl_config,
            light_id,
            entity_id,
            config,
            hass_state_dict,
        )
    elif const.HASS_COLOR_MODE_COLOR_TEMP in entity_color_modes:
        device_obj = CTDevice(
            ctrl_hass,
            ctrl_config,
            light_id,
            entity_id,
            config,
            hass_state_dict,
        )
    elif const.HASS_COLOR_MODE_BRIGHTNESS in entity_color_modes:
        device_obj = BrightnessDevice(
            ctrl_hass,
            ctrl_config,
            light_id,
            entity_id,
            config,
            hass_state_dict,
        )
    else:
        device_obj = OnOffDevice(
            ctrl_hass,
            ctrl_config,
            light_id,
            entity_id,
            config,
            hass_state_dict,
        )
    await device_obj.async_update_state()
    # Pull device state from Home Assistant every 5 seconds
    add_scheduler(device_obj.async_update_state, 5000)
    __device_cache[entity_id] = device_obj
    return device_obj