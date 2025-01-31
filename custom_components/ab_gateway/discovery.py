"""Support for MQTT discovery."""
from __future__ import annotations

import asyncio
from collections import deque
import functools
import json
import logging
import re
import time
from datetime import timedelta
import voluptuous as vol
import msgpack

from . import ble_parser
from homeassistant.components import mqtt
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from homeassistant.const import (
    CONF_MAC,
)
from homeassistant.components.mqtt import (
    CONF_BROKER,
    CONF_DISCOVERY_PREFIX,
    CONF_TOPIC,
)
from homeassistant.components.bluetooth import (
    BaseHaRemoteScanner,
    async_get_advertisement_callback,
    async_register_scanner,
)
from homeassistant.components.bluetooth.models import BluetoothServiceInfoBleak
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from .const import (
    DOMAIN,
    CONF_GATEWAY_ID,
    INDEX_ADV,
    INDEX_MAC,
    INDEX_RSSI
)

DISCOVERY_UNSUBSCRIBE = "ab_gateway_discovery_unsubscribe"

_LOGGER = logging.getLogger(__name__)

async def async_start(hass, config, config_entry=None) -> bool:
    new_info_callback = async_get_advertisement_callback(hass)
    assert config_entry.unique_id is not None
    source = str(config_entry.unique_id)
    _LOGGER.debug(
        "%s: Connecting scanner",
        source
    )

    scanner = ABGatewayScanner(hass, source, config, new_info_callback)
    config_entry.async_on_unload(async_register_scanner(hass, scanner, connectable=False))
    hass.loop.create_task(scanner.async_run(hass))
    return True

async def async_stop(hass) -> bool:
    """Stop MQTT Discovery."""
    if DISCOVERY_UNSUBSCRIBE in hass.data:
        for unsub in hass.data[DISCOVERY_UNSUBSCRIBE]:
            unsub()
        hass.data[DISCOVERY_UNSUBSCRIBE] = []
    return True


class ABGatewayScanner(BaseHaRemoteScanner):

    def __init__(
        self,
        hass: HomeAssistant,
        scanner_id: str,
        config,
        new_info_callback: Callable[[BluetoothServiceInfoBleak], None],
    ) -> None:
        """Initialize the scanner."""
        self._hass = hass
        self._new_info_callback = new_info_callback
        self.source = scanner_id
        self._config = config
        self._queues: DiscoveryQueue = hass.data[DOMAIN]["queues"]
        self._discovered_device_advertisement_datas: dict[
            str, tuple[BLEDevice, AdvertisementData]
        ] = {}
        self._discovered_device_timestamps: dict[str, float] = {}
        self.name = DOMAIN

    async def async_run(self, hass):

        queues = self._queues
        callback = self._new_info_callback

        def convert_dev_to_dict(data):
            adpayload_start = 8
            rssi_index = 7
            adtype = data[0]
            mac = data[1:7].hex()
            adpayload = data[adpayload_start:]
            rssi = data[rssi_index] - 256
            converted = [adtype, mac, rssi, adpayload]
            return converted

        async def async_process_discovery_data(data):
            """Process the data of a new discovery."""
            gateway_id = data.get('mac')
            for dev in data.get('devices', []):
                if type(dev).__name__ == 'bytes':
                    queues.put("adv", {"gateway_id": gateway_id, "device": convert_dev_to_dict(dev)})
                else:
                    dev[INDEX_MAC] = dev[INDEX_MAC].lower()
                    dev[INDEX_ADV] = bytes.fromhex(dev[3])
                    queues.put("adv", {"gateway_id": gateway_id, "device": dev})
            return True

        async def async_message_received(msg):
            """Handle new MQTT messages."""
            try:
                data = msgpack.unpackb(msg.payload)
                await async_process_discovery_data(data)
            except msgpack.exceptions.ExtraData as error:
                _LOGGER.debug("Msgpack cannot decode data: %s, try json instead", error)
                data =  json.loads(msg.payload)
                _LOGGER.debug("JSON data: %s", data.get('mac'))
                await async_process_discovery_data(data)
            except UnicodeDecodeError as error:
                _LOGGER.debug("Cannot decode data: %s", error)
                return
            except ValueError:
                _LOGGER.warning("Unable to parse JSON %s", payload)
                return

        discovery_topic = self._config[CONF_DISCOVERY_PREFIX]
        discovery_topics = [
            f"{discovery_topic}/+"
        ]
        self._hass.data[DISCOVERY_UNSUBSCRIBE] = await asyncio.gather(
            *(
                mqtt.async_subscribe(self._hass, topic, async_message_received, 0, encoding = None)
                for topic in discovery_topics
            )
        )

        queue = self._queues.get('adv').async_q
        while True:
            try:
                data = await asyncio.wait_for(queue.get(), 1)
                await self.async_on_advertisement(data)
            except asyncio.TimeoutError:
                pass

    async def async_on_advertisement(self, data) -> None:
        """Call the registered callback."""
        device, advertisement_data = ble_parser.parse_data(data.get("device"))
        # self.source = f"{DOMAIN}_{data.get('gateway_id')}"
        now = time.monotonic()
        self._discovered_device_advertisement_datas[device.address] = (
            device,
            advertisement_data,
        )
        self._discovered_device_timestamps[device.address] = now
        self._new_info_callback(
            BluetoothServiceInfoBleak(
                name=advertisement_data.local_name or device.name or device.address,
                address=device.address,
                rssi=advertisement_data.rssi,
                manufacturer_data=advertisement_data.manufacturer_data,
                service_data=advertisement_data.service_data,
                service_uuids=advertisement_data.service_uuids,
                source=f"{DOMAIN}_{data.get('gateway_id')}",
                device=device,
                advertisement=advertisement_data,
                connectable=True,
                time=now,
            )
        )

