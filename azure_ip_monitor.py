#!/usr/bin/env python3
"""Monitor TCP connectivity and rotate an Azure VM public IP after repeated failures."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "azure_ip_monitor.json"
COMPUTE_API = "2024-11-01"
NETWORK_API = "2024-05-01"
SUBSCRIPTIONS_API = "2022-12-01"
POST_ROTATION_CHECK_DELAY = 2
TCP_CHECK_HOST = "gd-cu-v4.ip.zstaticcdn.com"
TCP_CHECK_PORT = 80

CLOUDS = {
    "global": {
        "login": "https://login.microsoftonline.com",
        "arm": "https://management.azure.com",
        "scope": "https://management.azure.com/.default",
    },
    "china": {
        "login": "https://login.chinacloudapi.cn",
        "arm": "https://management.chinacloudapi.cn",
        "scope": "https://management.chinacloudapi.cn/.default",
    },
}


class MonitorError(RuntimeError):
    pass


@dataclass
class Config:
    cloud: str
    tenant_id: str
    client_id: str
    client_secret: str
    subscription_id: str
    resource_group: str
    vm_name: str
    tcp_timeout: int = 3
    check_interval: int = 10
    failure_threshold: int = 3
    delete_old_ip: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        required = (
            "cloud",
            "tenant_id",
            "client_id",
            "client_secret",
            "subscription_id",
            "resource_group",
            "vm_name",
        )
        missing = [key for key in required if not data.get(key)]
        if missing:
            raise MonitorError(f"配置缺少字段：{', '.join(missing)}")
        if data["cloud"] not in CLOUDS:
            raise MonitorError("cloud 必须是 global 或 china")
        return cls(
            cloud=str(data["cloud"]),
            tenant_id=str(data["tenant_id"]),
            client_id=str(data["client_id"]),
            client_secret=str(data["client_secret"]),
            subscription_id=str(data["subscription_id"]),
            resource_group=str(data["resource_group"]),
            vm_name=str(data["vm_name"]),
            tcp_timeout=positive_int(data.get("tcp_timeout", data.get("ping_timeout", 3)), "tcp_timeout"),
            check_interval=positive_int(data.get("check_interval", 10), "check_interval"),
            failure_threshold=positive_int(data.get("failure_threshold", 3), "failure_threshold"),
            delete_old_ip=bool(data.get("delete_old_ip", True)),
        )


@dataclass(frozen=True)
class AzureResources:
    nic_id: str
    ip_config_name: str
    public_ip_id: str
    public_ip_address: str


@dataclass(frozen=True)
class VmChoice:
    subscription_id: str
    subscription_name: str
    resource_group: str
    vm_name: str
    location: str
    public_ip_address: str


def positive_int(value: Any, name: str = "值") -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise MonitorError(f"{name} 必须是整数") from error
    if number < 1:
        raise MonitorError(f"{name} 必须大于 0")
    return number


def timestamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def prompt_text(label: str, default: str | None = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        prompt = f"{label}{suffix}："
        value = getpass.getpass(prompt) if secret else input(prompt).strip()
        if value:
            return value
        if default is not None:
            return default
        print("此项不能为空。")


def configure(path: Path) -> Config:
    if not sys.stdin.isatty():
        raise MonitorError("缺少配置且当前不是交互终端，请在交互终端中运行配置命令")

    print("首次运行，只需填写 Azure 应用凭据。")
    print("脚本会自动发现订阅、资源组、虚拟机和公网 IP。")
    client_id = prompt_text("APP ID")
    client_secret = prompt_text("Client Secret", secret=True)
    tenant_id = prompt_text("Tenant ID")

    print("正在识别 Azure 环境并查找可管理的虚拟机...", flush=True)
    client, base_config = authenticate_cloud(tenant_id, client_id, client_secret)
    choices = discover_vm_choices(client, base_config)
    choice = choose_vm(choices)
    config = Config(
        cloud=base_config.cloud,
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
        subscription_id=choice.subscription_id,
        resource_group=choice.resource_group,
        vm_name=choice.vm_name,
    )
    save_config(path, config)
    print(
        f"已选择：{choice.vm_name} / {choice.resource_group} / "
        f"{choice.subscription_name} / {choice.public_ip_address}"
    )
    print(
        f"监控参数：每 10 秒检测 TCP {TCP_CHECK_HOST}:{TCP_CHECK_PORT}，单次超时 3 秒，"
        "连续失败 3 次后更换 IP；换 IP 后每 2 秒验证，失败一次立即继续更换。"
    )
    print(f"配置已保存到 {path}（权限 600）。")
    return config


def save_config(path: Path, config: Config) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "cloud": config.cloud,
        "tenant_id": config.tenant_id,
        "client_id": config.client_id,
        "client_secret": config.client_secret,
        "subscription_id": config.subscription_id,
        "resource_group": config.resource_group,
        "vm_name": config.vm_name,
        "tcp_timeout": config.tcp_timeout,
        "check_interval": config.check_interval,
        "failure_threshold": config.failure_threshold,
        "delete_old_ip": config.delete_old_ip,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_config(path: Path) -> Config:
    try:
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise MonitorError(f"无法读取配置 {path}：{error}") from error
    if not isinstance(data, dict):
        raise MonitorError("配置文件格式错误")
    return Config.from_dict(data)


class AzureClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.endpoints = CLOUDS[config.cloud]
        self.access_token = ""
        self.token_expires_at = 0.0

    def authenticate(self) -> None:
        url = f"{self.endpoints['login']}/{urllib.parse.quote(self.config.tenant_id)}/oauth2/v2.0/token"
        body = urllib.parse.urlencode(
            {
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "scope": self.endpoints["scope"],
                "grant_type": "client_credentials",
            }
        ).encode()
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        response = self._open(request)
        token = response.get("access_token")
        if not isinstance(token, str):
            raise MonitorError("Azure 登录响应中没有 access_token")
        self.access_token = token
        self.token_expires_at = time.time() + int(response.get("expires_in", 3600)) - 120

    def request(
        self,
        method: str,
        resource_id: str,
        api_version: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = urllib.parse.quote(resource_id, safe="/:@")
        url = f"{self.endpoints['arm']}{path}?api-version={urllib.parse.quote(api_version)}"
        return self.request_url(method, url, body)

    def request_url(
        self,
        method: str,
        url: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.access_token or time.time() >= self.token_expires_at:
            self.authenticate()
        if not url.startswith(self.endpoints["arm"] + "/"):
            raise MonitorError("Azure API 返回了不受信任的分页地址")
        encoded = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": f"Bearer {self.access_token}"}
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=encoded, headers=headers, method=method)
        return self._open(request)

    def list_resources(self, resource_id: str, api_version: str) -> list[dict[str, Any]]:
        page = self.request("GET", resource_id, api_version)
        items: list[dict[str, Any]] = []
        while True:
            values = page.get("value", [])
            if not isinstance(values, list):
                raise MonitorError("Azure API 列表返回格式异常")
            items.extend(item for item in values if isinstance(item, dict))
            next_link = page.get("nextLink")
            if not isinstance(next_link, str) or not next_link:
                return items
            page = self.request_url("GET", next_link)

    @staticmethod
    def _open(request: urllib.request.Request) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(detail)
                detail = parsed.get("error", {}).get("message", detail)
            except json.JSONDecodeError:
                pass
            raise MonitorError(f"Azure API 返回 HTTP {error.code}：{detail}") from error
        except urllib.error.URLError as error:
            raise MonitorError(f"无法连接 Azure API：{error.reason}") from error
        if not payload:
            return {}
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as error:
            raise MonitorError("Azure API 返回了无效 JSON") from error
        if not isinstance(parsed, dict):
            raise MonitorError("Azure API 返回格式异常")
        return parsed

    def wait_for_resource(self, resource_id: str, api_version: str, timeout: int = 240) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resource = self.request("GET", resource_id, api_version)
            state = resource.get("properties", {}).get("provisioningState")
            if state == "Succeeded" or state is None:
                return resource
            if state in {"Failed", "Canceled"}:
                raise MonitorError(f"资源部署失败，状态：{state}")
            time.sleep(3)
        raise MonitorError(f"等待 Azure 资源部署超时（{timeout} 秒）")


def first_with_primary(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        raise MonitorError("Azure 资源列表为空")
    return next((item for item in items if item.get("properties", {}).get("primary")), items[0])


def discover_resources(client: AzureClient, config: Config) -> AzureResources:
    base = f"/subscriptions/{config.subscription_id}/resourceGroups/{config.resource_group}"
    vm_id = f"{base}/providers/Microsoft.Compute/virtualMachines/{config.vm_name}"
    vm = client.request("GET", vm_id, COMPUTE_API)
    nic_refs = vm.get("properties", {}).get("networkProfile", {}).get("networkInterfaces", [])
    if not isinstance(nic_refs, list) or not nic_refs:
        raise MonitorError("虚拟机没有网卡")
    nic_ref = first_with_primary(nic_refs)
    nic_id = nic_ref.get("id")
    if not isinstance(nic_id, str):
        raise MonitorError("无法获取虚拟机网卡 ID")

    nic = client.request("GET", nic_id, NETWORK_API)
    configurations = nic.get("properties", {}).get("ipConfigurations", [])
    if not isinstance(configurations, list) or not configurations:
        raise MonitorError("网卡没有 IP 配置")
    ip_config = first_with_primary(configurations)
    ip_config_name = ip_config.get("name")
    public_ip_id = ip_config.get("properties", {}).get("publicIPAddress", {}).get("id")
    if not isinstance(ip_config_name, str) or not isinstance(public_ip_id, str):
        raise MonitorError("主网卡 IP 配置没有绑定 Azure 公网 IP")

    public_ip = client.request("GET", public_ip_id, NETWORK_API)
    address = public_ip.get("properties", {}).get("ipAddress")
    if not isinstance(address, str) or not address:
        raise MonitorError("Azure 公网 IP 尚未分配地址")
    return AzureResources(nic_id, ip_config_name, public_ip_id, address)


def authenticate_cloud(tenant_id: str, client_id: str, client_secret: str) -> tuple[AzureClient, Config]:
    errors: list[str] = []
    for cloud in ("global", "china"):
        config = Config(
            cloud=cloud,
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            subscription_id="pending",
            resource_group="pending",
            vm_name="pending",
        )
        client = AzureClient(config)
        try:
            client.authenticate()
        except MonitorError as error:
            errors.append(f"{cloud}: {error}")
            continue
        print(f"已识别 Azure 环境：{cloud}")
        return client, config
    detail = errors[-1] if errors else "未知登录错误"
    raise MonitorError(f"APP ID、Client Secret 或 Tenant ID 无法登录 Azure；{detail}")


def resource_group_from_id(resource_id: str) -> str:
    parts = resource_id.strip("/").split("/")
    lowered = [part.lower() for part in parts]
    try:
        index = lowered.index("resourcegroups")
        return parts[index + 1]
    except (ValueError, IndexError) as error:
        raise MonitorError(f"无法从 VM 资源 ID 解析资源组：{resource_id}") from error


def discover_vm_choices(client: AzureClient, base_config: Config) -> list[VmChoice]:
    subscriptions = client.list_resources("/subscriptions", SUBSCRIPTIONS_API)
    subscriptions = [
        subscription
        for subscription in subscriptions
        if str(subscription.get("state", "Enabled")).lower() == "enabled"
    ]
    if not subscriptions:
        raise MonitorError("该应用没有可访问的 Azure 订阅，请检查角色分配")

    choices: list[VmChoice] = []
    skipped: list[str] = []
    for subscription in subscriptions:
        subscription_id = subscription.get("subscriptionId")
        if not isinstance(subscription_id, str) or not subscription_id:
            continue
        subscription_name = str(subscription.get("displayName") or subscription_id)
        vm_list_path = f"/subscriptions/{subscription_id}/providers/Microsoft.Compute/virtualMachines"
        try:
            virtual_machines = client.list_resources(vm_list_path, COMPUTE_API)
        except MonitorError as error:
            skipped.append(f"订阅 {subscription_name}: {error}")
            continue

        for vm in virtual_machines:
            vm_id = vm.get("id")
            vm_name = vm.get("name")
            if not isinstance(vm_id, str) or not isinstance(vm_name, str):
                continue
            resource_group = resource_group_from_id(vm_id)
            candidate = Config(
                cloud=base_config.cloud,
                tenant_id=base_config.tenant_id,
                client_id=base_config.client_id,
                client_secret=base_config.client_secret,
                subscription_id=subscription_id,
                resource_group=resource_group,
                vm_name=vm_name,
            )
            try:
                resources = discover_resources(client, candidate)
            except MonitorError as error:
                skipped.append(f"VM {vm_name}: {error}")
                continue
            choices.append(
                VmChoice(
                    subscription_id=subscription_id,
                    subscription_name=subscription_name,
                    resource_group=resource_group,
                    vm_name=vm_name,
                    location=str(vm.get("location") or "unknown"),
                    public_ip_address=resources.public_ip_address,
                )
            )

    if not choices:
        detail = f"；最后错误：{skipped[-1]}" if skipped else ""
        raise MonitorError(f"没有发现已绑定公网 IP 且可管理的 Azure VM{detail}")
    return choices


def choose_vm(choices: list[VmChoice]) -> VmChoice:
    if len(choices) == 1:
        print(f"自动发现唯一 VM：{choices[0].vm_name} ({choices[0].public_ip_address})")
        return choices[0]

    print("发现多台可管理的 VM，请选择监控对象：")
    for index, choice in enumerate(choices, start=1):
        print(
            f"  {index}. {choice.vm_name} | {choice.public_ip_address} | "
            f"{choice.resource_group} | {choice.subscription_name} | {choice.location}"
        )
    while True:
        selected = input(f"输入序号 [1-{len(choices)}]：").strip()
        try:
            index = int(selected)
        except ValueError:
            index = 0
        if 1 <= index <= len(choices):
            return choices[index - 1]
        print("序号无效，请重新输入。")


def id_reference(value: Any) -> dict[str, str] | None:
    if isinstance(value, dict) and isinstance(value.get("id"), str):
        return {"id": value["id"]}
    return None


def id_references(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    return [reference for item in value if (reference := id_reference(item)) is not None]


def writable_ip_config(item: dict[str, Any], selected_name: str, new_public_ip_id: str) -> dict[str, Any]:
    source = item.get("properties", {})
    properties: dict[str, Any] = {}
    scalar_keys = (
        "privateIPAddress",
        "privateIPAllocationMethod",
        "privateIPAddressVersion",
        "primary",
    )
    for key in scalar_keys:
        if key in source:
            properties[key] = source[key]

    subnet = id_reference(source.get("subnet"))
    if subnet:
        properties["subnet"] = subnet

    list_keys = (
        "applicationGatewayBackendAddressPools",
        "loadBalancerBackendAddressPools",
        "loadBalancerInboundNatRules",
        "applicationSecurityGroups",
    )
    for key in list_keys:
        references = id_references(source.get(key))
        if references:
            properties[key] = references

    if item.get("name") == selected_name:
        properties["publicIPAddress"] = {"id": new_public_ip_id}
    else:
        public_ip = id_reference(source.get("publicIPAddress"))
        if public_ip:
            properties["publicIPAddress"] = public_ip
    return {"name": item.get("name"), "properties": properties}


def nic_update_body(nic: dict[str, Any], selected_name: str, new_public_ip_id: str) -> dict[str, Any]:
    source = nic.get("properties", {})
    properties: dict[str, Any] = {
        "ipConfigurations": [
            writable_ip_config(item, selected_name, new_public_ip_id)
            for item in source.get("ipConfigurations", [])
        ]
    }
    for key in (
        "enableAcceleratedNetworking",
        "enableIPForwarding",
        "disableTcpStateTracking",
        "auxiliaryMode",
        "auxiliarySku",
    ):
        if key in source:
            properties[key] = source[key]
    dns_servers = source.get("dnsSettings", {}).get("dnsServers")
    if isinstance(dns_servers, list):
        properties["dnsSettings"] = {"dnsServers": dns_servers}

    body: dict[str, Any] = {"location": nic.get("location"), "properties": properties}
    if isinstance(nic.get("tags"), dict):
        body["tags"] = nic["tags"]
    return body


def new_public_ip_body(old: dict[str, Any]) -> dict[str, Any]:
    old_properties = old.get("properties", {})
    properties: dict[str, Any] = {
        "publicIPAllocationMethod": "Static",
        "publicIPAddressVersion": old_properties.get("publicIPAddressVersion", "IPv4"),
    }
    if "idleTimeoutInMinutes" in old_properties:
        properties["idleTimeoutInMinutes"] = old_properties["idleTimeoutInMinutes"]
    ddos = old_properties.get("ddosSettings")
    if isinstance(ddos, dict):
        writable_ddos = {key: ddos[key] for key in ("protectionMode",) if key in ddos}
        plan = id_reference(ddos.get("ddosProtectionPlan"))
        if plan:
            writable_ddos["ddosProtectionPlan"] = plan
        if writable_ddos:
            properties["ddosSettings"] = writable_ddos

    body: dict[str, Any] = {
        "location": old.get("location"),
        "properties": properties,
        "sku": {
            key: old.get("sku", {})[key]
            for key in ("name", "tier")
            if key in old.get("sku", {})
        },
    }
    if isinstance(old.get("zones"), list) and old["zones"]:
        body["zones"] = old["zones"]
    if isinstance(old.get("tags"), dict):
        body["tags"] = old["tags"]
    if isinstance(old.get("extendedLocation"), dict):
        body["extendedLocation"] = old["extendedLocation"]
    return body


def replacement_id(old_id: str) -> str:
    old_name = old_id.rstrip("/").rsplit("/", 1)[-1]
    suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    new_name = f"{old_name[:60]}-r{suffix}"
    return f"{old_id.rsplit('/', 1)[0]}/{new_name}"


def rotate_public_ip(client: AzureClient, resources: AzureResources, delete_old: bool) -> AzureResources:
    old_public_ip = client.request("GET", resources.public_ip_id, NETWORK_API)
    nic = client.request("GET", resources.nic_id, NETWORK_API)
    new_id = replacement_id(resources.public_ip_id)
    new_name = new_id.rsplit("/", 1)[-1]
    print(f"[{timestamp()}] 正在创建新公网 IP 资源：{new_name}", flush=True)
    client.request("PUT", new_id, NETWORK_API, new_public_ip_body(old_public_ip))
    new_public_ip = client.wait_for_resource(new_id, NETWORK_API)
    new_address = new_public_ip.get("properties", {}).get("ipAddress")
    if not isinstance(new_address, str) or not new_address:
        raise MonitorError("新公网 IP 创建成功，但没有获得 IP 地址")

    print(f"[{timestamp()}] 新地址为 {new_address}，正在更新网卡绑定", flush=True)
    body = nic_update_body(nic, resources.ip_config_name, new_id)
    client.request("PUT", resources.nic_id, NETWORK_API, body)
    updated_nic = client.wait_for_resource(resources.nic_id, NETWORK_API)
    configurations = updated_nic.get("properties", {}).get("ipConfigurations", [])
    selected = next((item for item in configurations if item.get("name") == resources.ip_config_name), None)
    attached_id = (selected or {}).get("properties", {}).get("publicIPAddress", {}).get("id", "")
    if attached_id.lower() != new_id.lower():
        raise MonitorError("网卡更新完成，但未验证到新公网 IP 绑定；旧 IP 未删除")

    if delete_old:
        print(f"[{timestamp()}] 新 IP 已绑定，正在删除旧公网 IP 资源", flush=True)
        client.request("DELETE", resources.public_ip_id, NETWORK_API)
    else:
        print(f"[{timestamp()}] 已按配置保留旧公网 IP 资源：{resources.public_ip_id}", flush=True)

    print(f"[{timestamp()}] 公网 IP 更换完成：{resources.public_ip_address} -> {new_address}", flush=True)
    return AzureResources(resources.nic_id, resources.ip_config_name, new_id, new_address)


def tcp_check_once(timeout: int) -> bool:
    command = [
        "timeout",
        str(timeout),
        "bash",
        "-c",
        f"</dev/tcp/{TCP_CHECK_HOST}/{TCP_CHECK_PORT}",
    ]
    result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0


def rotate_until_reachable(
    client: AzureClient,
    config: Config,
    resources: AzureResources,
) -> AzureResources:
    while True:
        resources = rotate_public_ip(client, resources, config.delete_old_ip)
        print(
            f"[{timestamp()}] 等待 {POST_ROTATION_CHECK_DELAY} 秒后通过新公网 IP 检测 "
            f"TCP {TCP_CHECK_HOST}:{TCP_CHECK_PORT}；如超时将立即继续更换",
            flush=True,
        )
        time.sleep(POST_ROTATION_CHECK_DELAY)
        if tcp_check_once(config.tcp_timeout):
            print(
                f"[{timestamp()}] 新公网 IP {resources.public_ip_address} 访问 "
                f"TCP {TCP_CHECK_HOST}:{TCP_CHECK_PORT} 正常，恢复常规监控",
                flush=True,
            )
            return resources
        print(
            f"[{timestamp()}] 新公网 IP {resources.public_ip_address} 访问 "
            f"TCP {TCP_CHECK_HOST}:{TCP_CHECK_PORT} 超时，立即继续更换",
            flush=True,
        )
        resources = discover_resources(client, config)


def monitor(client: AzureClient, config: Config, once: bool = False) -> int:
    resources = discover_resources(client, config)
    print(
        f"[{timestamp()}] 开始监控 {config.vm_name}，当前公网 IP：{resources.public_ip_address}；"
        f"检测目标 TCP {TCP_CHECK_HOST}:{TCP_CHECK_PORT}，"
        f"连续失败 {config.failure_threshold} 次后更换",
        flush=True,
    )
    failures = 0
    while True:
        reachable = tcp_check_once(config.tcp_timeout)
        if reachable:
            failures = 0
            print(
                f"[{timestamp()}] TCP {TCP_CHECK_HOST}:{TCP_CHECK_PORT} 正常 "
                f"（当前公网 IP：{resources.public_ip_address}）",
                flush=True,
            )
        else:
            failures += 1
            print(
                f"[{timestamp()}] TCP {TCP_CHECK_HOST}:{TCP_CHECK_PORT} 超时 "
                f"({failures}/{config.failure_threshold})",
                flush=True,
            )

        if once:
            return 0 if reachable else 1

        if failures >= config.failure_threshold:
            resources = discover_resources(client, config)
            resources = rotate_until_reachable(client, config, resources)
            failures = 0

        time.sleep(config.check_interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Azure TCP 连通性监控与公网 IP 自动更换")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="配置文件路径")
    parser.add_argument("--configure", action="store_true", help="重新填写并保存配置")
    parser.add_argument("--configure-only", action="store_true", help="完成配置后退出，不启动监控")
    parser.add_argument("--once", action="store_true", help="只检测一次，不更换 IP")
    parser.add_argument("--rotate-now", action="store_true", help="立即更换公网 IP")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    missing_commands = [name for name in ("bash", "timeout") if shutil.which(name) is None]
    if missing_commands:
        print(f"错误：系统缺少命令：{', '.join(missing_commands)}", file=sys.stderr)
        return 2

    try:
        should_configure = args.configure or args.configure_only or not args.config.exists()
        config = configure(args.config) if should_configure else load_config(args.config)
        if args.configure_only:
            return 0
        client = AzureClient(config)
        if args.rotate_now:
            resources = discover_resources(client, config)
            print(f"当前公网 IP：{resources.public_ip_address}")
            rotate_until_reachable(client, config, resources)
            return 0
        return monitor(client, config, once=args.once)
    except KeyboardInterrupt:
        print("\n监控已停止。")
        return 130
    except MonitorError as error:
        print(f"错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
