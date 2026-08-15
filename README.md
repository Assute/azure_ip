# Azure 公网 IP 自动监控与更换

一个轻量、零第三方 Python 依赖的 Azure 虚拟机公网 IP 监控工具。

程序会通过当前 Azure 公网 IP 定时检测 `gd-cu-v4.ip.zstaticcdn.com:80` 的 TCP 连通性；当连续检测失败达到设定阈值后，自动创建新的 Azure 公网 IP、更新网卡绑定，新 IP 绑定验证成功后立即删除前一个公网 IP，并继续检测新 IP 的 TCP 连通性。

项目提供一键安装脚本，可自动将程序部署到 `/opt/azure_ip`，创建并启动 `systemd` 服务，使监控程序在后台持续运行并随系统启动。还可选配 Cloudflare DDNS，在公网 IP 变化并确认可用后自动同步 A 记录。

## 一键安装

```bash
curl -fsSL https://raw.githubusercontent.com/Assute/azure_ip/main/azure_ip.sh | sudo bash
```

安装完成后，监控程序会在后台持续运行，并随系统自动启动。

## 功能特点

- 一条命令完成下载、配置、安装和后台启动
- 自动部署到 `/opt/azure_ip`
- 自动创建并启用 `systemd` 后台服务
- 可选配置 Cloudflare DDNS，自动创建或更新 A 记录
- 使用 Bash `/dev/tcp` 检测 `gd-cu-v4.ip.zstaticcdn.com:80` 的 TCP 连通性
- 常规检测失败后立即复检，默认连续失败 3 次后更换公网 IP
- 新 IP 绑定后等待 5 秒检测 TCP，失败一次立即继续更换，直到恢复正常
- 自动发现订阅、资源组、虚拟机、主网卡和公网 IP
- 自动识别 Azure 全球区和 Azure 中国区
- 创建新 IP 时尽量保留原资源的 SKU、区域、可用区、标签和超时设置
- 更新网卡公网 IP 时保留原有 NSG、DNS 和网卡功能设置，避免入站规则丢失
- 新 IP 绑定验证成功后立即删除前一个公网 IP，始终只保留当前 IP
- 支持单次检测和立即更换模式
- 仅使用 Python 标准库，无需安装 Azure CLI 或第三方 Python 包

## 工作流程

```text
systemd 后台运行
       │
       └── TCP 检测 gd-cu-v4.ip.zstaticcdn.com:80
                    │
                    ├── 连接正常 ──> 清零失败次数，等待常规间隔
                    │
                    └── 连接超时 ──> 不等待，立即再次检测
                                           │
                                           └── 连续 3 次失败
                                                  ├── 创建并绑定新公网 IP
                                                  ├── 立即删除前一个公网 IP
                                                  ├── 等待 5 秒再次检测 TCP
                                                  ├── 检测失败：立即继续更换
                                                  └── 检测正常：同步 Cloudflare A 记录并恢复监控
```

## 环境要求

- 使用 `systemd` 的 Linux 系统
- Python 3.10 或更高版本
- 系统已安装 `bash` 和 GNU `timeout` 命令
- 系统已安装 `curl` 或 `wget`
- 可访问 GitHub、Azure 登录端点和 Azure Resource Manager API
- 如启用 DDNS，需要能访问 Cloudflare API
- Azure VM 已绑定独立的公网 IPv4 地址

> 实际检测命令为 `timeout 3 bash -c '</dev/tcp/gd-cu-v4.ip.zstaticcdn.com/80'`，因此需要 Bash 支持 `/dev/tcp`。

## Azure 权限准备

需要先在 Microsoft Entra ID 中创建应用注册和客户端密钥，并为对应的服务主体授予目标资源的访问权限。

最简单的配置方式，是在目标资源组范围内授予服务主体 `Contributor`（参与者）角色。若使用自定义最小权限角色，至少需要具备：

- 读取订阅、虚拟机、网卡和公网 IP 信息
- 创建、读取和删除公网 IP 资源
- 读取和更新网络接口

首次配置需要准备：

- `APP ID`：应用程序（客户端）ID
- `Client Secret`：客户端密钥值
- `Tenant ID`：目录（租户）ID

## 详细安装说明

在 Linux 服务器中执行：

```bash
curl -fsSL https://raw.githubusercontent.com/Assute/azure_ip/main/azure_ip.sh | sudo bash
```

如果服务器没有 `curl`，可以使用：

```bash
wget -qO azure_ip.sh https://raw.githubusercontent.com/Assute/azure_ip/main/azure_ip.sh
sudo bash azure_ip.sh
```

首次运行时，安装脚本会：

1. 检查 `python3`、`bash`、`timeout`、`systemctl`、`curl` 或 `wget`。
2. 下载最新版程序到 `/opt/azure_ip`。
3. 提示输入 Azure 应用凭据。
4. 自动识别 Azure 全球区或中国区。
5. 查询并选择需要监控的虚拟机。
6. 将配置保存为 `/opt/azure_ip/azure_ip_monitor.json`。
7. 创建 `/etc/systemd/system/azure-ip-monitor.service`。
8. 启用并启动后台监控服务。

安装完成后，即使退出 SSH，程序也会继续在后台运行；服务器重启后服务会自动启动。

再次运行安装脚本并检测到已有配置时，会显示以下菜单：

```text
1. 更新脚本并重启服务（默认）
2. 重新配置
3. 删除配置并停止后台服务
4. Cloudflare DDNS配置
```

选择 `1` 会保留现有配置、下载最新版脚本并重启服务；选择 `2` 会重新填写 Azure 凭据和目标虚拟机；选择 `3` 会先停止并禁用 `azure-ip-monitor.service`，然后安全删除配置文件；选择 `4` 会配置 Cloudflare DDNS，并立即把当前 Azure 公网 IP 同步到指定 A 记录。


## Cloudflare DDNS

先确保 Azure 监控已经安装并生成配置文件，然后再次运行安装脚本，选择 `4. Cloudflare DDNS配置`。脚本会依次要求输入：

```text
CFKEY=
CFUSER=
CFZONE_NAME=
CFRECORD_NAME=
```

- `CFKEY`：Cloudflare 的 Global API Key，输入时不会回显。
- `CFUSER`：Cloudflare 账号邮箱。
- `CFZONE_NAME`：根域名，例如 `example.com`。
- `CFRECORD_NAME`：可填写记录前缀 `www`、根记录 `@`，也可直接填写完整域名 `www.example.com`。

配置时会先验证 Cloudflare 凭据和 Zone，并把当前 Azure 公网 IP 写入 A 记录；记录不存在时自动创建，存在时只更新地址。后续 Azure 公网 IP 更换并通过 TCP 检测后，会自动同步 Cloudflare。同步失败不会触发再次换 IP，后台服务会在后续正常检测时继续重试。

## 管理命令

```bash
# 查看后台服务状态
/opt/azure_ip/azure_ip.sh --status

# 持续查看运行日志，按 Ctrl+C 退出
/opt/azure_ip/azure_ip.sh --logs

# 下载最新版程序并重启后台服务
sudo /opt/azure_ip/azure_ip.sh

# 重新填写 Azure 凭据和选择虚拟机
sudo /opt/azure_ip/azure_ip.sh --reconfigure

# 不显示菜单，直接删除配置并停止后台服务
sudo /opt/azure_ip/azure_ip.sh --delete-config

# 不显示菜单，直接配置 Cloudflare DDNS
sudo /opt/azure_ip/azure_ip.sh --cf-ddns
```

也可以直接使用 `systemctl`：

```bash
sudo systemctl status azure-ip-monitor
sudo systemctl restart azure-ip-monitor
sudo systemctl stop azure-ip-monitor
sudo systemctl start azure-ip-monitor
journalctl -u azure-ip-monitor -f
```

## Python 命令参数

如需临时调试，可以直接运行安装目录中的 Python 程序：

```text
--config PATH       指定配置文件路径
--configure         重新填写配置并继续运行
--configure-only    完成配置后退出，不启动监控
--configure-cloudflare 配置 Cloudflare DDNS 后退出
--once              只检测一次，不自动更换 IP
--rotate-now        立即更换当前公网 IP
```

使用示例：

```bash
# 单次连通性检测
sudo python3 /opt/azure_ip/azure_ip_monitor.py \
  --config /opt/azure_ip/azure_ip_monitor.json --once


# 立即创建并切换到新公网 IP
sudo python3 /opt/azure_ip/azure_ip_monitor.py \
  --config /opt/azure_ip/azure_ip_monitor.json --rotate-now
```

手动调试前建议先停止后台服务，避免两个进程同时操作 Azure 网络资源：

```bash
sudo systemctl stop azure-ip-monitor
```

## 配置说明

默认配置文件位于 `/opt/azure_ip/azure_ip_monitor.json`。重新运行安装脚本时会保留现有配置，只有使用 `--reconfigure` 才会重新填写。

| 字段 | 默认值 | 说明 |
| --- | ---: | --- |
| `tcp_timeout` | `3` | 单次 TCP 连接超时时间，单位为秒 |
| `check_interval` | `10` | 检测正常后到下一次常规检测的间隔，单位为秒；失败复检不等待 |
| `failure_threshold` | `3` | 连续失败多少次后更换 IP |
| `CFKEY` | 无 | Cloudflare Global API Key |
| `CFUSER` | 无 | Cloudflare 账号邮箱 |
| `CFZONE_NAME` | 无 | 托管在 Cloudflare 的根域名，例如 `example.com` |
| `CFRECORD_NAME` | 无 | A 记录名称，可填 `www`、`@` 或完整域名 |


常规检测一旦失败会立即进行下一次检测，不等待 `check_interval`；默认连续失败 `3` 次后开始更换公网 IP。每次创建的新公网 IP 绑定并验证成功后，脚本会立即删除前一个公网 IP，因此正常情况下始终只保留当前绑定的一个公网 IP。随后等待 `5` 秒进行 TCP 检测；检测失败就直接继续更换，检测正常则恢复常规监控。旧配置中的 `ping_timeout` 会自动作为 `tcp_timeout` 读取，旧的 `delete_old_ip` 字段会被忽略，无需重新配置。

修改配置后重启服务：

```bash
sudo systemctl restart azure-ip-monitor
```

## 安全提示

- `/opt/azure_ip/azure_ip_monitor.json` 包含明文 `Client Secret`；启用 DDNS 后还包含明文 `CFKEY`，安装脚本会将文件权限设置为 `600`。
- `azure_ip_monitor.json` 已被 `.gitignore` 排除，请勿手动提交到 GitHub。
- 建议仅在目标资源组范围内授权，并遵循最小权限原则。
- 如果配置文件曾被上传、分享或泄露，请立即在 Azure 中吊销并重新创建客户端密钥。
- 建议定期轮换客户端密钥，并检查 Azure 活动日志中的公网 IP 和网卡变更记录。

## 注意事项

- 请确认服务器能解析并访问 `gd-cu-v4.ip.zstaticcdn.com:80`。目标服务故障、DNS 故障或出站防火墙拦截都会被判定为异常，并可能触发连续更换 IP。
- 当前程序监控主网卡的主 IP 配置，暂不支持手动选择多个网卡或多个 IP 配置。
- 更换的是 Azure 公网 IP 资源，不会修改虚拟机内部的私网 IP。
- 如果域名直接解析到旧 IP，需要自行同步更新 DNS 记录。
- 创建公网 IP 可能产生 Azure 费用，具体以账号所在区域和订阅计费规则为准。
- 更换过程中会创建带时间戳后缀的新公网 IP 资源名称。

## 安装后的文件

```text
/opt/azure_ip/
├── azure_ip.sh
├── azure_ip_monitor.py
└── azure_ip_monitor.json

/etc/systemd/system/
└── azure-ip-monitor.service
```

## 免责声明

本项目用于自动化运维和故障恢复。请在充分了解 Azure 网络资源、权限和计费规则后使用。生产环境部署前，建议先通过 `--once` 验证配置和 TCP 检测逻辑。