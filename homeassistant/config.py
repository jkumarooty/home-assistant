"""Module to help with parsing and generating configuration files."""
import asyncio
from collections import OrderedDict
import logging
import os
import re
import shutil
import sys
# pylint: disable=unused-import
from typing import Any, List, Tuple  # NOQA

import voluptuous as vol

from homeassistant.const import (
    CONF_LATITUDE, CONF_LONGITUDE, CONF_NAME, CONF_PACKAGES, CONF_UNIT_SYSTEM,
    CONF_TIME_ZONE, CONF_CUSTOMIZE, CONF_ELEVATION, CONF_UNIT_SYSTEM_METRIC,
    CONF_UNIT_SYSTEM_IMPERIAL, CONF_TEMPERATURE_UNIT, TEMP_CELSIUS,
    __version__)
from homeassistant.core import DOMAIN as CONF_CORE
from homeassistant.exceptions import HomeAssistantError
from homeassistant.loader import get_component
from homeassistant.util.yaml import load_yaml
import homeassistant.helpers.config_validation as cv
from homeassistant.util import dt as date_util, location as loc_util
from homeassistant.util.unit_system import IMPERIAL_SYSTEM, METRIC_SYSTEM
from homeassistant.helpers import customize

_LOGGER = logging.getLogger(__name__)

YAML_CONFIG_FILE = 'configuration.yaml'
VERSION_FILE = '.HA_VERSION'
CONFIG_DIR_NAME = '.homeassistant'

DEFAULT_CORE_CONFIG = (
    # Tuples (attribute, default, auto detect property, description)
    (CONF_NAME, 'Home', None, 'Name of the location where Home Assistant is '
     'running'),
    (CONF_LATITUDE, 0, 'latitude', 'Location required to calculate the time'
     ' the sun rises and sets'),
    (CONF_LONGITUDE, 0, 'longitude', None),
    (CONF_ELEVATION, 0, None, 'Impacts weather/sunrise data'
                              ' (altitude above sea level in meters)'),
    (CONF_UNIT_SYSTEM, CONF_UNIT_SYSTEM_METRIC, None,
     '{} for Metric, {} for Imperial'.format(CONF_UNIT_SYSTEM_METRIC,
                                             CONF_UNIT_SYSTEM_IMPERIAL)),
    (CONF_TIME_ZONE, 'UTC', 'time_zone', 'Pick yours from here: http://en.wiki'
     'pedia.org/wiki/List_of_tz_database_time_zones'),
)  # type: Tuple[Tuple[str, Any, Any, str], ...]
DEFAULT_CONFIG = """
# Show links to resources in log and frontend
introduction:

# Enables the frontend
frontend:

# Enables configuration UI
config:

http:
  # Uncomment this to add a password (recommended!)
  # api_password: PASSWORD
  # Uncomment this if you are using SSL or running in Docker etc
  # base_url: example.duckdns.org:8123

# Checks for available updates
updater:

# Discover some devices automatically
discovery:

# Allows you to issue voice commands from the frontend in enabled browsers
conversation:

# Enables support for tracking state changes over time.
history:

# View all events in a logbook
logbook:

# Track the sun
sun:

# Weather Prediction
sensor:
  platform: yr

# Text to speech
tts:
  platform: google

"""


PACKAGES_CONFIG_SCHEMA = vol.Schema({
    cv.slug: vol.Schema(  # Package names are slugs
        {cv.slug: vol.Any(dict, list)})  # Only slugs for component names
})

CORE_CONFIG_SCHEMA = vol.Schema({
    CONF_NAME: vol.Coerce(str),
    CONF_LATITUDE: cv.latitude,
    CONF_LONGITUDE: cv.longitude,
    CONF_ELEVATION: vol.Coerce(int),
    vol.Optional(CONF_TEMPERATURE_UNIT): cv.temperature_unit,
    CONF_UNIT_SYSTEM: cv.unit_system,
    CONF_TIME_ZONE: cv.time_zone,
    vol.Optional(CONF_CUSTOMIZE, default=[]): customize.CUSTOMIZE_SCHEMA,
    vol.Optional(CONF_PACKAGES, default={}): PACKAGES_CONFIG_SCHEMA,
})


def get_default_config_dir() -> str:
    """Put together the default configuration directory based on OS."""
    data_dir = os.getenv('APPDATA') if os.name == "nt" \
        else os.path.expanduser('~')
    return os.path.join(data_dir, CONFIG_DIR_NAME)


def ensure_config_exists(config_dir: str, detect_location: bool=True) -> str:
    """Ensure a config file exists in given configuration directory.

    Creating a default one if needed.
    Return path to the config file.
    """
    config_path = find_config_file(config_dir)

    if config_path is None:
        print("Unable to find configuration. Creating default one in",
              config_dir)
        config_path = create_default_config(config_dir, detect_location)

    return config_path


def create_default_config(config_dir, detect_location=True):
    """Create a default configuration file in given configuration directory.

    Return path to new config file if success, None if failed.
    This method needs to run in an executor.
    """
    config_path = os.path.join(config_dir, YAML_CONFIG_FILE)
    version_path = os.path.join(config_dir, VERSION_FILE)

    info = {attr: default for attr, default, _, _ in DEFAULT_CORE_CONFIG}

    location_info = detect_location and loc_util.detect_location_info()

    if location_info:
        if location_info.use_metric:
            info[CONF_UNIT_SYSTEM] = CONF_UNIT_SYSTEM_METRIC
        else:
            info[CONF_UNIT_SYSTEM] = CONF_UNIT_SYSTEM_IMPERIAL

        for attr, default, prop, _ in DEFAULT_CORE_CONFIG:
            if prop is None:
                continue
            info[attr] = getattr(location_info, prop) or default

        if location_info.latitude and location_info.longitude:
            info[CONF_ELEVATION] = loc_util.elevation(location_info.latitude,
                                                      location_info.longitude)

    # Writing files with YAML does not create the most human readable results
    # So we're hard coding a YAML template.
    try:
        with open(config_path, 'w') as config_file:
            config_file.write("homeassistant:\n")

            for attr, _, _, description in DEFAULT_CORE_CONFIG:
                if info[attr] is None:
                    continue
                elif description:
                    config_file.write("  # {}\n".format(description))
                config_file.write("  {}: {}\n".format(attr, info[attr]))

            config_file.write(DEFAULT_CONFIG)

        with open(version_path, 'wt') as version_file:
            version_file.write(__version__)

        return config_path

    except IOError:
        print('Unable to create default configuration file', config_path)
        return None


@asyncio.coroutine
def async_hass_config_yaml(hass):
    """Load YAML from hass config File.

    This function allow component inside asyncio loop to reload his config by
    self.

    This method is a coroutine.
    """
    def _load_hass_yaml_config():
        path = find_config_file(hass.config.config_dir)
        conf = load_yaml_config_file(path)
        return conf

    conf = yield from hass.loop.run_in_executor(None, _load_hass_yaml_config)
    return conf


def find_config_file(config_dir):
    """Look in given directory for supported configuration files.

    Async friendly.
    """
    config_path = os.path.join(config_dir, YAML_CONFIG_FILE)

    return config_path if os.path.isfile(config_path) else None


def load_yaml_config_file(config_path):
    """Parse a YAML configuration file.

    This method needs to run in an executor.
    """
    conf_dict = load_yaml(config_path)

    if not isinstance(conf_dict, dict):
        msg = 'The configuration file {} does not contain a dictionary'.format(
            os.path.basename(config_path))
        _LOGGER.error(msg)
        raise HomeAssistantError(msg)

    return conf_dict


def process_ha_config_upgrade(hass):
    """Upgrade config if necessary.

    This method needs to run in an executor.
    """
    version_path = hass.config.path(VERSION_FILE)

    try:
        with open(version_path, 'rt') as inp:
            conf_version = inp.readline().strip()
    except FileNotFoundError:
        # Last version to not have this file
        conf_version = '0.7.7'

    if conf_version == __version__:
        return

    _LOGGER.info('Upgrading config directory from %s to %s', conf_version,
                 __version__)

    lib_path = hass.config.path('deps')
    if os.path.isdir(lib_path):
        shutil.rmtree(lib_path)

    with open(version_path, 'wt') as outp:
        outp.write(__version__)


@asyncio.coroutine
def async_process_ha_core_config(hass, config):
    """Process the [homeassistant] section from the config.

    This method is a coroutine.
    """
    config = CORE_CONFIG_SCHEMA(config)
    hac = hass.config

    def set_time_zone(time_zone_str):
        """Helper method to set time zone."""
        if time_zone_str is None:
            return

        time_zone = date_util.get_time_zone(time_zone_str)

        if time_zone:
            hac.time_zone = time_zone
            date_util.set_default_time_zone(time_zone)
        else:
            _LOGGER.error('Received invalid time zone %s', time_zone_str)

    for key, attr in ((CONF_LATITUDE, 'latitude'),
                      (CONF_LONGITUDE, 'longitude'),
                      (CONF_NAME, 'location_name'),
                      (CONF_ELEVATION, 'elevation')):
        if key in config:
            setattr(hac, attr, config[key])

    if CONF_TIME_ZONE in config:
        set_time_zone(config.get(CONF_TIME_ZONE))

    merged_customize = merge_packages_customize(
        config[CONF_CUSTOMIZE], config[CONF_PACKAGES])
    customize.set_customize(hass, CONF_CORE, merged_customize)

    if CONF_UNIT_SYSTEM in config:
        if config[CONF_UNIT_SYSTEM] == CONF_UNIT_SYSTEM_IMPERIAL:
            hac.units = IMPERIAL_SYSTEM
        else:
            hac.units = METRIC_SYSTEM
    elif CONF_TEMPERATURE_UNIT in config:
        unit = config[CONF_TEMPERATURE_UNIT]
        if unit == TEMP_CELSIUS:
            hac.units = METRIC_SYSTEM
        else:
            hac.units = IMPERIAL_SYSTEM
        _LOGGER.warning("Found deprecated temperature unit in core config, "
                        "expected unit system. Replace '%s: %s' with "
                        "'%s: %s'", CONF_TEMPERATURE_UNIT, unit,
                        CONF_UNIT_SYSTEM, hac.units.name)

    # Shortcut if no auto-detection necessary
    if None not in (hac.latitude, hac.longitude, hac.units,
                    hac.time_zone, hac.elevation):
        return

    discovered = []

    # If we miss some of the needed values, auto detect them
    if None in (hac.latitude, hac.longitude, hac.units,
                hac.time_zone):
        info = yield from hass.loop.run_in_executor(
            None, loc_util.detect_location_info)

        if info is None:
            _LOGGER.error('Could not detect location information')
            return

        if hac.latitude is None and hac.longitude is None:
            hac.latitude, hac.longitude = (info.latitude, info.longitude)
            discovered.append(('latitude', hac.latitude))
            discovered.append(('longitude', hac.longitude))

        if hac.units is None:
            hac.units = METRIC_SYSTEM if info.use_metric else IMPERIAL_SYSTEM
            discovered.append((CONF_UNIT_SYSTEM, hac.units.name))

        if hac.location_name is None:
            hac.location_name = info.city
            discovered.append(('name', info.city))

        if hac.time_zone is None:
            set_time_zone(info.time_zone)
            discovered.append(('time_zone', info.time_zone))

    if hac.elevation is None and hac.latitude is not None and \
       hac.longitude is not None:
        elevation = yield from hass.loop.run_in_executor(
            None, loc_util.elevation, hac.latitude, hac.longitude)
        hac.elevation = elevation
        discovered.append(('elevation', elevation))

    if discovered:
        _LOGGER.warning(
            'Incomplete core config. Auto detected %s',
            ', '.join('{}: {}'.format(key, val) for key, val in discovered))


def _log_pkg_error(package, component, config, message):
    """Log an error while merging."""
    message = "Package {} setup failed. Component {} {}".format(
        package, component, message)

    pack_config = config[CONF_CORE][CONF_PACKAGES].get(package, config)
    message += " (See {}:{}). ".format(
        getattr(pack_config, '__config_file__', '?'),
        getattr(pack_config, '__line__', '?'))

    _LOGGER.error(message)


def _identify_config_schema(module):
    """Extract the schema and identify list or dict based."""
    try:
        schema = module.CONFIG_SCHEMA.schema[module.DOMAIN]
    except (AttributeError, KeyError):
        return (None, None)
    t_schema = str(schema)
    if (t_schema.startswith('<function ordered_dict') or
            t_schema.startswith('<Schema({<function slug')):
        return ('dict', schema)
    if t_schema.startswith('All(<function ensure_list'):
        return ('list', schema)
    return '', schema


def merge_packages_config(config, packages):
    """Merge packages into the top-level config. Mutate config."""
    # pylint: disable=too-many-nested-blocks
    PACKAGES_CONFIG_SCHEMA(packages)
    for pack_name, pack_conf in packages.items():
        for comp_name, comp_conf in pack_conf.items():
            if comp_name == CONF_CORE:
                continue
            component = get_component(comp_name)

            if component is None:
                _log_pkg_error(pack_name, comp_name, config, "does not exist")
                continue

            if hasattr(component, 'PLATFORM_SCHEMA'):
                config[comp_name] = cv.ensure_list(config.get(comp_name))
                config[comp_name].extend(cv.ensure_list(comp_conf))
                continue

            if hasattr(component, 'CONFIG_SCHEMA'):
                merge_type, _ = _identify_config_schema(component)

                if merge_type == 'list':
                    config[comp_name] = cv.ensure_list(config.get(comp_name))
                    config[comp_name].extend(cv.ensure_list(comp_conf))
                    continue

                if merge_type == 'dict':
                    if not isinstance(comp_conf, dict):
                        _log_pkg_error(
                            pack_name, comp_name, config,
                            "cannot be merged. Expected a dict.")
                        continue

                    if comp_name not in config:
                        config[comp_name] = OrderedDict()

                    if not isinstance(config[comp_name], dict):
                        _log_pkg_error(
                            pack_name, comp_name, config,
                            "cannot be merged. Dict expected in main config.")
                        continue

                    for key, val in comp_conf.items():
                        if key in config[comp_name]:
                            _log_pkg_error(pack_name, comp_name, config,
                                           "duplicate key '{}'".format(key))
                            continue
                        config[comp_name][key] = val
                    continue

            # The last merge type are sections that may occur only once
            if comp_name in config:
                _log_pkg_error(
                    pack_name, comp_name, config, "may occur only once"
                    " and it already exist in your main config")
                continue
            config[comp_name] = comp_conf

    return config


def merge_packages_customize(core_customize, packages):
    """Merge customize from packages."""
    schema = vol.Schema({
        vol.Optional(CONF_CORE): vol.Schema({
            CONF_CUSTOMIZE: customize.CUSTOMIZE_SCHEMA}),
    }, extra=vol.ALLOW_EXTRA)

    cust = list(core_customize)
    for pkg in packages.values():
        conf = schema(pkg)
        cust.extend(conf.get(CONF_CORE, {}).get(CONF_CUSTOMIZE, []))
    return cust


@asyncio.coroutine
def async_check_ha_config_file(hass):
    """Check if HA config file valid.

    This method is a coroutine.
    """
    proc = yield from asyncio.create_subprocess_exec(
        sys.executable, '-m', 'homeassistant', '--script',
        'check_config', '--config', hass.config.config_dir,
        stdout=asyncio.subprocess.PIPE, loop=hass.loop)
    # Wait for the subprocess exit
    stdout_data, dummy = yield from proc.communicate()
    result = yield from proc.wait()

    if not result:
        return None

    return re.sub(r'\033\[[^m]*m', '', str(stdout_data, 'utf-8'))
