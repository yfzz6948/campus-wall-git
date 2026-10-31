# 校园信息墙 (Campus Wall)

基于 Flask + MariaDB + Bootstrap 5 构建的极简校园信息墙。专为校园内部交流设计，支持多台 Web 服务器共用同一个数据库，配合 DNS 轮询或 Nginx 负载均衡即可轻松实现高可用双机/多机部署。

前端采用朋友圈风格的灰底白卡 UI，移动端优先（最大宽度 600px），界面干净清爽。

## ✨ 核心特性

- **强制身份校验**：学号、姓名、年级、班级四要素强校验登录，已注册用户必须信息完全一致才可放行。
- **防外校注册机制**：新用户注册需答对 3 道随机提取的校内题库题，全对才可建号，防批量注册。
- **高阶 Markdown 渲染**：支持 Markdown 发帖。内置基于 `bleach` 的极严白名单过滤，彻底剥离外链图片、脚本及任何跨站请求伪造可能，保障零 XSS 风险。
- **图片安全存储**：单图支持 ≤5MB（自动魔数验真防伪造后缀），直接转换为 `MEDIUMBLOB` 存入数据库。Web 服务器实现“零状态”，无需配置文件同步（如 NFS）。
- **零日志与强匿名机制**：
  - 匿名发帖仅展示 `年级班级 ***` 标识，前端显示为“匿”。
  - 数据库绝不采集记录 IP、User-Agent 等设备特征。
  - Gunicorn 与 Nginx 均默认关闭访问日志（Access Log）。
- **自动化清理与管理**：
  - 支持发帖、回复、管理员置顶（最多 3 条）。
  - 自动清理策略：距今 90 天前的帖子将随页面访问自动彻底抹除（幂等删除，每日至多一次，极低开销）。
  - 全网页化的超管面板，方便进行权限下放、删帖及动态题库管理。

---

## 🏗️ 架构设计 (v5)

```text
                         ┌─────────────────────┐
        DNS 轮询 /        │   Nginx 负载均衡     │  （可选，推荐；自带健康检查）
   域名解析到多台机器     └──────────┬──────────┘
                                   │
                    ┌──────────────┴──────────────┐
                    ▼                             ▼
          ┌──────────────────┐          ┌──────────────────┐
          │  Web 服务器 A     │          │  Web 服务器 B     │
          │  Flask/gunicorn  │          │  Flask/gunicorn  │
          │  （无状态）       │          │  （无状态）       │
          └────────┬─────────┘          └────────┬─────────┘
                   └──────────────┬──────────────┘
                                  ▼
                    ┌──────────────────────────┐
                    │  数据库服务器（单点集中）  │
                    │  MariaDB：数据 + 图片     │
                    └──────────────────────────┘
```

- **单一事实来源**：任意数量的 Web 节点连接同一个 MariaDB 实例，避免分布式文件同步（如 Rsync/NFS）的复杂性。节点 A 上传的图片节点 B 瞬间可见。   
- **极致备份**：由于图片亦入库，整站备份仅需一条 `mysqldump` 命令即可打包所有文本与媒体资产。   

## 🚀 部署指南

### 第 1 步：配置 MariaDB 数据库

数据库可以与其中一台 Web 服务部署在同一台机器上，也可以完全独立。

Bash

```
# RHEL/Rocky/CentOS 系安装命令
sudo dnf install -y mariadb-server
sudo systemctl enable --now mariadb
sudo mariadb-secure-installation     # 初始化安全设置
```

登录数据库进行建库（**必须使用 utf8mb4，以防 Emoji 或生僻字乱码**）：   

SQL

```
CREATE DATABASE campus_wall CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- 创建数据库账号（请将 'YOUR_PASSWORD' 替换为高强度密码）
-- 如果 Web 服务与数据库在同一台机器：
CREATE USER 'campus_wall'@'127.0.0.1' IDENTIFIED BY 'YOUR_PASSWORD';
GRANT ALL PRIVILEGES ON campus_wall.* TO 'campus_wall'@'127.0.0.1';

-- 如果 Web 服务在其它机器（如内网 10.0.0.x 网段），需额外授权：
CREATE USER 'campus_wall'@'10.0.0.%' IDENTIFIED BY 'YOUR_PASSWORD';
GRANT ALL PRIVILEGES ON campus_wall.* TO 'campus_wall'@'10.0.0.%';

FLUSH PRIVILEGES;
```

> **注意**：如果通过内网连接，请确保系统防火墙/安全组仅对受信任的 Web 服务器 IP 开放 3306 端口。

### 第 2 步：部署 Web 服务环境

在每一台计划运行 Web 服务的机器上执行相同操作。

Bash

```
sudo dnf install -y python3-pip
sudo mkdir -p /opt/campus-wall
# 上传/克隆项目文件至 /opt/campus-wall/
cd /opt/campus-wall

# 构建 Python 虚拟环境并安装依赖
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install gunicorn
```

### 第 3 步：配置环境变量

复制环境变量模板并根据实际情况修改：

Bash

```
cp campus_wall.env.example campus_wall.env
chmod 600 campus_wall.env
vi campus_wall.env
```

配置说明：

- `DB_HOST`：数据库 IP（同机填 `127.0.0.1`，异机填内网 IP）[cite: 21]。
- **`SECRET_KEY`**：Flask Session 签名密钥[cite: 21]。**若多机部署，各机的 `SECRET_KEY` 与 `DB_PASSWORD` 必须完全一致**，否则会导致用户 Session 掉线[cite: 21]。

### 第 4 步：初始化数据库（仅需执行一次）

在任意一台已配置好的 Web 服务器上执行初始化脚本[cite: 21]。该操作为幂等操作，仅首次建表及播种初始题库[cite: 21]。

Bash

```
cd /opt/campus-wall
set -a && source campus_wall.env && set +a
.venv/bin/python app.py initdb
```

> ⚠️ **重要**：默认内置的示例考题非常简单[cite: 21]。**系统上线前，请超级管理员务必在后台（`/admin/questions`）将题目替换为只有本校学生才知晓答案的真实校内题目**[cite: 21]。

### 第 5 步：配置 Systemd 守护进程与 Nginx

**1. Systemd 进程守护**

创建 `/etc/systemd/system/campus-wall.service`：

Ini, TOML

```
[Unit]
Description=Campus Wall (Flask)
After=network.target

[Service]
WorkingDirectory=/opt/campus-wall
EnvironmentFile=/opt/campus-wall/campus_wall.env
# -w 2 表示启动 2 个 Worker 进程，可依据 CPU 核心数适度调整
ExecStart=/opt/campus-wall/.venv/bin/gunicorn -w 2 -b 127.0.0.1:5000 --access-logfile /dev/null app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

启动服务：

Bash

```
sudo systemctl daemon-reload
sudo systemctl enable --now campus-wall
```

**2. Nginx 反向代理示例**

配置 Nginx 将外部 HTTP/HTTPS 流量转发至内网 5000 端口：

Nginx

```
server {
    listen 80;
    server_name wall.example.com;
    
    # 强制重定向至 HTTPS
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name wall.example.com;
    
    ssl_certificate     /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;
    
    # 零日志原则：关闭 Access Log
    access_log off;
    
    # 上传体积限制保障
    client_max_body_size 6M;

    location / {
        proxy_pass [http://127.0.0.1:5000](http://127.0.0.1:5000);
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### （可选）第 6 步：多机负载均衡（Nginx）

若采用 Nginx 作为前置负载均衡网关管理多台 Web 服务器[cite: 21]：

Nginx

```
upstream campus_backend {
    server 10.0.0.11:5000 max_fails=2 fail_timeout=10s;
    server 10.0.0.12:5000 max_fails=2 fail_timeout=10s;
}

server {
    listen 443 ssl;
    server_name wall.example.com;
    # ... SSL 配置略 ...
    
    location / {
        proxy_pass http://campus_backend;
        proxy_set_header Host $host;
    }
}
```

## 👨‍💻 权限系统说明

本系统通过硬编码一个“根管理员（超级管理员）”来实现初始授权。

1. **设置超管学号**：在 `app.py` 中，修改 `ADMIN_STUDENT_ID` 变量（默认为 `25710202061`）[cite: 21]。
2. **提权**：当该学号首次通过答题验证并注册成功时，系统将自动赋予其 `is_admin=1`[cite: 21]。
3. **管理面板**：超管登录后，点击顶部“管理员”即可进入管理面板[cite: 21]。输入任意已注册用户的 11 位学号即可提拔对方为管理员（对方重新登录后生效）[cite: 21]。取消管理员权限即刻生效[cite: 21]。
4. **防御限制**：超管账号不可被网页端取消管理权限，亦不可自我取消[cite: 21]。

## 📦 数据备份与恢复

因多媒体资源均已转为 `MEDIUMBLOB` 入库，备份仅需处理数据库[cite: 21]。

**备份：**

Bash

```
mysqldump -u root -p --single-transaction --default-character-set=utf8mb4 campus_wall > wall_backup.sql
```

**恢复：**

Bash

```
mysql -u root -p --default-character-set=utf8mb4 campus_wall < wall_backup.sql
```

