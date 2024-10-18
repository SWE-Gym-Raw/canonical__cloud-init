# Copyright 2022 Red Hat, Inc.
#
# Author: Lubomir Rintel <lkundrak@v3.sk>
# Fixes and suggestions contributed by James Falcon, Neal Gompa,
# Zbigniew Jędrzejewski-Szmek and Emanuele Giuseppe Esposito.
#
# This file is part of cloud-init. See LICENSE file for license information.

import configparser
import io
import itertools
import logging
import os
import uuid
from typing import List, Optional

from cloudinit import subp, util
from cloudinit.net import (
    is_ipv6_address,
    is_ipv6_network,
    renderer,
    subnet_is_ipv6,
)
from cloudinit.net.network_state import NetworkState
from cloudinit.net.sysconfig import available_nm_ifcfg_rh

NM_RUN_DIR = "/etc/NetworkManager"
NM_LIB_DIR = "/usr/lib/NetworkManager"
IFCFG_CFG_FILE = "/etc/sysconfig/network-scripts"
NM_IPV6_ADDR_GEN_CONF = """# This is generated by cloud-init. Do not edit.
#
[.config]
  enable=nm-version-min:1.40
[connection.30-cloud-init-ip6-addr-gen-mode]
  # Select EUI64 to be used if the profile does not specify it.
  ipv6.addr-gen-mode=0

"""
LOG = logging.getLogger(__name__)


class NMConnection:
    """Represents a NetworkManager connection profile."""

    def __init__(self, con_id):
        """
        Initializes the connection with some very basic properties,
        notably the UUID so that the connection can be referred to.
        """

        # Chosen by fair dice roll
        CI_NM_UUID = uuid.UUID("a3924cb8-09e0-43e9-890b-77972a800108")

        self.config = configparser.ConfigParser()
        # Identity option name mapping, to achieve case sensitivity
        self.config.optionxform = str

        self.config["connection"] = {
            "id": f"cloud-init {con_id}",
            "uuid": str(uuid.uuid5(CI_NM_UUID, con_id)),
            "autoconnect-priority": "120",
        }

        # This is not actually used anywhere, but may be useful in future
        self.config["user"] = {
            "org.freedesktop.NetworkManager.origin": "cloud-init"
        }

    def _set_default(self, section, option, value):
        """
        Sets a property unless it's already set, ensuring the section
        exists.
        """

        if not self.config.has_section(section):
            self.config[section] = {}
        if not self.config.has_option(section, option):
            self.config[section][option] = value

    def _config_option_is_set(self, section, option):
        """
        Checks if a config option is set. Returns True if it is,
        else returns False.
        """
        return self.config.has_section(section) and self.config.has_option(
            section, option
        )

    def _get_config_option(self, section, option):
        """
        Returns the value of a config option if its set,
        else returns None.
        """
        if self._config_option_is_set(section, option):
            return self.config[section][option]
        else:
            return None

    def _change_set_config_option(self, section, option, value):
        """
        Overrides the value of a config option if its already set.
        Else, if the config option is not set, it does nothing.
        """
        if self._config_option_is_set(section, option):
            self.config[section][option] = value

    def _set_mayfail_true_if_both_false_dhcp(self):
        """
        If for both ipv4 and ipv6, 'may-fail' is set to be False,
        set it to True for both of them.
        """
        for family in ["ipv4", "ipv6"]:
            if self._get_config_option(family, "may-fail") != "false":
                # if either ipv4 or ipv6 sections are not set/configured,
                # or if both are configured but for either ipv4 or ipv6,
                # 'may-fail' is not 'false', do not do anything.
                return
            if self._get_config_option(family, "method") not in [
                "dhcp",
                "auto",
            ]:
                # if both v4 and v6 are not dhcp, do not do anything.
                return

        # If we landed here, it means both ipv4 and ipv6 are configured
        # with dhcp/auto and both have 'may-fail' set to 'false'. So set
        # both to 'true'.
        for family in ["ipv4", "ipv6"]:
            self._change_set_config_option(family, "may-fail", "true")

    def _set_ip_method(self, family, subnet_type):
        """
        Ensures there's appropriate [ipv4]/[ipv6] for given family
        appropriate for given configuration type
        """

        method_map = {
            "static": "manual",
            "static6": "manual",
            "dhcp6": "auto",
            "ipv6_slaac": "auto",
            "ipv6_dhcpv6-stateless": "auto",
            "ipv6_dhcpv6-stateful": "dhcp",
            "dhcp4": "auto",
            "dhcp": "auto",
        }

        # Ensure we have an [ipvX] section, default to disabled
        method = "disabled"
        self._set_default(family, "method", method)

        try:
            if subnet_type:
                method = method_map[subnet_type]
        except KeyError:
            # What else can we do
            method = "auto"
            self.config[family]["may-fail"] = "true"

        # Make sure we don't "downgrade" the method in case
        # we got conflicting subnets (e.g. static along with dhcp)
        if self.config[family]["method"] == "dhcp":
            return
        if self.config[family]["method"] == "auto" and method == "manual":
            return

        if subnet_type in [
            "ipv6_dhcpv6-stateful",
            "ipv6_dhcpv6-stateless",
            "ipv6_slaac",
        ]:
            # set ipv4 method to 'disabled' to align with sysconfig renderer.
            self._set_default("ipv4", "method", "disabled")

        self.config[family]["method"] = method

        # Network Manager sets the value of `may-fail` to `True` by default.
        # Please see https://www.networkmanager.dev/docs/api/1.32.10/settings-ipv6.html.
        # Therefore, when no configuration for ipv4 or ipv6 is specified,
        # `may-fail = True` applies. When the user explicitly configures ipv4
        # or ipv6, `may-fail` is set to `False`. This is so because it is
        # assumed that a network failure with the user provided configuration
        # is unexpected. In other words, we think that the user knows what
        # works in their target environment and what does not and they have
        # correctly configured cloud-init network configuration such that
        # it works in that environment. When no such configuration is
        # specified, we do not know what would work and what would not in
        # user's environment. Therefore, we are more conservative in assuming
        # that failure with ipv4 or ipv6 can be expected or tolerated.
        self._set_default(family, "may-fail", "false")

    def _get_next_numbered_section(self, section, key_prefix) -> str:
        if not self.config.has_section(section):
            self.config[section] = {}
        for index in itertools.count(1):
            key = f"{key_prefix}{index}"
            if not self.config.has_option(section, key):
                return key
        return "not_possible"  # for typing

    def _add_numbered(self, section, key_prefix, value):
        """
        Adds a numbered property, such as address<n> or route<n>, ensuring
        the appropriate value gets used for <n>.
        """
        key = self._get_next_numbered_section(section, key_prefix)
        self.config[section][key] = value

    def _add_route_options(self, section, route, key, value):
        """Add route options to a given route

        Example:
        Given:
          section: ipv4
          route: route0
          key: mtu
          value: 500

        Create line under [ipv4] section:
            route0_options=mtu=500

        If the line already exists, then append the new key/value pair
        """
        numbered_key = f"{route}_options"
        route_options = self.config[section].get(numbered_key)
        self.config[section][numbered_key] = (
            f"{route_options},{key}={value}"
            if route_options
            else f"{key}={value}"
        )

    def _add_address(self, family, subnet):
        """
        Adds an ipv[46]address<n> property.
        """

        value = subnet["address"] + "/" + str(subnet["prefix"])
        self._add_numbered(family, "address", value)

    def _add_route(self, route):
        """Adds a ipv[46].route<n> property."""
        # Because network v2 route definitions can have mixed v4 and v6
        # routes, determine the family per route based on the network
        family = "ipv6" if is_ipv6_network(route["network"]) else "ipv4"
        value = f'{route["network"]}/{route["prefix"]}'
        if "gateway" in route:
            value += f',{route["gateway"]}'
        route_key = self._get_next_numbered_section(family, "route")
        self.config[family][route_key] = value
        if "mtu" in route:
            self._add_route_options(family, route_key, "mtu", route["mtu"])

    def _add_nameserver(self, dns: str) -> None:
        """
        Extends the ipv[46].dns property with a name server.
        """
        family = "ipv6" if is_ipv6_address(dns) else "ipv4"
        if (
            self.config.has_section(family)
            and self._get_config_option(family, "method") != "disabled"
        ):
            self._set_default(family, "dns", "")
            self.config[family]["dns"] = self.config[family]["dns"] + dns + ";"

    def _add_dns_search(self, dns_search: List[str]) -> None:
        """
        Extends the ipv[46].dns-search property with a name server.
        """
        for family in ["ipv4", "ipv6"]:
            if (
                self.config.has_section(family)
                and self._get_config_option(family, "method") != "disabled"
            ):
                self._set_default(family, "dns-search", "")
                self.config[family]["dns-search"] = (
                    self.config[family]["dns-search"]
                    + ";".join(dns_search)
                    + ";"
                )

    def con_uuid(self):
        """
        Returns the connection UUID
        """
        return self.config["connection"]["uuid"]

    def valid(self):
        """
        Can this be serialized into a meaningful connection profile?
        """
        return self.config.has_option("connection", "type")

    @staticmethod
    def mac_addr(addr):
        """
        Sanitize a MAC address.
        """
        return addr.replace("-", ":").upper()

    def render_interface(self, iface, network_state, renderer):
        """
        Integrate information from network state interface information
        into the connection. Most of the work is done here.
        """

        # Initialize type & connectivity
        _type_map = {
            "physical": "ethernet",
            "vlan": "vlan",
            "bond": "bond",
            "bridge": "bridge",
            "infiniband": "infiniband",
            "loopback": None,
        }

        if_type = _type_map[iface["type"]]
        if if_type is None:
            return
        if "bond-master" in iface:
            slave_type = "bond"
        else:
            slave_type = None

        self.config["connection"]["type"] = if_type
        if slave_type is not None:
            self.config["connection"]["slave-type"] = slave_type
            self.config["connection"]["master"] = renderer.con_ref(
                iface[slave_type + "-master"]
            )

        # Add type specific-section
        self.config[if_type] = {}

        # These are the interface properties that map nicely
        # to NetworkManager properties
        # NOTE: Please ensure these items are formatted so as
        # to match the schema in schema-network-config-v1.json
        #
        # Supported parameters
        # https://networkmanager.dev/docs/libnm/latest/NMSettingBond.html#NMSettingBond.other
        # https://www.kernel.org/doc/Documentation/networking/bonding.txt
        _prop_map = {
            "bond": {
                "mode": "bond-mode",
                "miimon": "bond-miimon",
                # only in balance-xor(2), 802.3ad(4), balance-tlb(5)
                "xmit_hash_policy": "bond-xmit_hash_policy",
                # only in active-backup(1)
                "num_grat_arp": "bond-num_grat_arp",
                "downdelay": "bond-downdelay",
                "updelay": "bond-updelay",
                "fail_over_mac": "bond-fail_over_mac",
                # only in active-backup(1), balance-tlb(5), balance-alb(6)
                "primary_reselect": "bond-primary_reselect",
                # only in active-backup(1), balance-tlb(5), balance-alb(6)
                "primary": "bond-primary",
                # only in active-backup(1), balance-tlb(5), balance-alb(6)
                "active_slave": "bond-active_slave",
                # only in 802.3ad(4)
                "ad_actor_sys_prio": "bond-ad_actor_sys_prio",
                # only in 802.3ad(4)
                "ad_actor_system": "bond-ad_actor_system",
                # only in 802.3ad(4)
                "ad_select": "bond-ad_select",
                # only in 802.3ad(4)
                "ad_user_port_key": "bond-ad_user_port_key",
                "all_slaves_active": "bond-all_slaves_active",
                "arp_all_targets": "bond-arp_all_targets",
                "arp_interval": "bond-arp_interval",
                "arp_ip_target": "bond-arp_ip_target",
                "arp_validate": "bond-arp_validate",
                # only in 802.3ad(4)
                "lacp_rate": "bond-lacp_rate",
                # only in balance-tlb(5), balance-alb(6)
                "lp_interval": "bond-lp_interval",
                # only in 802.3ad(4)
                "min_links": "bond-min_links",
                # only in active-backup(1)
                "num_unsol_na": "bond-num_unsol_na",
                # only in balance-rr(0)
                "packets_per_slave": "bond-packets_per_slave",
                # only in active-backup(1)
                "peer_notif_delay": "bond-peer_notif_delay",
                # only in active-backup(1), balance rr(0), tlb(5), alb(6)
                "resend_igmp": "bond-resend_igmp",
                # only in balance-tlb(5)
                "tlb_dynamic_lb": "bond-tlb_dynamic_lb",
                "use_carrier": "bond-use_carrier",
            },
            "bridge": {
                "stp": "bridge_stp",
                "priority": "bridge_bridgeprio",
            },
            "vlan": {
                "id": "vlan_id",
            },
            "ethernet": {},
            "infiniband": {},
        }

        device_mtu = iface["mtu"]
        ipv4_mtu = None
        found_nameservers = []
        found_dns_search = []

        # Deal with Layer 3 configuration
        if if_type == "bond" and not iface["subnets"]:
            # If there is no L3 subnet config for a given connection,
            # ensure it is disabled. Without this, the interface
            # defaults to 'auto' which implies DHCP. This is problematic
            # for certain configurations such as bonds where the root
            # device itself may not have a subnet config and should be
            # disabled while a separate VLAN interface on the bond holds
            # the subnet information.
            for family in ["ipv4", "ipv6"]:
                self._set_ip_method(family, None)

        for subnet in iface["subnets"]:
            family = "ipv6" if subnet_is_ipv6(subnet) else "ipv4"

            self._set_ip_method(family, subnet["type"])
            if "address" in subnet:
                self._add_address(family, subnet)
            if "gateway" in subnet:
                self.config[family]["gateway"] = subnet["gateway"]
            for route in subnet["routes"]:
                self._add_route(route)
            # Add subnet-level DNS
            if "dns_nameservers" in subnet:
                found_nameservers.extend(subnet["dns_nameservers"])
            if "dns_search" in subnet:
                found_dns_search.extend(subnet["dns_search"])
            if family == "ipv4" and "mtu" in subnet:
                ipv4_mtu = subnet["mtu"]

        # Add interface-level DNS
        if "dns" in iface:
            found_nameservers += [
                dns
                for dns in iface["dns"]["nameservers"]
                if dns not in found_nameservers
            ]
            found_dns_search += [
                search
                for search in iface["dns"]["search"]
                if search not in found_dns_search
            ]

        # We prefer any interface-specific DNS entries, but if we do not
        # have any, add the global DNS to the connection
        if not found_nameservers and network_state.dns_nameservers:
            found_nameservers = network_state.dns_nameservers
        if not found_dns_search and network_state.dns_searchdomains:
            found_dns_search = network_state.dns_searchdomains

        # Write out all DNS entries to the connection
        for nameserver in found_nameservers:
            self._add_nameserver(nameserver)
        if found_dns_search:
            self._add_dns_search(found_dns_search)

        # we do not want to set may-fail to false for both ipv4 and ipv6 dhcp
        # at the at the same time. This will make the network configuration
        # work only when both ipv4 and ipv6 dhcp succeeds. This may not be
        # what we want. If we have configured both ipv4 and ipv6 dhcp, any one
        # succeeding should be enough. Therefore, if "may-fail" is set to
        # False for both ipv4 and ipv6 dhcp, set them both to True.
        self._set_mayfail_true_if_both_false_dhcp()

        if ipv4_mtu is None:
            ipv4_mtu = device_mtu
        if not ipv4_mtu == device_mtu:
            LOG.warning(
                "Network config: ignoring %s device-level mtu:%s"
                " because ipv4 subnet-level mtu:%s provided.",
                iface["name"],
                device_mtu,
                ipv4_mtu,
            )

        # Parse type-specific properties
        for nm_prop, key in _prop_map[if_type].items():
            if key not in iface:
                continue
            if iface[key] is None:
                continue
            if isinstance(iface[key], bool):
                self.config[if_type][nm_prop] = (
                    "true" if iface[key] else "false"
                )
            else:
                self.config[if_type][nm_prop] = str(iface[key])

        # These ones need special treatment
        if if_type == "ethernet":
            if iface["wakeonlan"] is True:
                # NM_SETTING_WIRED_WAKE_ON_LAN_MAGIC
                self.config["ethernet"]["wake-on-lan"] = str(0x40)
            if ipv4_mtu is not None:
                self.config["ethernet"]["mtu"] = str(ipv4_mtu)
            if iface["mac_address"] is not None:
                self.config["ethernet"]["mac-address"] = self.mac_addr(
                    iface["mac_address"]
                )
        if if_type == "vlan" and "vlan-raw-device" in iface:
            self.config["vlan"]["parent"] = renderer.con_ref(
                iface["vlan-raw-device"]
            )
        if if_type == "bond" and ipv4_mtu is not None:
            if "ethernet" not in self.config:
                self.config["ethernet"] = {}
            self.config["ethernet"]["mtu"] = str(ipv4_mtu)
        if if_type == "bridge":
            # Bridge is ass-backwards compared to bond
            for port in iface["bridge_ports"]:
                port = renderer.get_conn(port)
                port._set_default("connection", "slave-type", "bridge")
                port._set_default("connection", "master", self.con_uuid())
            if iface["mac_address"] is not None:
                self.config["bridge"]["mac-address"] = self.mac_addr(
                    iface["mac_address"]
                )
        if if_type == "infiniband" and ipv4_mtu is not None:
            self.config["infiniband"]["transport-mode"] = "datagram"
            self.config["infiniband"]["mtu"] = str(ipv4_mtu)
            if iface["mac_address"] is not None:
                self.config["infiniband"]["mac-address"] = self.mac_addr(
                    iface["mac_address"]
                )

        # Finish up
        if if_type == "bridge" or not self.config.has_option(
            if_type, "mac-address"
        ):
            self.config["connection"]["interface-name"] = iface["name"]

    def dump(self):
        """
        Stringify.
        """

        buf = io.StringIO()
        self.config.write(buf, space_around_delimiters=False)
        header = "# Generated by cloud-init. Changes will be lost.\n\n"
        return header + buf.getvalue()


class Renderer(renderer.Renderer):
    """Renders network information in a NetworkManager keyfile format.

    See https://networkmanager.dev/docs/api/latest/nm-settings-keyfile.html
    """

    def __init__(self, config=None):
        self.connections = {}
        self.config = config

    def get_conn(self, con_id):
        return self.connections[con_id]

    def con_ref(self, con_id):
        if con_id in self.connections:
            return self.connections[con_id].con_uuid()
        else:
            # Well, what can we do...
            return con_id

    def render_network_state(
        self,
        network_state: NetworkState,
        templates: Optional[dict] = None,
        target=None,
    ) -> None:
        # First pass makes sure there's NMConnections for all known
        # interfaces that have UUIDs that can be linked to from related
        # interfaces
        for iface in network_state.iter_interfaces():
            conn_key = iface.get("config_id") or iface["name"]
            self.connections[conn_key] = NMConnection(iface["name"])

        # Now render the actual interface configuration
        for iface in network_state.iter_interfaces():
            conn_key = iface.get("config_id") or iface["name"]
            conn = self.connections[conn_key]
            conn.render_interface(iface, network_state, self)

        # And finally write the files
        for con_id, conn in self.connections.items():
            if not conn.valid():
                continue
            name = nm_conn_filename(con_id, target)
            util.write_file(name, conn.dump(), 0o600)

        # Select EUI64 to be used by default by NM for creating the address
        # for use with RFC4862 IPv6 Stateless Address Autoconfiguration.
        util.write_file(
            cloud_init_nm_conf_filename(target), NM_IPV6_ADDR_GEN_CONF, 0o600
        )


def nm_conn_filename(con_id, target=None):
    target_con_dir = subp.target_path(target, NM_RUN_DIR)
    con_file = f"cloud-init-{con_id}.nmconnection"
    return f"{target_con_dir}/system-connections/{con_file}"


def sysconfig_conn_filename(devname, target=None):
    target_con_dir = subp.target_path(target, IFCFG_CFG_FILE)
    con_file = f"ifcfg-{devname}"
    return f"{target_con_dir}/{con_file}"


def conn_filename(devname):
    """
    This function returns the name of the interface config file.
    It first checks for presence of network manager connection file.
    If absent and ifcfg-rh plugin for network manager is available,
    it returns the name of the ifcfg file if it is present. If the
    plugin is not present or the plugin is present but ifcfg file is
    not, it returns None.
    This function is called from NetworkManagerActivator class in
    activators.py.
    """
    conn_file = nm_conn_filename(devname)
    # If the network manager connection file is absent, also check for
    # presence of ifcfg files for the same interface (if nm-ifcfg-rh plugin is
    # present, network manager can handle ifcfg files). If both network manager
    # connection file and ifcfg files are absent, return None.
    if not os.path.isfile(conn_file) and available_nm_ifcfg_rh():
        conn_file = sysconfig_conn_filename(devname)
    return conn_file if os.path.isfile(conn_file) else None


def cloud_init_nm_conf_filename(target=None):
    target_con_dir = subp.target_path(target, NM_RUN_DIR)
    conf_file = "30-cloud-init-ip6-addr-gen-mode.conf"
    return f"{target_con_dir}/conf.d/{conf_file}"


def available(target=None):
    # TODO: Move `uses_systemd` to a more appropriate location
    # It is imported here to avoid circular import
    from cloudinit.distros import uses_systemd

    nmcli_present = subp.which("nmcli", target=target)
    service_active = True
    if uses_systemd():
        try:
            subp.subp(["systemctl", "is-enabled", "NetworkManager.service"])
        except subp.ProcessExecutionError:
            service_active = False

    return bool(nmcli_present) and service_active
