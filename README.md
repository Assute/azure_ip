# Azure 公网 IP 自动监控与更换

一个轻量、零第三方依赖的 Azure 虚拟机公网 IP 监控工具。

程序会定时 Ping 指定虚拟机的公网 IP；当连续检测失败达到设定阈值后，自动创建新的 Azure 公网 IP、更新网卡绑定，并在确认切换成功后删除旧公网 IP 资源。

支持 **Azure 全球区** 与 **Azure 中国区**，首次运行时可自动识别云环境，并列出当前应用凭据有权管理的虚拟机供用户选择。

## 功能特点

- 定时检测 Azure VM 公网 IP 的 ICMP 可达性
- 连续失败达到阈值后自动更换公网 IP
- 自动发现订阅、资源组、虚拟机、主网卡和公网 IP
- 自动识别 Azure 全球区和 Azure 中国区
- 创建新 IP 时尽量保留原资源的 SKU、区域、可用区、标签和超时设置
- 新 IP 绑定验证成功后再删除旧 IP，降低误删除风险
- 支持保留旧公网 IP 资源
- 支持单次检测、立即更换和演练模式
- 仅使用 Python 标准库，无需安装 Azure CLI 或额外依赖

## 工作流程

```text
检测当前公网 IP
       │
       ├── Ping 正常 ──> 清零失败次数并继续监控
       │
       └── Ping 超时 ──> 累计失败次数
                              │
                              └── 达到阈值
                                     │
                                     ├── 创建新公网 IP
                                     ├── 更新 VM 主网卡绑定
                                     ├── 验证新 IP 已绑定
                                     └── 删除或保留旧公网 IP
```

## 环境要求

- Linux 系统
- Python 3.10 或更高版本
- 系统已安装 `ping` 命令
- 可访问 Azure 登录端点和 Azure Resource Manager API
- Azure VM 已绑定独立的公网 IPv4 地址

> 当前 Ping 参数按 Linux `iputils` 编写，不适用于 Windows 原生命令行。

## Azure 权限准备

需要先在 Microsoft Entra ID 中创建应用注册和客户端密钥，并为对应的服务主体授予目标资源的访问权限。

最简单的配置方式，是在目标资源组范围内授予服务主体 `Contributor`（参与者）角色。若使用自定义最小权限角色，至少需要具备：

- 读取订阅、虚拟机、网卡和公网 IP 信息
- 创建、读取和删除公网 IP 资源
- 读取和更新网络接口

首次配置需要准备以下三个值：

- `APP ID`：应用程序（客户端）ID
- `Client Secret`：客户端密钥值
- `Tenant ID`：目录（租户）ID

## 快速开始

```bash
git clone <your-repository-url>
cd azure-ip-monitor
chmod +x azure_ip.sh
./azure_ip.sh --configure
```

首次运行时，程序会：

1. 提示输入 Azure 应用凭据。
2. 自动尝试连接 Azure 全球区和 Azure 中国区。
3. 查询有权访问的虚拟机及其公网 IP。
4. 提示选择需要监控的虚拟机。
5. 在程序目录生成 `azure_ip_monitor.json` 配置文件。
6. 开始持续监控。

之后直接运行即可：

```bash
./azure_ip.sh
```

也可以直接运行 Python 文件：

```bash
python3 azure_ip_monitor.py
```

## 命令参数

```text
--config PATH    指定配置文件路径
--configure      重新进行交互式配置
--once           只检测一次，不自动更换 IP
--rotate-now     立即更换当前公网 IP
--dry-run        达到失败阈值时只输出提示，不修改 Azure 资源
```

使用示例：

```bash
# 单次连通性检测
./azure_ip.sh --once

# 测试监控逻辑，但不修改 Azure 资源
./azure_ip.sh --dry-run

# 立即创建并切换到一个新公网 IP
./azure_ip.sh --rotate-now

# 使用其他位置的配置文件
./azure_ip.sh --config /etc/azure-ip-monitor/config.json
```

## 配置说明

默认配置文件为 `azure_ip_monitor.json`，首次配置时自动生成。主要监控参数如下：

| 字段 | 默认值 | 说明 |
| --- | ---: | --- |
| `ping_timeout` | `3` | 单次 Ping 超时时间，单位为秒 |
| `check_interval` | `10` | 两次检测之间的间隔，单位为秒 |
| `failure_threshold` | `3` | 连续失败多少次后更换 IP |
| `rotation_cooldown` | `60` | 更换完成后的等待时间，单位为秒 |
| `delete_old_ip` | `true` | 切换成功后是否删除旧公网 IP 资源 |

如需保留旧公网 IP，可将现有配置中的字段修改为：

```json
"delete_old_ip": false
```

请勿删除配置中的凭据和资源定位字段。

## 后台运行

临时后台运行可使用：

```bash
nohup ./azure_ip.sh > azure_ip_monitor.log 2>&1 &
```

长期运行建议配置为 `systemd` 服务，并为配置文件设置严格的访问权限。

## 安全提示

- `azure_ip_monitor.json` 包含明文 `Client Secret`，**请勿提交到 GitHub**。
- 项目提供的 `.gitignore` 已默认排除此配置文件和常见日志文件。
- 建议仅在目标资源组范围内授权，并遵循最小权限原则。
- 如果配置文件曾被上传、分享或泄露，请立即在 Azure 中吊销并重新创建客户端密钥。
- 建议定期轮换客户端密钥，并检查 Azure 活动日志中的公网 IP 和网卡变更记录。

## 注意事项

- 请先确认虚拟机和网络安全组允许 ICMP。若服务器主动禁用 Ping，程序会将其判断为故障并更换 IP。
- 当前程序监控主网卡的主 IP 配置，暂不支持手动选择多个网卡或多个 IP 配置。
- 更换的是 Azure 公网 IP 资源，不会修改虚拟机内部的私网 IP。
- 如果域名直接解析到旧 IP，需要自行同步更新 DNS 记录。
- 创建公网 IP 可能产生 Azure 费用，具体以账号所在区域和订阅计费规则为准。
- 更换过程中会创建带时间戳后缀的新公网 IP 资源名称。

## 文件结构

```text
.
├── azure_ip_monitor.py  # 监控、Azure API 调用和 IP 更换逻辑
├── azure_ip.sh          # Bash 启动入口
├── .gitignore           # 排除凭据、日志和本地缓存
└── README.md            # 项目说明
```

## 免责声明

本项目用于自动化运维和故障恢复。请在充分了解 Azure 网络资源、权限和计费规则后使用。生产环境部署前，建议先通过 `--once` 和 `--dry-run` 验证配置与检测逻辑。