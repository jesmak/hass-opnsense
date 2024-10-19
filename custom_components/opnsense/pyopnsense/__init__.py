import asyncio
import inspect
import ipaddress
import json
import logging
import re
import socket
import ssl
import traceback
import xmlrpc.client
from abc import ABC
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote_plus, urlparse

import aiohttp
import awesomeversion
from dateutil.parser import parse

# value to set as the socket timeout
DEFAULT_TIMEOUT = 60

_LOGGER: logging.Logger = logging.getLogger(__name__)


def get_ip_key(item) -> tuple:
    address = item.get("address", None)

    if not address:
        # If the address is empty, place it at the end
        return (3, "")
    try:
        ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address = ipaddress.ip_address(
            address
        )
        # Sort by IP version (IPv4 first, IPv6 second), then by numerical value
        return (0 if ip_obj.version == 4 else 1, ip_obj)
    except ValueError:
        return (2, "")


def dict_get(data: Mapping[str, Any], path: str, default=None):
    pathList = re.split(r"\.", path, flags=re.IGNORECASE)
    result = data
    for key in pathList:
        try:
            key = int(key) if key.isnumeric() else key
            result = result[key]
        except (TypeError, KeyError, AttributeError):
            result = default
            break

    return result


class OPNsenseClient(ABC):
    """OPNsense Client"""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        session: aiohttp.ClientSession,
        opts: Mapping[str, Any] = None,
        initial: bool = False,
    ) -> None:
        """OPNsense Client initializer."""

        self._username: str = username
        self._password: str = password

        self._opts: Mapping[str, Any] = opts or {}
        self._verify_ssl: bool = self._opts.get("verify_ssl", True)
        parts = urlparse(url.rstrip("/"))
        self._url: str = f"{parts.scheme}://{parts.netloc}"
        self._xmlrpc_url: str = (
            f"{parts.scheme}://{quote_plus(username)}:{quote_plus(password)}@{parts.netloc}"
        )
        self._scheme: str = parts.scheme
        self._session: aiohttp.ClientSession = session
        self._initial = initial
        self._firmware_version = None
        self._xmlrpc_query_count = 0
        self._rest_api_query_count = 0
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

    def _xmlrpc_timeout(func):
        async def inner(self, *args, **kwargs):
            response = None
            # timout applies to each recv() call, not the whole request
            default_timeout = socket.getdefaulttimeout()
            try:
                socket.setdefaulttimeout(DEFAULT_TIMEOUT)
                response = await func(self, *args, **kwargs)
            finally:
                socket.setdefaulttimeout(default_timeout)
            return response

        return inner

    def _log_errors(func):
        async def inner(self, *args, **kwargs):
            try:
                return await func(self, *args, **kwargs)
            except asyncio.CancelledError as e:
                raise e
            except (TimeoutError, aiohttp.ServerTimeoutError) as e:
                _LOGGER.warning(
                    f"Timeout Error in {func.__name__.strip('_')}. Will retry. {e}"
                )
                if self._initial:
                    raise e
            except Exception as e:
                redacted_message = re.sub(
                    r"(\w+):(\w+)@", "<redacted>:<redacted>@", str(e)
                )
                _LOGGER.error(
                    f"Error in {func.__name__.strip('_')}. {e.__class__.__qualname__}: {redacted_message}\n{''.join(traceback.format_tb(e.__traceback__))}"
                )
                if self._initial:
                    raise e

        return inner

    async def reset_query_counts(self):
        self._xmlrpc_query_count = 0
        self._rest_api_query_count = 0

    async def get_query_counts(self) -> tuple:
        return self._rest_api_query_count, self._xmlrpc_query_count

    # https://stackoverflow.com/questions/64983392/python-multiple-patch-gives-http-client-cannotsendrequest-request-sent
    def _get_proxy(self) -> xmlrpc.client.ServerProxy:
        # https://docs.python.org/3/library/xmlrpc.client.html#module-xmlrpc.client
        # https://stackoverflow.com/questions/30461969/disable-default-certificate-verification-in-python-2-7-9
        context = None

        if self._scheme == "https" and not self._verify_ssl:
            context = ssl._create_unverified_context()

        # set to True if necessary during development
        verbose = False

        proxy = xmlrpc.client.ServerProxy(
            f"{self._xmlrpc_url}/xmlrpc.php", context=context, verbose=verbose
        )
        return proxy

    # @_xmlrpc_timeout
    async def _get_config_section(self, section) -> Mapping[str, Any]:
        config: Mapping[str, Any] = await self.get_config()
        if config is None or not isinstance(config, Mapping):
            _LOGGER.error("Invalid data returned from get_config_section")
            return {}
        return config.get(section, {})

    @_xmlrpc_timeout
    async def _restore_config_section(self, section_name, data):
        params: Mapping[str, Any] = {section_name: data}
        response = await self._loop.run_in_executor(
            None, self._get_proxy().opnsense.restore_config_section, params
        )
        return response

    @_xmlrpc_timeout
    async def _exec_php(self, script) -> Mapping[str, Any]:
        self._xmlrpc_query_count += 1
        script: str = (
            r"""
ini_set('display_errors', 0);

{}

// wrapping this in json_encode and then unwrapping in python prevents funny XMLRPC NULL encoding errors
// https://github.com/travisghansen/hass-pfsense/issues/35
$toreturn_real = $toreturn;
$toreturn = [];
$toreturn["real"] = json_encode($toreturn_real);
""".format(
                script
            )
        )
        try:
            response = await self._loop.run_in_executor(
                None, self._get_proxy().opnsense.exec_php, script
            )
            response_json = json.loads(response["real"])
            return response_json
        except TypeError as e:
            _LOGGER.error(
                f"Invalid data returned from exec_php for {inspect.currentframe().f_back.f_code.co_qualname.strip('_')}. {e.__class__.__qualname__}: {e}. Ensure the OPNsense user connected to HA either has full Admin access or specifically has the 'XMLRPC Library' privilege"
            )
            return {}
        except xmlrpc.client.Fault as e:
            _LOGGER.error(
                f"Error running exec_php script for {inspect.currentframe().f_back.f_code.co_qualname.strip('_')}. {e.__class__.__qualname__}: {e}. Ensure the 'os-homeassistant-maxit' plugin has been installed on OPNsense"
            )
            return {}
        except socket.gaierror as e:
            _LOGGER.warning(
                f"Connection Error running exec_php script for {inspect.currentframe().f_back.f_code.co_qualname.strip('_')}. {e.__class__.__qualname__}: {e}. Will retry"
            )
            return {}

    @_log_errors
    async def get_host_firmware_version(self) -> None | str:
        firmware_info: Mapping[str, Any] | list = await self._get(
            "/api/core/firmware/status"
        )
        if not isinstance(firmware_info, Mapping):
            return None
        firmware: str | None = firmware_info.get("product_version", None)
        _LOGGER.debug(f"[get_host_firmware_version] firmware: {firmware}")
        self._firmware_version = firmware
        return firmware

    async def _get_from_stream(self, path: str) -> Mapping[str, Any] | list:
        self._rest_api_query_count += 1
        url: str = f"{self._url}{path}"
        _LOGGER.debug(f"[get_from_stream] url: {url}")
        try:
            async with self._session.get(
                url,
                auth=aiohttp.BasicAuth(self._username, self._password),
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
                ssl=self._verify_ssl,
            ) as response:
                _LOGGER.debug(
                    f"[get_from_stream] Response {response.status}: {response.reason}"
                )

                if response.ok:
                    buffer = ""
                    message_count = 0

                    async for chunk in response.content.iter_chunked(1024):
                        buffer += chunk.decode("utf-8")

                        if "\n\n" in buffer:
                            message, buffer = buffer.split("\n\n", 1)
                            lines = message.splitlines()

                            for line in lines:
                                if line.startswith("data:"):
                                    message_count += 1
                                    if message_count == 2:
                                        response_str: str = line[len("data:") :].strip()
                                        response_json: Mapping[str, Any] | list = (
                                            json.loads(response_str)
                                        )

                                        # _LOGGER.debug(f"[get_from_stream] response_json ({type(response_json).__name__}): {response_json}")
                                        return response_json  # Exit after processing the second message

                elif response.status == 403:
                    _LOGGER.error(
                        f"Permission Error in {inspect.currentframe().f_back.f_code.co_qualname.strip('_')}. Path: {url}. Ensure the OPNsense user connected to HA has full Admin access."
                    )
                else:
                    _LOGGER.error(
                        f"Error in {inspect.currentframe().f_back.f_code.co_qualname.strip('_')}. Path: {url}. Response {response.status}: {response.reason}"
                    )
        except aiohttp.ClientError as e:
            _LOGGER.error(f"Client error. {e.__class__.__qualname__}: {e}")
            if self._initial:
                raise e

        return None

    async def _get(self, path: str) -> Mapping[str, Any] | list:
        # /api/<module>/<controller>/<command>/[<param1>/[<param2>/...]]
        self._rest_api_query_count += 1
        url: str = f"{self._url}{path}"
        _LOGGER.debug(f"[get] url: {url}")
        try:
            async with self._session.get(
                url,
                auth=aiohttp.BasicAuth(self._username, self._password),
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
                ssl=self._verify_ssl,
            ) as response:
                _LOGGER.debug(f"[get] Response {response.status}: {response.reason}")
                if response.ok:
                    response_json: Mapping[str, Any] | list = await response.json(
                        content_type=None
                    )
                    return response_json
                if response.status == 403:
                    _LOGGER.error(
                        f"Permission Error in {inspect.currentframe().f_back.f_code.co_qualname.strip('_')}. Path: {url}. Ensure the OPNsense user connected to HA has full Admin access"
                    )
                else:
                    _LOGGER.error(
                        f"Error in {inspect.currentframe().f_back.f_code.co_qualname.strip('_')}. Path: {url}. Response {response.status}: {response.reason}"
                    )
        except aiohttp.ClientError as e:
            _LOGGER.error(f"Client error. {e.__class__.__qualname__}: {e}")
            if self._initial:
                raise e

        return None

    async def _post(self, path: str, payload=None) -> Mapping[str, Any] | list:
        # /api/<module>/<controller>/<command>/[<param1>/[<param2>/...]]
        self._rest_api_query_count += 1
        url: str = f"{self._url}{path}"
        _LOGGER.debug(f"[post] url: {url}")
        _LOGGER.debug(f"[post] payload: {payload}")
        try:
            async with self._session.post(
                url,
                json=payload,
                auth=aiohttp.BasicAuth(self._username, self._password),
                timeout=aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT),
                ssl=self._verify_ssl,
            ) as response:
                _LOGGER.debug(f"[post] Response {response.status}: {response.reason}")
                if response.ok:
                    response_json: Mapping[str, Any] | list = await response.json(
                        content_type=None
                    )
                    return response_json
                elif response.status == 403:
                    _LOGGER.error(
                        f"Permission Error in {inspect.currentframe().f_back.f_code.co_qualname.strip('_')}. Path: {url}. Ensure the OPNsense user connected to HA has full Admin access"
                    )
                else:
                    _LOGGER.error(
                        f"Error in {inspect.currentframe().f_back.f_code.co_qualname.strip('_')}. Path: {url}. Response {response.status}: {response.reason}"
                    )
        except aiohttp.ClientError as e:
            _LOGGER.error(f"Client error. {e.__class__.__qualname__}: {e}")
            if self._initial:
                raise e

        return None

    @_log_errors
    async def _filter_configure(self) -> None:
        script: str = r"""
filter_configure();
clear_subsystem_dirty('natconf');
clear_subsystem_dirty('filter');
"""
        await self._exec_php(script)

    @_log_errors
    async def get_device_unique_id(self) -> str | None:
        instances: Mapping[str, Any] | list = await self._post(
            "/api/interfaces/overview/export"
        )
        if not isinstance(instances, list):
            return None

        mac_addresses = [
            d.get("macaddr_hw")
            for d in instances
            if d.get("is_physical") and "macaddr_hw" in d
        ]

        unique_mac_addresses: list = sorted(set(mac_addresses))
        device_unique_id: str | None = (
            unique_mac_addresses[0] if unique_mac_addresses else None
        )
        if device_unique_id:
            return device_unique_id.replace(":", "_").strip()
        return None

    @_log_errors
    async def get_system_info(self) -> Mapping[str, Any]:
        # TODO: add bios details here
        if not self._firmware_version:
            await self.get_host_firmware_version()
        try:
            if awesomeversion.AwesomeVersion(
                self._firmware_version
            ) < awesomeversion.AwesomeVersion("24.7"):
                _LOGGER.info("Using legacy get_system_info method for OPNsense < 24.7")
                return await self._get_system_info_legacy()
        except awesomeversion.exceptions.AwesomeVersionCompareException:
            pass
        system_info: Mapping[str, Any] = {}
        response: Mapping[str, Any] | list = await self._get(
            "/api/diagnostics/system/systemInformation"
        )
        if isinstance(response, Mapping):
            system_info["name"] = response.get("name", None)
        return system_info

    @_log_errors
    async def _get_system_info_legacy(self) -> Mapping[str, Any]:
        # TODO: add bios details here
        script: str = r"""
global $config;

$toreturn = [
  "hostname" => $config["system"]["hostname"],
  "domain" => $config["system"]["domain"],
];
"""
        response: Mapping[str, Any] = await self._exec_php(script)
        if not isinstance(response, Mapping):
            return {}
        response["name"] = f"{response.pop('hostname','')}.{response.pop('domain','')}"
        return response

    @_log_errors
    async def get_firmware_update_info(self):
        refresh_triggered = False
        refresh_interval = 2 * 60 * 60  # 2 hours

        status = None
        upgradestatus = None

        # GET /api/core/firmware/status
        status = await self._get("/api/core/firmware/status")
        # print(status)

        # if error or too old trigger check (only if check is not already in progress)
        # {'status_msg': 'Firmware status check was aborted internally. Please try again.', 'status': 'error'}
        # error could be because data has not been refreshed at all OR an upgrade is currently in progress
        if (
            not isinstance(status, Mapping)
            or status.get("status", None) == "error"
            or "last_check" not in status
            or not isinstance(dict_get(status, "product.product_check"), dict)
            or not dict_get(status, "product.product_check")
        ):
            await self._post("/api/core/firmware/check")
            refresh_triggered = True
        elif "last_check" in status:
            # "last_check": "Wed Dec 22 16:56:20 UTC 2021"
            # "last_check": "Mon Jan 16 00:08:28 CET 2023"
            # "last_check": "Sun Jan 15 22:05:55 UTC 2023"
            # format = "%a %b %d %H:%M:%S %Z %Y"
            try:
                last_check: datetime = parse(status.get("last_check"))
                if last_check.tzinfo is None:
                    last_check = last_check.replace(
                        tzinfo=datetime.now().astimezone().tzinfo
                    )

                last_check_timestamp: float = last_check.timestamp()

            except (ValueError, TypeError):
                last_check_timestamp: float = 0

            stale: bool = (
                datetime.now().astimezone().timestamp() - last_check_timestamp
            ) > refresh_interval
            if stale:
                upgradestatus = await self._get("/api/core/firmware/upgradestatus")
                # print(upgradestatus)
                if isinstance(upgradestatus, Mapping):
                    # status = running (package refresh in progress OR upgrade in progress)
                    # status = done (refresh/upgrade done)
                    if upgradestatus.get("status", None) == "done":
                        # tigger repo update
                        # should this be /api/core/firmware/upgrade
                        # check = await self._post("/api/core/firmware/check")
                        # print(check)
                        refresh_triggered = True
                    else:
                        # print("upgrade already running")
                        pass

        wait_for_refresh = False
        if refresh_triggered and wait_for_refresh:
            # print("refresh triggered, waiting for it to finish")
            pass

        return status

    @_log_errors
    async def upgrade_firmware(self, type="update"):
        # minor updates of the same opnsense version
        if type == "update":
            # can watch the progress on the 'Updates' tab in the UI
            return await self._post("/api/core/firmware/update")

        # major updates to a new opnsense version
        if type == "upgrade":
            # can watch the progress on the 'Updates' tab in the UI
            return await self._post("/api/core/firmware/upgrade")

    @_log_errors
    async def upgrade_status(self):
        return await self._post("/api/core/firmware/upgradestatus")

    @_log_errors
    async def firmware_changelog(self, version):
        return await self._post("/api/core/firmware/changelog/" + version)

    @_log_errors
    async def get_config(self) -> Mapping[str, Any]:
        script: str = r"""
global $config;

$toreturn = [
  "data" => $config,
];
"""
        response: Mapping[str, Any] = await self._exec_php(script)
        if not isinstance(response, Mapping):
            return {}
        ret_data = response.get("data", {})
        if not isinstance(ret_data, Mapping):
            return {}
        return ret_data

    @_log_errors
    async def enable_filter_rule_by_created_time(self, created_time) -> None:
        config = await self.get_config()
        for rule in config["filter"]["rule"]:
            if "created" not in rule.keys():
                continue
            if "time" not in rule["created"].keys():
                continue
            if rule["created"]["time"] != created_time:
                continue

            if "disabled" in rule.keys():
                del rule["disabled"]
                await self._restore_config_section("filter", config["filter"])
                await self._filter_configure()

    @_log_errors
    async def disable_filter_rule_by_created_time(self, created_time) -> None:
        config: Mapping[str, Any] = await self.get_config()

        for rule in config.get("filter", {}).get("rule", []):
            if "created" not in rule.keys():
                continue
            if "time" not in rule["created"].keys():
                continue
            if rule["created"]["time"] != created_time:
                continue

            if "disabled" not in rule.keys():
                rule["disabled"] = "1"
                await self._restore_config_section("filter", config["filter"])
                await self._filter_configure()

    # use created_time as a unique_id since none other exists
    @_log_errors
    async def enable_nat_port_forward_rule_by_created_time(self, created_time) -> None:
        config: Mapping[str, Any] = await self.get_config()
        for rule in config.get("nat", {}).get("rule", []):
            if "created" not in rule.keys():
                continue
            if "time" not in rule["created"].keys():
                continue
            if rule["created"]["time"] != created_time:
                continue

            if "disabled" in rule.keys():
                del rule["disabled"]
                await self._restore_config_section("nat", config["nat"])
                await self._filter_configure()

    # use created_time as a unique_id since none other exists
    @_log_errors
    async def disable_nat_port_forward_rule_by_created_time(self, created_time) -> None:
        config: Mapping[str, Any] = await self.get_config()
        for rule in config.get("nat", {}).get("rule", []):
            if "created" not in rule.keys():
                continue
            if "time" not in rule["created"].keys():
                continue
            if rule["created"]["time"] != created_time:
                continue

            if "disabled" not in rule.keys():
                rule["disabled"] = "1"
                await self._restore_config_section("nat", config["nat"])
                await self._filter_configure()

    # use created_time as a unique_id since none other exists
    @_log_errors
    async def enable_nat_outbound_rule_by_created_time(self, created_time) -> None:
        config: Mapping[str, Any] = await self.get_config()
        for rule in config.get("nat", {}).get("outbound", {}).get("rule", []):
            if "created" not in rule.keys():
                continue
            if "time" not in rule["created"].keys():
                continue
            if rule["created"]["time"] != created_time:
                continue

            if "disabled" in rule.keys():
                del rule["disabled"]
                await self._restore_config_section("nat", config["nat"])
                await self._filter_configure()

    # use created_time as a unique_id since none other exists
    @_log_errors
    async def disable_nat_outbound_rule_by_created_time(self, created_time) -> None:
        config: Mapping[str, Any] = await self.get_config()
        for rule in config.get("nat", {}).get("outbound", {}).get("rule", []):
            if rule["created"]["time"] != created_time:
                continue

            if "disabled" not in rule.keys():
                rule["disabled"] = "1"
                await self._restore_config_section("nat", config["nat"])
                await self._filter_configure()

    @_log_errors
    async def get_arp_table(self, resolve_hostnames=False) -> Mapping[str, Any]:
        # [{'hostname': '?', 'ip-address': '<ip>', 'mac-address': '<mac>', 'interface': 'em0', 'expires': 1199, 'type': 'ethernet'}, ...]
        request_body: Mapping[str, Any] = {"resolve": "yes"}
        arp_table_info: Mapping[str, Any] | list = await self._post(
            "/api/diagnostics/interface/search_arp", payload=request_body
        )
        if not isinstance(arp_table_info, Mapping):
            return []
        # _LOGGER.debug(f"[get_arp_table] arp_table_info: {arp_table_info}")
        arp_table: list = arp_table_info.get("rows", [])
        # _LOGGER.debug(f"[get_arp_table] arp_table: {arp_table}")
        return arp_table

    @_log_errors
    async def get_services(self) -> list:
        response: Mapping[str, Any] | list = await self._get("/api/core/service/search")
        if not isinstance(response, Mapping):
            _LOGGER.error("Invalid data returned from get_services")
            return []
        # _LOGGER.debug(f"[get_services] response: {response}")
        services: list = response.get("rows", [])
        for service in services:
            service["status"] = service.get("running", 0) == 1
        # _LOGGER.debug(f"[get_services] services: {services}")
        return services

    @_log_errors
    async def get_service_is_running(self, service: str) -> bool:
        services: list = await self.get_services()
        if services is None or not isinstance(services, list):
            return False
        for svc in services:
            if (
                svc.get("name", None) == service or svc.get("id", None) == service
            ) and svc.get("status", False):
                return True
        return False

    async def _manage_service(self, action: str, service: str) -> bool:
        if not service:
            return False
        api_addr: str = f"/api/core/service/{action}/{service}"
        response: Mapping[str, Any] | list = await self._post(api_addr)
        _LOGGER.debug(f"[{action}_service] service: {service}, response: {response}")
        return (
            isinstance(response, Mapping) and response.get("result", "failed") == "ok"
        )

    @_log_errors
    async def start_service(self, service: str) -> bool:
        return await self._manage_service("start", service)

    @_log_errors
    async def stop_service(self, service: str) -> bool:
        return await self._manage_service("stop", service)

    @_log_errors
    async def restart_service(self, service: str) -> bool:
        return await self._manage_service("restart", service)

    @_log_errors
    async def restart_service_if_running(self, service: str) -> bool:
        if await self.get_service_is_running(service):
            return await self.restart_service(service)
        return True

    @_log_errors
    async def get_dhcp_leases(self) -> list:
        leases_raw: list = (
            await self._get_kea_dhcpv4_leases()
            + await self._get_isc_dhcpv4_leases()
            + await self._get_isc_dhcpv6_leases()
        )

        # _LOGGER.debug(f"[get_dhcp_leases] leases_raw: {leases_raw}")
        leases: Mapping[str, Any] = {}
        lease_interfaces: Mapping[str, Any] = {}
        for lease in leases_raw:
            if (
                not isinstance(lease, Mapping)
                or not isinstance(lease.get("if_name", None), str)
                or len(lease.get("if_name", "")) == 0
            ):
                continue
            if_name = lease.pop("if_name", None)
            if_descr = lease.pop("if_descr", None)
            if if_name not in leases:
                lease_interfaces.update({if_name: if_descr})
                leases[if_name] = []
            leases[if_name].append(lease)

        sorted_lease_interfaces: Mapping[str, Any] = {
            key: lease_interfaces[key] for key in sorted(lease_interfaces)
        }
        sorted_leases: Mapping[str, Any] = {key: leases[key] for key in sorted(leases)}
        for if_subnet in sorted_leases.values():
            sorted_if: list = sorted(if_subnet, key=get_ip_key)
            if_subnet: list = sorted_if

        dhcp_leases: Mapping[str, Any] = {
            "lease_interfaces": sorted_lease_interfaces,
            "leases": sorted_leases,
        }
        # _LOGGER.debug(f"[get_dhcp_leases] dhcp_leases: {dhcp_leases}")

        return dhcp_leases

    async def _get_kea_dhcpv4_leases(self) -> list:
        response: Mapping[str, Any] | list = await self._get("/api/kea/leases4/search")
        if not isinstance(response, Mapping) or not isinstance(
            response.get("rows", None), list
        ):
            return []
        res_resp = await self._get("/api/kea/dhcpv4/searchReservation")
        if not isinstance(res_resp, Mapping) or not isinstance(
            res_resp.get("rows", None), list
        ):
            res_info = []
        else:
            res_info = res_resp.get("rows", [])
        reservations = {}
        for res in res_info:
            if res.get("hw_address", None):
                reservations.update({res.get("hw_address"): res.get("ip_address", "")})
        # _LOGGER.debug(f"[get_kea_dhcpv4_leases] reservations: {reservations}")
        leases_info: list = response.get("rows", [])
        # _LOGGER.debug(f"[get_kea_dhcpv4_leases] leases_info: {leases_info}")
        leases: list = []
        for lease_info in leases_info:
            if (
                lease_info is None
                or not isinstance(lease_info, Mapping)
                or lease_info.get("state", "0") != "0"
                or not lease_info.get("hwaddr", None)
            ):
                continue
            lease: Mapping[str, Any] = {}
            lease["address"] = lease_info.get("address", None)
            lease["hostname"] = (
                lease_info.get("hostname", None).strip(".")
                if isinstance(lease_info.get("hostname", None), str)
                and len(lease_info.get("hostname", "")) > 0
                else None
            )
            lease["if_descr"] = lease_info.get("if_descr", None)
            lease["if_name"] = lease_info.get("if_name", None)
            if (
                lease_info.get("hwaddr", None)
                and lease_info.get("hwaddr") in reservations
                and reservations[lease_info.get("hwaddr")]
                == lease_info.get("address", None)
            ):
                lease["type"] = "static"
            else:
                lease["type"] = "dynamic"
            lease["mac"] = lease_info.get("hwaddr", None)
            if self._try_to_int(lease_info.get("expire", None)):
                lease["expires"] = datetime.fromtimestamp(
                    self._try_to_int(lease_info.get("expire", None)),
                    tz=datetime.now().astimezone().tzinfo,
                )
                if lease["expires"] < datetime.now().astimezone():
                    continue
            else:
                lease["expires"] = lease_info.get("expire", None)
            leases.append(lease)
        # _LOGGER.debug(f"[get_kea_dhcpv4_leases] leases: {leases}")
        return leases

    # Kea DHCPv4
    # {
    #     "if": "vlan03",
    #     "address": "10.100.50.5",
    #     "hwaddr": "34:98:7a:5e:ae:b4",
    #     "client_id": "01:34:98:7a:5e:ae:b4",
    #     "valid_lifetime": "86400",
    #     "expire": "1727487541",
    #     "subnet_id": "3",
    #     "fqdn_fwd": "0",
    #     "fqdn_rev": "0",
    #     "hostname": "keepconnect",
    #     "state": "0",
    #     "user_context": "",
    #     "pool_id": "0",
    #     "if_descr": "IOT_50",
    #     "if_name": "opt5"
    # },

    async def _get_isc_dhcpv4_leases(self) -> list:
        response: Mapping[str, Any] | list = await self._get(
            "/api/dhcpv4/leases/searchLease"
        )
        if not isinstance(response, Mapping):
            return []
        leases_info: list = response.get("rows", [])
        if not isinstance(leases_info, list):
            return []
        # _LOGGER.debug(f"[get_isc_dhcpv4_leases] leases_info: {leases_info}")
        leases: list = []
        for lease_info in leases_info:
            # _LOGGER.debug(f"[get_isc_dhcpv4_leases] lease_info: {lease_info}")
            if (
                not isinstance(lease_info, Mapping)
                or lease_info.get("state", "") != "active"
                or not lease_info.get("mac", None)
            ):
                continue
            lease: Mapping[str, Any] = {}
            lease["address"] = lease_info.get("address", None)
            lease["hostname"] = (
                lease_info.get("hostname", None)
                if isinstance(lease_info.get("hostname", None), str)
                and len(lease_info.get("hostname", "")) > 0
                else None
            )
            lease["if_descr"] = lease_info.get("if_descr", None)
            lease["if_name"] = lease_info.get("if", None)
            lease["type"] = lease_info.get("type", None)
            lease["mac"] = lease_info.get("mac", None)
            if lease_info.get("ends", None):
                dt: datetime = datetime.strptime(
                    lease_info.get("ends", None), "%Y/%m/%d %H:%M:%S"
                )
                lease["expires"] = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
                if lease["expires"] < datetime.now().astimezone():
                    continue
            else:
                lease["expires"] = lease_info.get("ends", None)
            leases.append(lease)
        # _LOGGER.debug(f"[get_isc_dhcpv4_leases] leases: {leases}")
        return leases

    # Legacy DHCPv4
    # {
    #     "address": "10.100.150.100",
    #     "starts": "2024/10/05 18:42:57",
    #     "ends": "2024/10/05 20:42:57",
    #     "cltt": 1728168177,
    #     "binding": "active",
    #     "uid": "\\001^v#\\342Z\\277",
    #     "type": "dynamic",
    #     "status": "online",
    #     "descr": "",
    #     "mac": "5e:76:23:e2:5a:bf",
    #     "hostname": "",
    #     "state": "active",
    #     "man": "",
    #     "if": "opt8",
    #     "if_descr": "Guest_150"
    # }

    async def _get_isc_dhcpv6_leases(self) -> list:
        response: Mapping[str, Any] | list = await self._get(
            "/api/dhcpv6/leases/searchLease"
        )
        if not isinstance(response, Mapping):
            return []
        leases_info: list = response.get("rows", [])
        if not isinstance(leases_info, list):
            return []
        # _LOGGER.debug(f"[get_isc_dhcpv6_leases] leases_info: {leases_info}")
        leases: list = []
        for lease_info in leases_info:
            # _LOGGER.debug(f"[get_isc_dhcpv6_leases] lease_info: {lease_info}")
            if (
                not isinstance(lease_info, Mapping)
                or lease_info.get("state", "") != "active"
                or not lease_info.get("mac", None)
            ):
                continue
            lease: Mapping[str, Any] = {}
            lease["address"] = lease_info.get("address", None)
            lease["hostname"] = (
                lease_info.get("hostname", None)
                if isinstance(lease_info.get("hostname", None), str)
                and len(lease_info.get("hostname", "")) > 0
                else None
            )
            lease["if_descr"] = lease_info.get("if_descr", None)
            lease["if_name"] = lease_info.get("if", None)
            lease["type"] = lease_info.get("type", None)
            lease["mac"] = lease_info.get("mac", None)
            if lease_info.get("ends", None):
                dt: datetime = datetime.strptime(
                    lease_info.get("ends", None), "%Y/%m/%d %H:%M:%S"
                )
                lease["expires"] = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
                if lease["expires"] < datetime.now().astimezone():
                    continue
            else:
                lease["expires"] = lease_info.get("ends", None)
            leases.append(lease)
        # _LOGGER.debug(f"[get_isc_dhcpv6_leases] leases: {leases}")
        return leases

    # Legacy DHCPv6
    # {
    #     "type": "dynamic",
    #     "lease_type": "ia-na",
    #     "iaid": 0,
    #     "duid": "00:01:00:01:20:44:59:47:ac:bc:32:75:d6:af",
    #     "iaid_duid": "00:00:00:00:00:01:00:01:20:44:59:47:ac:bc:32:75:d6:af",
    #     "descr": "",
    #     "if": "opt5",
    #     "cltt": "2024/09/26 22:05:30",
    #     "state": "active",
    #     "ends": "2024/09/27 00:05:30",
    #     "address": "2600:4040:a506:ce32::1764",
    #     "status": "online",
    #     "man": "Apple, Inc.",
    #     "mac": "ac:bc:32:75:d6:ad",
    #     "if_descr": "IOT_50",
    # },

    @_log_errors
    async def get_carp_status(self) -> bool:
        response: Mapping[str, Any] | list = await self._get(
            "/api/diagnostics/interface/get_vip_status"
        )
        if not isinstance(response, Mapping):
            _LOGGER.error("Invalid data returned from get_carp_status")
            return False
        # _LOGGER.debug(f"[get_carp_status] response: {response}")
        return response.get("carp", {}).get("allow", "0") == "1"

    @_log_errors
    async def get_carp_interfaces(self) -> Mapping[str, Any]:
        script: str = r"""
global $config;

$vips = [];
if ($config['virtualip'] && is_iterable($config['virtualip']['vip'])) {
    foreach ($config['virtualip']['vip'] as $vip) {
        if ($vip["mode"] != "carp") {
            continue;
        }
        $vips[] = $vip;
    }
}

$intf_details = legacy_interfaces_details();

foreach ($vips as &$vip) {
  $intf = get_real_interface($vip['interface']);
  if (!empty($intf_details[$intf]) && !empty($intf_details[$intf]['carp'][$vip['vhid']])) {
    $status = $intf_details[$intf]['carp'][$vip['vhid']]['status'];
  } else {
    $status = "DISABLED";
  }

  $vip["status"] = $status;
}

$toreturn = [
  "data" => $vips,
];
"""
        response: Mapping[str, Any] = await self._exec_php(script)
        if not isinstance(response, Mapping):
            _LOGGER.error("Invalid data returned from get_carp_interfaces")
            return {}
        _LOGGER.debug(f"[get_carp_interfaces] exec_php response: {response}")
        get_vip_status_response: Mapping[str, Any] | list = await self._get(
            "/api/diagnostics/interface/get_vip_status"
        )
        vip_settings_response: Mapping[str, Any] | list = await self._get(
            "/api/interfaces/vip_settings/get"
        )
        vip_search_item_response: Mapping[str, Any] | list = await self._get(
            "/api/interfaces/vip_settings/searchItem"
        )
        lagg_settings_response: Mapping[str, Any] | list = await self._get(
            "/api/interfaces/lagg_settings/get"
        )
        lagg_search_item_response: Mapping[str, Any] | list = await self._get(
            "/api/interfaces/lagg_settings/searchItem"
        )
        vlan_settings_response: Mapping[str, Any] | list = await self._get(
            "/api/interfaces/vlan_settings/get"
        )
        vlan_search_item_response: Mapping[str, Any] | list = await self._get(
            "/api/interfaces/vlan_settings/searchItem"
        )
        vxlan_settings_response: Mapping[str, Any] | list = await self._get(
            "/api/interfaces/vxlan_settings/get"
        )
        vxlan_search_item_response: Mapping[str, Any] | list = await self._get(
            "/api/interfaces/vxlan_settings/searchItem"
        )
        interfaces_export_response: Mapping[str, Any] | list = await self._get(
            "/api/interfaces/overview/export"
        )
        _LOGGER.debug(
            f"[get_carp_interfaces] get_vip_status_response: {get_vip_status_response}"
        )
        _LOGGER.debug(
            f"[get_carp_interfaces] vip_settings_response: {vip_settings_response}"
        )
        _LOGGER.debug(
            f"[get_carp_interfaces] vip_search_item_response: {vip_search_item_response}"
        )
        _LOGGER.debug(
            f"[get_carp_interfaces] lagg_settings_response: {lagg_settings_response}"
        )
        _LOGGER.debug(
            f"[get_carp_interfaces] lagg_search_item_response: {lagg_search_item_response}"
        )
        _LOGGER.debug(
            f"[get_carp_interfaces] vlan_settings_response: {vlan_settings_response}"
        )
        _LOGGER.debug(
            f"[get_carp_interfaces] vlan_search_item_response: {vlan_search_item_response}"
        )
        _LOGGER.debug(
            f"[get_carp_interfaces] vxlan_settings_response: {vxlan_settings_response}"
        )
        _LOGGER.debug(
            f"[get_carp_interfaces] vxlan_search_item_response: {vxlan_search_item_response}"
        )
        _LOGGER.debug(
            f"[get_carp_interfaces] interfaces_export_response: {interfaces_export_response}"
        )
        return response.get("data", {})

    @_log_errors
    async def system_reboot(self) -> bool:
        response: Mapping[str, Any] | list = await self._post("/api/core/system/reboot")
        _LOGGER.debug(f"[system_reboot] response: {response}")
        if isinstance(response, Mapping) and response.get("status", "") == "ok":
            return True
        return False

    @_log_errors
    async def system_halt(self) -> None:
        response: Mapping[str, Any] | list = await self._post("/api/core/system/halt")
        _LOGGER.debug(f"[system_halt] response: {response}")
        if isinstance(response, Mapping) and response.get("status", "") == "ok":
            return True
        return False

    @_log_errors
    async def send_wol(self, interface, mac) -> bool:
        """
        interface should be wan, lan, opt1, opt2 etc, not the description
        """
        payload: Mapping[str, Any] = {"wake": {"interface": interface, "mac": mac}}
        _LOGGER.debug(f"[send_wol] payload: {payload}")
        response = await self._post("/api/wol/wol/set", payload)
        _LOGGER.debug(f"[send_wol] response: {response}")
        if isinstance(response, Mapping) and response.get("status", "") == "ok":
            return True
        return False

    def _try_to_int(self, input, retval=None) -> int | None:
        try:
            return int(input)
        except (ValueError, TypeError):
            return retval

    def _try_to_float(self, input, retval=None) -> int | None:
        try:
            return float(input)
        except (ValueError, TypeError):
            return retval

    @_log_errors
    async def get_telemetry(self) -> Mapping[str, Any]:
        if not self._firmware_version:
            await self.get_host_firmware_version()
        try:
            if awesomeversion.AwesomeVersion(
                self._firmware_version
            ) < awesomeversion.AwesomeVersion("24.7"):
                _LOGGER.info("Using legacy get_telemetry method for OPNsense < 24.7")
                return await self._get_telemetry_legacy()
        except awesomeversion.exceptions.AwesomeVersionCompareException:
            pass
        telemetry: Mapping[str, Any] = {}
        telemetry["mbuf"] = await self._get_telemetry_mbuf()
        telemetry["pfstate"] = await self._get_telemetry_pfstate()
        telemetry["memory"] = await self._get_telemetry_memory()
        telemetry["system"] = await self._get_telemetry_system()
        telemetry["cpu"] = await self._get_telemetry_cpu()
        telemetry["filesystems"] = await self._get_telemetry_filesystems()
        telemetry["temps"] = await self._get_telemetry_temps()
        # _LOGGER.debug(f"[get_telemetry] telemetry: {telemetry}")
        return telemetry

    @_log_errors
    async def get_interfaces(self) -> Mapping[str, Any]:
        interface_info: Mapping[str, Any] | list = await self._get(
            "/api/interfaces/overview/export"
        )
        # _LOGGER.debug(f"[get_interfaces] interface_info: {interface_info}")
        if not isinstance(interface_info, list) or not len(interface_info) > 0:
            return {}
        interfaces: Mapping[str, Any] = {}
        for ifinfo in interface_info:
            interface: Mapping[str, Any] = {}
            if not isinstance(ifinfo, Mapping) or ifinfo.get("identifier", "") == "":
                continue
            interface["inpkts"] = self._try_to_int(
                ifinfo.get("statistics", {}).get("packets received", None)
            )
            interface["outpkts"] = self._try_to_int(
                ifinfo.get("statistics", {}).get("packets transmitted", None)
            )
            interface["inbytes"] = self._try_to_int(
                ifinfo.get("statistics", {}).get("bytes received", None)
            )
            interface["outbytes"] = self._try_to_int(
                ifinfo.get("statistics", {}).get("bytes transmitted", None)
            )
            interface["inbytes_frmt"] = self._try_to_int(
                ifinfo.get("statistics", {}).get("bytes received", None)
            )
            interface["outbytes_frmt"] = self._try_to_int(
                ifinfo.get("statistics", {}).get("bytes transmitted", None)
            )
            interface["inerrs"] = self._try_to_int(
                ifinfo.get("statistics", {}).get("input errors", None)
            )
            interface["outerrs"] = self._try_to_int(
                ifinfo.get("statistics", {}).get("output errors", None)
            )
            interface["collisions"] = self._try_to_int(
                ifinfo.get("statistics", {}).get("collisions", None)
            )
            interface["descr"] = ifinfo.get("identifier", "")
            interface["name"] = ifinfo.get("description", "")
            interface["status"] = ""
            if ifinfo.get("status", "") in ("down", "no carrier", "up"):
                interface["status"] = ifinfo.get("status", "")
            elif ifinfo.get("status", "") in ("associated"):
                interface["status"] = "up"
            interface["ipaddr"] = ifinfo.get("addr4", "")
            interface["media"] = ifinfo.get("media", "")
            interfaces[ifinfo.get("identifier", "")] = interface
        # _LOGGER.debug(f"[get_interfaces] interfaces: {interfaces}")
        return interfaces

    @_log_errors
    async def _get_telemetry_mbuf(self) -> Mapping[str, Any]:
        mbuf_info: Mapping[str, Any] | list = await self._post(
            "/api/diagnostics/system/system_mbuf"
        )
        # _LOGGER.debug(f"[get_telemetry_mbuf] mbuf_info: {mbuf_info}")
        if not isinstance(mbuf_info, Mapping):
            return {}
        mbuf: Mapping[str, Any] = {}
        mbuf["used"] = self._try_to_int(
            mbuf_info.get("mbuf-statistics", {}).get("mbuf-current", None)
        )
        mbuf["total"] = self._try_to_int(
            mbuf_info.get("mbuf-statistics", {}).get("mbuf-total", None)
        )
        mbuf["used_percent"] = (
            round(mbuf["used"] / mbuf["total"] * 100)
            if isinstance(mbuf["used"], int)
            and isinstance(mbuf["total"], int)
            and mbuf["total"] > 0
            else None
        )
        # _LOGGER.debug(f"[get_telemetry_mbuf] mbuf: {mbuf}")
        return mbuf

    @_log_errors
    async def _get_telemetry_pfstate(self) -> Mapping[str, Any]:
        pfstate_info: Mapping[str, Any] | list = await self._post(
            "/api/diagnostics/firewall/pf_states"
        )
        # _LOGGER.debug(f"[get_telemetry_pfstate] pfstate_info: {pfstate_info}")
        if not isinstance(pfstate_info, Mapping):
            return {}
        pfstate: Mapping[str, Any] = {}
        pfstate["used"] = self._try_to_int(pfstate_info.get("current", None))
        pfstate["total"] = self._try_to_int(pfstate_info.get("limit", None))
        pfstate["used_percent"] = (
            round(pfstate["used"] / pfstate["total"] * 100)
            if isinstance(pfstate["used"], int)
            and isinstance(pfstate["total"], int)
            and pfstate["total"] > 0
            else None
        )
        # _LOGGER.debug(f"[get_telemetry_pfstate] pfstate: {pfstate}")
        return pfstate

    @_log_errors
    async def _get_telemetry_memory(self) -> Mapping[str, Any]:
        memory_info: Mapping[str, Any] | list = await self._post(
            "/api/diagnostics/system/systemResources"
        )
        # _LOGGER.debug(f"[get_telemetry_memory] memory_info: {memory_info}")
        if not isinstance(memory_info, Mapping):
            return {}
        memory: Mapping[str, Any] = {}
        memory["physmem"] = self._try_to_int(
            memory_info.get("memory", {}).get("total", None)
        )
        memory["used"] = self._try_to_int(
            memory_info.get("memory", {}).get("used", None)
        )
        memory["used_percent"] = (
            round(memory["used"] / memory["physmem"] * 100)
            if isinstance(memory["used"], int)
            and isinstance(memory["physmem"], int)
            and memory["physmem"] > 0
            else None
        )
        swap_info: Mapping[str, Any] = await self._post(
            "/api/diagnostics/system/system_swap"
        )
        if (
            not isinstance(swap_info, Mapping)
            or not isinstance(swap_info.get("swap", None), list)
            or not len(swap_info.get("swap", [])) > 0
            or not isinstance(swap_info.get("swap", [])[0], Mapping)
        ):
            return memory
        # _LOGGER.debug(f"[get_telemetry_memory] swap_info: {swap_info}")
        memory["swap_total"] = self._try_to_int(
            swap_info.get("swap", [])[0].get("total", None)
        )
        memory["swap_reserved"] = self._try_to_int(
            swap_info["swap"][0].get("used", None)
        )
        memory["swap_used_percent"] = (
            round(memory["swap_reserved"] / memory["swap_total"] * 100)
            if isinstance(memory["swap_reserved"], int)
            and isinstance(memory["swap_total"], int)
            and memory["swap_total"] > 0
            else 0
        )
        # _LOGGER.debug(f"[get_telemetry_memory] memory: {memory}")
        return memory

    @_log_errors
    async def _get_telemetry_system(self) -> Mapping[str, Any]:
        time_info: Mapping[str, Any] | list = await self._post(
            "/api/diagnostics/system/systemTime"
        )
        # _LOGGER.debug(f"[get_telemetry_system] time_info: {time_info}")
        if not isinstance(time_info, Mapping):
            return {}
        system: Mapping[str, Any] = {}
        pattern = re.compile(r"^(?:(\d+)\s+days?,\s+)?(\d{2}):(\d{2}):(\d{2})$")
        match = pattern.match(time_info.get("uptime", ""))
        if match:
            days_str, hours_str, minutes_str, seconds_str = match.groups()
            days: int = self._try_to_int(days_str, 0)
            hours: int = self._try_to_int(hours_str, 0)
            minutes: int = self._try_to_int(minutes_str, 0)
            seconds: int = self._try_to_int(seconds_str, 0)
            system["uptime"] = days * 86400 + hours * 3600 + minutes * 60 + seconds

            boottime: datetime = datetime.now() - timedelta(seconds=system["uptime"])
            system["boottime"] = boottime.timestamp()
        else:
            _LOGGER.warning("Invalid uptime format")

        load_str: str = time_info.get("loadavg", "")
        load_list: list[str] = load_str.split(", ")
        if len(load_list) == 3:
            system["load_average"] = {
                "one_minute": float(load_list[0]),
                "five_minute": float(load_list[1]),
                "fifteen_minute": float(load_list[2]),
            }
        else:
            system["load_average"] = {
                "one_minute": None,
                "five_minute": None,
                "fifteen_minute": None,
            }
        # _LOGGER.debug(f"[get_telemetry_system] system: {system}")
        return system

    @_log_errors
    async def _get_telemetry_cpu(self) -> Mapping[str, Any]:
        cputype_info: Mapping[str, Any] | list = await self._post(
            "/api/diagnostics/cpu_usage/getCPUType"
        )
        # _LOGGER.debug(f"[get_telemetry_cpu] cputype_info: {cputype_info}")
        if not isinstance(cputype_info, list) or not len(cputype_info) > 0:
            return {}
        cpu: Mapping[str, Any] = {}
        cores_match = re.search(r"\((\d+) cores", cputype_info[0])
        cpu["count"] = self._try_to_int(cores_match.group(1)) if cores_match else 0

        cpustream_info: Mapping[str, Any] | list = await self._get_from_stream(
            "/api/diagnostics/cpu_usage/stream"
        )
        # {"total":29,"user":2,"nice":0,"sys":27,"intr":0,"idle":70}
        # _LOGGER.debug(f"[get_telemetry_cpu] cpustream_info: {cpustream_info}")
        if not isinstance(cpustream_info, Mapping):
            return cpu
        cpu["usage_total"] = self._try_to_int(cpustream_info.get("total", None))
        cpu["usage_user"] = self._try_to_int(cpustream_info.get("user", None))
        cpu["usage_nice"] = self._try_to_int(cpustream_info.get("nice", None))
        cpu["usage_system"] = self._try_to_int(cpustream_info.get("sys", None))
        cpu["usage_interrupt"] = self._try_to_int(cpustream_info.get("intr", None))
        cpu["usage_idle"] = self._try_to_int(cpustream_info.get("idle", None))
        # _LOGGER.debug(f"[get_telemetry_cpu] cpu: {cpu}")
        return cpu

    @_log_errors
    async def _get_telemetry_filesystems(self) -> list:
        filesystems_info: Mapping[str, Any] | list = await self._post(
            "/api/diagnostics/system/systemDisk"
        )
        if not isinstance(filesystems_info, Mapping):
            return []
        # _LOGGER.debug(f"[get_telemetry_filesystems] filesystems_info: {filesystems_info}")
        filesystems: list = filesystems_info.get("devices", [])
        # _LOGGER.debug(f"[get_telemetry_filesystems] filesystems: {filesystems}")
        return filesystems

    @_log_errors
    async def get_openvpn(self) -> Mapping[str, Any]:
        openvpn_info: Mapping[str, Any] | list = await self._post(
            "/api/openvpn/export/providers"
        )
        # _LOGGER.debug(f"[get_openvpn] openvpn_info: {openvpn_info}")
        if not isinstance(openvpn_info, Mapping):
            return {}
        openvpn: Mapping[str, Any] = {}
        openvpn["servers"] = {}
        connection_info: Mapping[str, Any] = await self._post(
            "/api/openvpn/service/searchSessions"
        )
        # _LOGGER.debug(f"[get_openvpn] connection_info: {connection_info}")
        if connection_info is None or not isinstance(connection_info, Mapping):
            return {}
        for vpnid, vpn_info in openvpn_info.items():
            vpn: Mapping[str, Any] = {}
            vpn["vpnid"] = vpn_info.get("vpnid", "")
            vpn["name"] = vpn_info.get("name", "")
            total_bytes_recv = 0
            total_bytes_sent = 0
            for connect in connection_info.get("rows", {}):
                id = connect.get("id", None)
                vpn_id = vpn.get("vpnid", None)
                if id and (
                    id == vpn_id
                    or (isinstance(id, str) and id.startswith(vpn_id + "_"))
                ):
                    total_bytes_recv += self._try_to_int(
                        connect.get("bytes_received", 0), 0
                    )
                    total_bytes_sent += self._try_to_int(
                        connect.get("bytes_sent", 0), 0
                    )
            vpn["total_bytes_recv"] = total_bytes_recv
            vpn["total_bytes_sent"] = total_bytes_sent
            openvpn["servers"][vpnid] = vpn
        # _LOGGER.debug(f"[get_openvpn] openvpn: {openvpn}")
        return openvpn

    @_log_errors
    async def get_gateways(self) -> Mapping[str, Any]:
        gateways_info: Mapping[str, Any] | list = await self._post(
            "/api/routes/gateway/status"
        )
        # _LOGGER.debug(f"[get_gateways] gateways_info: {gateways_info}")
        if not isinstance(gateways_info, Mapping):
            return {}
        gateways: Mapping[str, Any] = {}
        for gw_info in gateways_info.get("items", []):
            if isinstance(gw_info, Mapping) and "name" in gw_info:
                gateways[gw_info["name"]] = gw_info
        for gateway in gateways.values():
            gateway["status"] = gateway.pop(
                "status_translated", gateway.get("status", "")
            ).lower()
        # _LOGGER.debug(f"[get_gateways] gateways: {gateways}")
        return gateways

    @_log_errors
    async def _get_telemetry_temps(self) -> Mapping[str, Any]:
        temps_info: Mapping[str, Any] | list = await self._post(
            "/api/diagnostics/system/systemTemperature"
        )
        # _LOGGER.debug(f"[get_telemetry_temps] temps_info: {temps_info}")
        if not isinstance(temps_info, list) or not len(temps_info) > 0:
            return {}
        temps: Mapping[str, Any] = {}
        for i, temp_info in enumerate(temps_info):
            temp: Mapping[str, Any] = {}
            temp["temperature"] = self._try_to_float(temp_info.get("temperature", 0), 0)
            temp["name"] = (
                f"{temp_info.get('type_translated', 'Num')} {temp_info.get('device_seq', i)}"
            )
            temp["device_id"] = temp_info.get("device", str(i))
            temps[temp_info.get("device", str(i)).replace(".", "_")] = temp
        # _LOGGER.debug(f"[get_telemetry_temps] temps: {temps}")
        return temps

    @_log_errors
    async def _get_telemetry_legacy(self) -> Mapping[str, Any]:
        script: str = r"""
require_once '/usr/local/www/widgets/api/plugins/system.inc';

$system_api_data = system_api();

// OPNsense 23.1.1: replaced single exec_command() with new shell_safe() wrapper
if (function_exists('exec_command')) {
    $boottime = exec_command("sysctl kern.boottime");
} else {
    $boottime = shell_safe("sysctl kern.boottime");
}

// kern.boottime: { sec = 1634047554, usec = 237429 } Tue Oct 12 08:05:54 2021
preg_match("/sec = [0-9]*/", $boottime, $matches);
$boottime = $matches[0];
$boottime = explode("=", $boottime)[1];
$boottime = (int) trim($boottime);

$toreturn = [
    "pfstate" => [
        "used" => (int) $system_api_data["kernel"]["pf"]["states"],
        "total" => (int) $system_api_data["kernel"]["pf"]["maxstates"],
        "used_percent" => round(floatval($system_api_data["kernel"]["pf"]["states"] / $system_api_data["kernel"]["pf"]["maxstates"]) * 100, 0),
    ],

    "mbuf" => [
        "used" => (int) $system_api_data["kernel"]["mbuf"]["total"],
        "total" => (int) $system_api_data["kernel"]["mbuf"]["max"],
        "used_percent" =>  round(floatval($system_api_data["kernel"]["mbuf"]["total"] / $system_api_data["kernel"]["mbuf"]["max"]) * 100, 0),
    ],

    "memory" => [
        "swap_used_percent" => ($system_api_data["disk"]["swap"][0]["total"] > 0) ? round(floatval($system_api_data["disk"]["swap"][0]["used"] / $system_api_data["disk"]["swap"][0]["total"]) * 100, 0) : 0,
        "used_percent" => round(floatval($system_api_data["kernel"]["memory"]["used"] / $system_api_data["kernel"]["memory"]["total"]) * 100, 0),
        "physmem" => (int) $system_api_data["kernel"]["memory"]["total"],
        "used" => (int) $system_api_data["kernel"]["memory"]["used"],
        "swap_total" => (int) $system_api_data["disk"]["swap"][0]["total"],
        "swap_reserved" => (int) $system_api_data["disk"]["swap"][0]["used"],
    ],

    "system" => [
        "boottime" => $boottime,
        "uptime" => (int) $system_api_data["uptime"],
        "load_average" => [
            "one_minute" => floatval(trim($system_api_data["cpu"]["load"][0])),
            "five_minute" => floatval(trim($system_api_data["cpu"]["load"][1])),
            "fifteen_minute" => floatval(trim($system_api_data["cpu"]["load"][2])),
        ],
    ],

    "cpu" => [
        "count" => (int) $system_api_data["cpu"]["cur.freq"],
    ],

    "filesystems" => $system_api_data["disk"]["devices"],

];

"""
        telemetry: Mapping[str, Any] = await self._exec_php(script)
        if not isinstance(telemetry, Mapping):
            _LOGGER.error("Invalid data returned from get_telemetry_legacy")
            return {}
        if isinstance(telemetry.get("gateways", []), list):
            telemetry["gateways"] = {}
        if isinstance(telemetry.get("filesystems", []), list):
            for filesystem in telemetry.get("filesystems", []):
                filesystem["blocks"] = filesystem.pop("size", None)
                try:
                    filesystem["used_pct"] = int(
                        filesystem.pop("capacity", "").strip("%")
                    )
                except ValueError:
                    filesystem.pop("capacity", None)
        # _LOGGER.debug(f"[get_telemetry_legacy] telemetry: {telemetry}")
        return telemetry

    @_log_errors
    async def get_notices(self) -> list:
        notices_info: Mapping[str, Any] | list = await self._get(
            "/api/core/system/status"
        )
        # _LOGGER.debug(f"[get_notices] notices_info: {notices_info}")

        if not isinstance(notices_info, Mapping):
            return []
        pending_notices_present = False
        pending_notices: list = []
        for key, notice in notices_info.items():
            if isinstance(notices_info, Mapping) and notice.get("statusCode", 2) != 2:
                pending_notices_present = True
                real_notice: Mapping[str, Any] = {}
                real_notice["notice"] = notice.get("message", None)
                real_notice["id"] = key
                real_notice["created_at"] = notice.get("timestamp", None)
                pending_notices.append(real_notice)

        notices: Mapping[str, Any] = {}
        notices["pending_notices_present"] = pending_notices_present
        notices["pending_notices"] = pending_notices
        _LOGGER.debug(f"[get_notices] notices: {notices}")
        return notices

    @_log_errors
    async def close_notice(self, id) -> bool:
        """
        id = "all" to wipe everything
        """
        success = True
        if id.lower() == "all":
            notices: Mapping[str, Any] | list = await self._get(
                "/api/core/system/status"
            )
            _LOGGER.debug(f"[close_notice] notices: {notices}")

            if not isinstance(notices, Mapping):
                return False
            for key, notice in notices.items():
                if "statusCode" in notice:
                    dismiss: Mapping[str, Any] | list = await self._post(
                        "/api/core/system/dismissStatus", payload={"subject": key}
                    )
                    _LOGGER.debug(f"[close_notice] id: {key}, dismiss: {dismiss}")
                    if (
                        not isinstance(dismiss, Mapping)
                        or dismiss.get("status", "failed") != "ok"
                    ):
                        success = False
        else:
            dismiss: Mapping[str, Any] | list = await self._post(
                "/api/core/system/dismissStatus", payload={"subject": id}
            )
            _LOGGER.debug(f"[close_notice] id: {id}, dismiss: {dismiss}")
            if (
                not isinstance(dismiss, Mapping)
                or dismiss.get("status", "failed") != "ok"
            ):
                success = False
        _LOGGER.debug(f"[close_notice] success: {success}")
        return success

    @_log_errors
    async def get_unbound_blocklist(self) -> Mapping[str, Any]:
        response: Mapping[str, Any] | list = await self._get(
            "/api/unbound/settings/get"
        )
        if not isinstance(response, Mapping):
            _LOGGER.error("Invalid data returned from get_unbound_blocklist")
            return {}
        # _LOGGER.debug(f"[get_unbound_blocklist] response: {response}")
        dnsbl_settings = response.get("unbound", {}).get("dnsbl", {})
        # _LOGGER.debug(f"[get_unbound_blocklist] dnsbl_settings: {dnsbl_settings}")
        if not isinstance(dnsbl_settings, Mapping):
            return {}
        dnsbl = {}
        for attr in ["enabled", "safesearch", "nxdomain", "address"]:
            dnsbl[attr] = dnsbl_settings.get(attr, "")
        for attr in ["type", "lists", "whitelists", "blocklists", "wildcards"]:
            if isinstance(dnsbl_settings.get(attr, None), Mapping):
                dnsbl[attr] = ",".join(
                    [
                        key
                        for key, value in dnsbl_settings.get(attr, {}).items()
                        if isinstance(value, Mapping) and value.get("selected", 0) == 1
                    ]
                )
            else:
                dnsbl[attr] = ""
        _LOGGER.debug(f"[get_unbound_blocklist] dnsbl: {dnsbl}")
        return dnsbl

    async def _set_unbound_blocklist(self, set_state: bool) -> bool:
        payload: Mapping[str, Any] = {}
        payload["unbound"] = {}
        payload["unbound"]["dnsbl"] = await self.get_unbound_blocklist()
        if not payload["unbound"]["dnsbl"]:
            _LOGGER.error("Unable to get Unbound Blocklist Status")
            return False
        if set_state:
            payload["unbound"]["dnsbl"]["enabled"] = "1"
        else:
            payload["unbound"]["dnsbl"]["enabled"] = "0"
        response: Mapping[str, Any] | list = await self._post(
            "/api/unbound/settings/set", payload=payload
        )
        dnsbl_resp: Mapping[str, Any] | list = await self._get(
            "/api/unbound/service/dnsbl"
        )
        restart_resp: Mapping[str, Any] | list = await self._post(
            "/api/unbound/service/restart"
        )
        _LOGGER.debug(
            f"[set_unbound_blocklist] set_state: {'On' if set_state else 'Off'}, payload: {payload}, response: {response}, dnsbl_resp: {dnsbl_resp}, restart_resp: {restart_resp}"
        )
        return (
            isinstance(response, Mapping)
            and isinstance(dnsbl_resp, Mapping)
            and isinstance(restart_resp, Mapping)
            and response.get("result", "failed") == "saved"
            and dnsbl_resp.get("status", "failed").startswith("OK")
            and restart_resp.get("response", "failed") == "OK"
        )

    @_log_errors
    async def enable_unbound_blocklist(self) -> bool:
        return await self._set_unbound_blocklist(set_state=True)

    @_log_errors
    async def disable_unbound_blocklist(self) -> bool:
        return await self._set_unbound_blocklist(set_state=False)
