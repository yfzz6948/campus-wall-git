# 校园信息墙 v5（Campus Wall）

Flask + **MariaDB** + Bootstrap 5 的极简校园信息墙。支持多台 Web 服务器共用同一个数据库，
配合 DNS 轮询或 nginx 负载均衡做双机/多机部署。

功能：四要素强校验登录、防外校答题注册（答题 5 次失败锁定 1 小时）、Markdown 帖子（bleach 严格清洗）、
匿名发帖（每日 10 条上限，管理员不限）、图片上传（存数据库）、帖子回复、置顶（最多 3 条）、
草稿箱（每人最多 3 条）、90 天自动清理、网页化管理员管理、超管删帖与题库管理、数据库故障 503 友好降级页、
公开可读的隐私政策页（`/privacy`，引用 Cloudflare Turnstile 隐私补充条款）。
移动端优先，最大宽度 600px，朋友圈灰底白卡风格。

登录页在提交前会过一次 **Cloudflare Turnstile（不可见模式）**：正常情况下用户全程看不到任何东西，
只有 CF 判定可疑时才可能弹一次点击。注册跳转页 `/register/jump` 已下线，注册流程是
`POST /login`（新学号）→ 直接进 `/register/quiz` 答题，防外校靠题库、防脚本靠人机验证。

## v5 架构（重要变更）

```
                         ┌─────────────────────┐
        DNS 轮询 /        │   Nginx 负载均衡     │  （可选，推荐；自带健康检查）
   域名解析到两台机器     └──────────┬──────────┘
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
                    │  数据库服务器（只有一台）  │
                    │  MariaDB：数据 + 图片     │
                    └──────────────────────────┘
```

- **数据库只有一台**：两台 Web 都连同一个 MariaDB，数据天然一致，不存在"同步方向"问题。
- **图片也存数据库**（`MEDIUMBLOB`）：Web 服务器本地不保存任何状态，A 机上传的图片 B 机立即可见，
  无需 NFS/文件同步；备份只需一个 `mysqldump`。
- 数据库机本身也可以同时跑一份 Web（它本机用 `127.0.0.1` 连库）。

## 文件结构

```
campus-wall/
├── app.py                      # 路由 + MariaDB + 安全逻辑（python app.py initdb 建表）
├── migrate_from_sqlite.py      # 旧版 SQLite 数据一次性导入 MariaDB（没有旧数据可忽略）
├── requirements.txt            # Flask / Markdown / bleach / PyMySQL
├── campus_wall.env.example     # 环境变量示例（数据库地址、密码、SECRET_KEY）
├── campus_wall_service.txt     # systemd 单元样例（EnvironmentFile 载入环境变量）
├── README.md
├── templates/                  # 11 个 Jinja 模板（登录/注册跳转/答题/首页/草稿箱/站外提示页/题库/题库表单/管理员/降级页/基础布局）
└── static/
    ├── bootstrap.min.css       # 本地化，不依赖 CDN
    ├── bootstrap.bundle.min.js
    └── favicon.png
```

---

## 部署步骤（Rocky / Fedora / CentOS 系）

### 第 1 步：数据库服务器安装 MariaDB（只在一台机器上做）

```bash
sudo dnf install -y mariadb-server
sudo systemctl enable --now mariadb
sudo mariadb-secure-installation     # 按提示设置 root 密码、关闭匿名登录
```

建库（**utf8mb4 是硬性要求**，否则 emoji 和生僻字会乱码）：

```bash
sudo mariadb -u root -p
```

```sql
CREATE DATABASE campus_wall CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- 数据库本机也跑 Web：本机连接账号
CREATE USER 'campus_wall'@'localhost' IDENTIFIED BY '这里换成强密码';
-- 另一台 Web 从内网连接账号（把 10.0.0.% 换成你实际的内网网段）
CREATE USER 'campus_wall'@'10.0.0.%' IDENTIFIED BY '这里换成强密码';

GRANT ALL PRIVILEGES ON campus_wall.* TO 'campus_wall'@'localhost';
GRANT ALL PRIVILEGES ON campus_wall.* TO 'campus_wall'@'10.0.0.%';
FLUSH PRIVILEGES;
```

让 3306 只对另一台 Web 开放（**绝不对公网开放**）：

```bash
# 只允许另一台 Web（假设 10.0.0.12）访问 3306
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="10.0.0.12/32" port port="3306" protocol="tcp" accept'
sudo firewall-cmd --reload
```

> 如果上传图片时数据库报数据包过大，在 `/etc/my.cnf.d/` 下加 `[mysqld] max_allowed_packet=32M` 后重启。
> 图片上限已放到 10MB，默认 16MB 的数据包能装下，但换 10MB 大图后建议显式设成 `32M` 更稳妥。

### 第 2 步：两台 Web 服务器部署代码（两台操作完全相同）

```bash
sudo dnf install -y python3-pip
sudo mkdir -p /home/campus-wall
# 把项目文件放到 /home/campus-wall/
cd /home/campus-wall
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

> `requirements.txt` 里已经包含 gunicorn，不用再单独装一次。

> **上传 / 打包时务必排除 `venv` 和 `Lib`**（尤其是从 Windows 拷 `venv` 上去的话，
> 里面全是 `.exe`，云主机的安全扫描会直接报「恶意文件 Win32.Virus.Darkkomet」）。
> `.venv` 是在服务器上现建的，从来不需要上传：`python3 -m venv .venv` 一行就重建了。
> 详见下面「云主机把 pip 里的 .exe 报成病毒怎么办」。
>
> **同样必须排除 `campus_wall.env`**：里面有 `SECRET_KEY`、`DB_PASSWORD` 和 Turnstile 的 Secret Key。
> 打包/上传代码时只传 `campus_wall.env.example`，真钥匙在服务器上手工填。本机那份请当密码对待。

### 云主机把 pip 里的 .exe 报成病毒怎么办

腾讯云 / 阿里云的主机安全会报这类告警：

```
文件路径：/home/campus-wall/.venv/lib/python3.12/site-packages/pip/_vendor/distlib/t32.exe
病毒名：  Win32.Virus.Darkkomet.Vr      标签：darkkomet、Worm      等级：Fatal
```

**结论：绝大多数情况下是误报，且对本站没有任何影响。**

原因有三条：

1. **这些 `.exe` 是 pip 自带的，任何平台的 pip 都有**。pip 内嵌了 `distlib`，
   而 `distlib` 给 Windows 准备了一批启动器存根（`t32.exe` / `w32.exe` / `w64.exe` ……），
   打包 wheel 时不分平台，所以 Linux 上的 pip 里也躺着这几个 Windows PE 文件。
   它们在本站的使用路径里**一次都不会被调用**：Linux 内核不会执行 PE 文件，
   也没有 wine；它们只在"在 Windows 上创建 venv"时才会用到。
2. **Darkkomet 是 Windows 蠕虫**，靠感染 `.exe` 传播。停在 Linux 磁盘上的 PE 文件是一堆死数据。
3. 这类存根体积小、结构固定，常年被各家引擎按启发式规则误判。

想彻底自证（任选一种）：

```bash
# ① 看它是不是就是 pip 官方那一个：新建一个干净 venv 比对哈希
python3 -m venv /tmp/chk && /tmp/chk/bin/pip install -q --upgrade pip
sha256sum /tmp/chk/lib/python3*/site-packages/pip/_vendor/distlib/{t32,w32,w64}.exe
sha256sum    .venv/lib*/python3.12/site-packages/pip/_vendor/distlib/{t32,w32,w64}.exe
```
两边哈希一致 → 就是 PyPI 上的官方文件，误报无疑。

```bash
# ② 直接删掉：Linux 上本来用不到，pip 功能不受影响
rm -f .venv/lib*/python3.12/site-packages/pip/_vendor/distlib/*.exe
.venv/bin/pip --version     # 仍然正常
```
（以后 `pip install --upgrade pip` 会把它们再装回来，再删一次即可。）

然后去主机安全控制台把告警标成**误报/忽略**，并把 `/home/campus-wall/.venv` 加进白名单，
免得每次 pip 升级都来吓你一次。**不要**把这个 `.venv` 打包拷到 Windows 上再用——
那才是这几个 PE 文件唯一可能真正被执行的地方。


配置环境变量：

```bash
cp campus_wall.env.example campus_wall.env
chmod 600 campus_wall.env
vi campus_wall.env
```

- 数据库本机：`DB_HOST=127.0.0.1`
- 另一台 Web：`DB_HOST=数据库机内网IP`
- **两台的 `SECRET_KEY` 和 `DB_PASSWORD` 必须完全相同**（SECRET_KEY 不一致会导致登录状态随机掉线）。
- 可选：`SITE_HOSTS=y4006948.com,www.y4006948.com`
  用来判断"哪些链接算站内"。默认已包含这两个域名，并且代码还会叠加当前请求的 Host
  （所以 Tor 隐藏服务的域名也自动算站内），一般不用写。

### 第 3 步：初始化表（只执行一次，在任意一台 Web 上做）

```bash
cd /home/campus-wall
.venv/bin/python app.py initdb
```

`app.py` 启动时会自动读取同目录的 `campus_wall.env` 填补环境变量
（已存在的环境变量优先、不会被覆盖），所以**不需要先 `source`**。
如果仍然报 `(1045, "Access denied ... using password: NO")`，说明
`/home/campus-wall/campus_wall.env` 不存在或里面没有 `DB_PASSWORD`，
systemd 服务也会因此连不上库——先确认这个文件和 `DB_*` 四项。

> 这一步幂等，重复执行不会重复建表；示例题库只在空库时播种一次。
> **上线前务必在 `/admin/questions` 把示例题替换为只有本校学生知道答案的真题。**
>
> **升级注意**：本次新增了 `quiz_attempts`（注册答题失败计数）和 `drafts`（草稿箱）两张表，
> 并给 `replies` 补了 `is_anonymous` 列（回复匿名）。全部写在 `initdb` 里：
> 建表用 `CREATE TABLE IF NOT EXISTS`，补列用 `ALTER TABLE replies ADD COLUMN IF NOT EXISTS`，
> 重跑一次即可，不动已有数据。**必须有 `drafts` 表，否则草稿箱会直接 503**；
> `replies.is_anonymous` 缺失则回复查询会报错，同样务必补跑。

### 第 4 步：启动服务（两台都启动）

systemd 单元示例（`/etc/systemd/system/campus-wall.service`），关键是 `EnvironmentFile`：

```ini
[Unit]
Description=Campus Wall (Flask)
After=network.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
User=campus-wall
Group=campus-wall
WorkingDirectory=/home/campus-wall
Environment="PATH=/home/campus-wall/.venv/bin"
EnvironmentFile=/home/campus-wall/campus_wall.env
ExecStartPre=/bin/sh -c "ss -lntp 2>/dev/null | grep -q ':5000 ' && { echo '[campus-wall] 端口 5000 已被其他进程占用，请先 kill 掉它'; exit 1; } || true"
ExecStart=/home/campus-wall/.venv/bin/gunicorn -w 2 -b 127.0.0.1:5000 --access-logfile /dev/null app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

> `StartLimit*` 与 `ExecStartPre` 那几行的用途：端口被游离的老进程占住时，
> gunicorn 一启动就退出，默认的 `Restart=always` 会让它「退出→3 秒后重试」无限循环，
> `systemctl status` 在重试间隙还显示 `active (running)`，极容易误判成服务正常。
> 加了这两处之后，连续失败 3 次就会停在 `failed` 状态，journal 里还会直接写出
> 「端口 5000 已被其他进程占用」。修好端口后记得 `systemctl reset-failed campus-wall`。


> **别用 root 跑 gunicorn**（`campus_wall_service.txt` 里现在是 `User=root`）。
> Web 进程一旦被打穿，root 就是整台机器。改成专用账号只需三步：
> `useradd -r -d /home/campus-wall -s /sbin/nologin campus-wall`、
> `chown -R campus-wall:campus-wall /home/campus-wall`、把上面两行 `User/Group` 写进单元文件后重启。
> 注意 `chown` 要包含 `.venv` 和 `.last_sweep` 所在的目录，否则进程写不了清理标记。

> **不要在单元文件里用 `python3 -c "import secrets;..."` 现生成 `SECRET_KEY`**：
> 那样每次重启密钥都会变，所有人被强制掉登录，两台机器的 Session 也互相不认。
> `SECRET_KEY` 必须写在 `campus_wall.env` 里并由 `EnvironmentFile` 载入。
>
> 启用服务前先确认 `campus_wall.env` 确实存在且为 `600` 权限（`ls -l` 检查一次）。
> systemd 对 `EnvironmentFile` 是硬失败：文件不存在时服务直接起不来，而不是回退到默认值。

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now campus-wall
```

每台机器上再用 nginx 把 HTTPS 反代到 `127.0.0.1:5000`（你现有的 nginx 配置可直接沿用）。

### 第 5 步：旧数据迁移（仅老用户、一次性）

如果 v4/v3 时期已经有 SQLite 数据（`campus_wall.db` 和 `static/uploads/`），
在任意一台 Web 上执行（目标库必须是刚 initdb 的空库）：

```bash
cd /home/campus-wall
.venv/bin/python migrate_from_sqlite.py ./campus_wall.db
```

（`migrate_from_sqlite.py` 会自己读取同目录的 `campus_wall.env`，不需要先 `source`。）

用户、帖子、回复、题库按原 id 导入，旧图片文件会被读入数据库（MEDIUMBLOB）。

---

## 第 6 步：让两台机器一起对外服务

**方式一：DNS 轮询（最简单，但无健康检查）**
给域名加两条 A 记录分别指向两台机器 IP。缺点：一台宕机时，约一半用户仍会被分到坏机器，
且浏览器缓存 IP 期间持续故障。

**方式二：nginx 负载均衡（推荐，带健康检查）**

在一台独立机器（或数据库机）上：

```nginx
upstream campus_backend {
    server 10.0.0.11:5000 max_fails=2 fail_timeout=10s;
    server 10.0.0.12:5000 max_fails=2 fail_timeout=10s;
}
server {
    listen 443 ssl;
    server_name wall.example.com;
    ssl_certificate     /etc/nginx/ssl/cert.pem;
    ssl_certificate_key /etc/nginx/ssl/key.pem;
    access_log off;                 # 零日志原则：不记录访问者 IP
    location / {
        proxy_pass http://campus_backend;
        proxy_set_header Host $host;
    }
}
```

某台 Web 连续失败 2 次后，nginx 自动把它摘除 10 秒，故障对用户透明。

---

## 上线前自检清单

逐条过一遍，任何一条没做都会在线上暴露：

| # | 检查项 | 怎么确认 | 漏了会怎样 |
|---|--------|----------|-----------|
| 1 | `campus_wall.env` 里有 `SECRET_KEY`（两台完全一致） | `grep -c '^SECRET_KEY=' campus_wall.env` 返回 1 | 应用**拒绝服务**（500 提示服务器配置错误）；这是故意的，避免用公开默认密钥伪造 Session |
| 2 | `initdb` 跑成功过一次 | `SHOW INDEX FROM posts` 能看到 `idx_posts_time`、`idx_posts_user_time`；`SHOW INDEX FROM replies` 能看到 `idx_replies_user_time`；`SHOW TABLES` 里有 `login_attempts` | 时间线清理与配额统计每次全表扫描；登录防冒充退化成会话内计数（换个浏览器就绕过去了） |
| 3 | 题库换成校内真题 | 登录后 `/admin/questions`，把 7 道「示例题」删掉或改内容 | 任何人答「1+1=2」就能注册，防外校设计失效 |
| 4 | nginx 已换新配置并 reload | `grep client_max_body_size /etc/nginx/conf.d/*.conf`；`nginx -t && systemctl reload nginx` | 图片上限仍是 nginx 默认 1m，10MB 图直接 413 |
| 5 | gunicorn 已安装 | `.venv/bin/gunicorn --version` | systemd 服务起不来 |
| 6 | 服务不要用 root 跑 | `grep -E '^User=' /etc/systemd/system/campus-wall.service` | 进程被打穿 = 整机失守 |
| 6.1 | **只有一个 campus 单元，且已开机自启** | `systemctl list-unit-files \| grep -i campus` 只出现 `campus-wall.service` 且状态是 `enabled`；`ss -lntp \| grep ':5000'` 的 PID 要等于 `systemctl show -p MainPID campus-wall` | 两份单元抢 5000：新服务起不来、旧单元一直用老代码应答（页面看着正常但永远不更新）；`disabled` 则机器一重启站点就没了 |
| 7 | `info/` 目录（onion 私钥、四个 key 文件）移出项目目录 | 项目目录里 `ls info` 应为空或不存在 | 私钥随代码/备份一起泄露，Tor 身份和前门全部作废 |
| 8 | 每日备份 | `mysqldump --single-transaction campus_wall \| gzip > /backup/$(date +%F).sql.gz` 放 cron | 数据只有一份（图片在 BLOB 里，文件系统备份救不回来） |
| 9 | Cloudflare 规则按下一节调整 | 面板里核对顺序与动作 | 注册被反复质询、表单体丢失 |
| 10 | Turnstile 的两把 key 已写进两台机器的 `campus_wall.env` | `grep -c '^TURNSTILE_' campus_wall.env` 返回 2；登录页源码里有 `challenges.cloudflare.com/turnstile` | 登录不做人机验证（留空也不报错，只是静默不启用），脚本可无限猜学号+身份信息 |
| 11 | 旧的 `/register/jump` 质询规则已从 Custom rules 删除 | 面板里只剩「国外NP访问」等仍在用的规则 | 白占规则名额；指向一个已 404 的路径，纯误导 |
| 12 | `/privacy` 能匿名打开且引用了 Turnstile 补充条款；新注册会被强制拦在同意页 | `curl -sI https://y4006948.com/privacy \| head -1` 返回 200；`grep -c turnstile-privacy-policy` ≥ 1；用一个新学号走一遍注册，应停在「滚动到底→勾选→开始答题」那一步 | Turnstile 隐形模式在使用条件上不达标；访客也能不看社区准则就发言 |

补充说明：

- **不要把 `python app.py` 当成生产方式**：它现在只监听 `127.0.0.1:5000`、默认 `debug=False`（
  只有显式设 `FLASK_DEBUG=1` 才会开调试器）。生产请走 systemd + gunicorn。
- **会话有效期**：默认是浏览器会话 Cookie，关掉浏览器就要重新登录。想要「记住 30 天」，
  在 `app.py` 里把 `session.permanent = True` 打开并设 `PERMANENT_SESSION_LIFETIME`，
  代价是共用设备上更容易被下一个人直接进入账号。
- **置顶帖不参与 90 天清理**：管理员公告（最多 3 条）会一直留着，取消置顶后回归普通帖按时间清理。

---

## 登录人机验证（Cloudflare Turnstile，不可见模式）

替代了原来那套「注册前先跳一次 `/register/jump` 让 CF 质询」的做法 —— 那个跳转页本身就不稳定，
而且只挡注册、不挡登录。现在是：**登录提交时校验一次，不可见，用户无感**。

### 代码侧行为（已实现）

- 环境变量 `TURNSTILE_SITE_KEY` / `TURNSTILE_SECRET_KEY` **两个都填**才启用；任一为空则完全不启用（本机开发就该留空）。
- `.onion`、`localhost`、`127.0.0.1` 自动跳过：Tor 里连不上 `challenges.cloudflare.com`，强校验等于把门焊死。
- 前端：`api.js?render=explicit` + `appearance: 'interaction-only'` + `execution: 'execute'`，
  组件挂在隐藏容器里，点「进入校园墙」时才执行，拿到 token 后写进隐藏域提交。
  正常用户不会看到任何东西；CF 觉得可疑时才可能自动弹出一次点击。
- 后端：`turnstile_verdict()` 把 `secret` + `response` POST 到 `siteverify`，**故意不传 `remoteip`**
  （本站不存访客 IP，也就不把它转手交给第三方）。
- 三种结果：
  - 通过 → 正常往下走；
  - 未通过/没有 token → `400` + 提示「人机验证未通过，请重试」；
  - 校验接口够不着（超时 6 秒 / 网络错误）→ **放行**，交给原有的身份四要素校验与「连续 5 次不符锁 1 小时」兜底。
    宁可放行也不让一次 CF 抖动把全校挡在门外，这是有意的取舍。
- 注册不再有跳转页：`POST /login`（新学号）→ 直接 `302 /register/quiz`。旧地址 `/register/jump` 现在 404。

### 你要在 Cloudflare 面板做的事

1. 控制台 → **Turnstile** → 添加站点：
   - 域名填 `y4006948.com`（不需要带 `www.`）
   - 小组件类型选 **不可见（Invisible）**
   - 添加后得到 **Site Key**（公开，前端用）和 **Secret Key**（私密，只放服务器）
2. 两台 Web 的 `/home/campus-wall/campus_wall.env` 都加上（内容必须一致）：
   ```
   TURNSTILE_SITE_KEY=0x4AAAAAAA...
   TURNSTILE_SECRET_KEY=0x4AAAAAAA...
   ```
   然后 `chmod 600 campus_wall.env`。
3. 重启：`systemctl restart campus-wall`（两台都做）。刷新登录页，页脚版本号变了就说明新代码生效。
4. **删掉旧的质询规则**：Custom rules 里那条 `URI 路径等于 /register/jump` 的规则已经没有意义，
   留着只会白占规则名额。顺手把「国外NP访问」那条加个方法限制：
   `... and http.request.method eq "GET"`，否则它会连登录 POST 一起质询。
5. **隐私政策**：Turnstile 的隐形模式要求站点隐私政策里引用
   [Cloudflare Turnstile 隐私补充条款](https://www.cloudflare.com/privacypolicy/)。
   本站目前没有隐私政策页——要么补一页，要么至少在这份 README 里留痕（见下）。

### 验证是否真的生效

```bash
# 1. 服务端有没有读到 key（页面底部应出现 Turnstile 脚本）
curl -s https://y4006948.com/login | grep -c 'challenges.cloudflare.com/turnstile'

# 2. 不带 token 直接 POST，应该 400，而不是 302
curl -s -o /dev/null -w '%{http_code}\n' -X POST https://y4006948.com/login \
  -d 'student_id=25700000001&grade=25&class_name=%E7%BD%91%E7%BB%9C1&name=%E5%BC%A0%E4%B8%89'
# 期望 400（人机验证未通过）；如果返回 302，说明 secret 没生效或 host 被判定为本机/onion
```

> `.onion` 入口不做验证：那边连不到 CF，行为与改造前完全一致。
>
> 代价：如果访客所在网络把 `challenges.cloudflare.com` 整个屏蔽了，登录页会提示「人机验证组件没加载出来」，
> 这种网络下无法登录（这是接第三方人机验证的固有成本）。真要兜住，只能把这两把 key 留空关掉验证，
> 或者让这类访客走 `.onion`。

### 隐私政策及社区准则（`/privacy`）

Turnstile 隐形模式的启用条件之一就是「站点隐私政策里必须引用 Cloudflare 的 Turnstile 隐私补充条款」，
所以站内已经做了一页 **公开可读、不需要登录** 的 `/privacy`（`templates/privacy.html`，路由 `privacy()`）：

- 页脚「萌ICP备20261214号」旁边有入口，全站可点。
- **隐私政策与社区准则合并成一页**（正文抽在 `templates/_policy.html` 这个共用片段里，
  `/privacy` 和注册同意弹窗引用的是同一份内容，不会出现两份规则打架）。
  社区准则那一节写的是发帖规范：礼貌用语、人身攻击、歧视、**开盒/人肉**、违法与垃圾信息、
  匿名不是免责、冲突处理、违规处置梯度（删帖→警告→限制→封号）。
- **新用户注册强制同意一次**：`POST /login`（新学号）→ `302 /register/agree` → 该页会**强制弹出一个关不掉的窗口**
  （`data-bs-backdrop="static"` + `data-bs-keyboard="false"`，没有关闭按钮），条款放在可滚动框里，
  **滚到最后一行才解锁勾选框**，勾选后点「开始答题」才 `302 /register/quiz`。
  服务端另有一道守卫：没有 `session["agree_ok"]` 直接访问 `/register/quiz` 会被打回 `/register/agree`，
  `agree` 字段不是 `1` 也会被打回。已注册的老用户登录不经过这一页（他们的账号在改规则之前就已建立）。
- 内容不是套模板，是照本站真实行为写的：收哪几项、不记 IP/UA、只有会话 Cookie、
  Cloudflare 与 Turnstile 各自看到什么、**校验时不传 `remoteip`**、90 天清理、草稿 3 条、
  匿名只是"前台匿名"、注销需联系管理员、服务器在境外。
- 引用链接：`https://www.cloudflare.com/turnstile-privacy-policy/`（Turnstile Privacy Addendum）
  与 `https://www.cloudflare.com/privacypolicy/`（主政策）。
- 联系邮箱：`PRIVACY_CONTACT_EMAIL` **可以写多个**，用逗号 / 分号 / 顿号 / 斜杠 / 空格 / 中文「或」分隔都认
  （`a@x.com或b@y.com` 会渲染成两个独立链接）；非法项静默丢弃，不会在页面上变成一个畸形 mailto。
  留空则只提示「站内联系管理员」。
- 联系电话：`PRIVACY_CONTACT_PHONE`，写成 `tel:` 链接 + 「发短信」`sms:` 链接 + `wa.me` 的 WhatsApp 链接，
  旁边注明「可发短信、也可用 WhatsApp 发消息至该号码」。支持 `+86 138-0013-8000` 这类写法，
  只写 11 位手机号会自动按 `+86` 处理，`+852` 香港号码会按 4-4 分组显示；
  不像号码的值一律不渲染（页面不会留一行空的「电话：」）。
  **号码会公开在任何访客都能打开的页面上**，一定会被爬虫抓走，建议用一个专门的号，或干脆只留邮箱。
- **改动数据处理方式或社区规则时（新接第三方、增加采集字段、改保留期）必须同步改 `_policy.html`**，
  否则页面写的和实际做的对不上，那是比没有隐私政策更糟的情况。
- 顺带记一笔：页脚的「异次元之旅」徽标是外链图片，会由访客浏览器直接向第三方请求（对方能看到 IP）。
  本站在正文里已如实披露；如果想把「全站零外链」贯彻到底，就把那张图换成自托管到 `static/`。

---

## 排错：改了代码，页面却没变 / 提示一会儿站内一会儿是浏览器弹窗

典型症状：同样的按钮，点一次弹出**站内小条**，再点一次又变回**浏览器原生对话框**；
或者报错信息还是上一版的旧文案。这基本不是后端逻辑问题，而是**你看到的页面不是最新那一份**。

判断顺序（从快到慢）：

1. **先看页面版本号**：页脚右下角有一行灰色小字，形如 `v20261107.1542`
   （= `app.py` 与 `templates/` 里最新的修改时间，换文件就自动变，不用手改）。
   没有这行、或版本号对不上，说明这份 HTML 是旧的，后端改什么都没用。
2. **看响应头**（不依赖浏览器缓存）：
   ```bash
   curl -sI https://y4006948.com/ | grep -iE "x-cw-build|cache-control|cf-cache-status|age"
   for i in 1 2 3; do curl -sI https://y4006948.com/ | grep -i x-cw-build; done
   ```
   - `x-cw-build` 与 `app.py` 里对不上 → 打到的机器还是旧代码（多机部署时逐台查 `md5sum`）。
   - 三次 `x-cw-build` 不一样 → 多台 Web 代码不同步。
   - `cf-cache-status: HIT` → Cloudflare 边缘缓存的旧 HTML，去 Caching → Purge Everything，
     或临时开 Development Mode。
3. **服务是否真的重启**：`systemctl status campus-wall`、`ps aux | grep gunicorn`，
   确认进程启动时间晚于你上传文件的时间。
4. **浏览器本身**：Ctrl+F5 强刷，或用无痕窗口；`后退` 回到旧页面时 Chrome 可能从
   bfcache 直接恢复老页面，这一样会表现成「版本倒退」。
5. **有没有一个"游离"的旧进程占着 5000**（最容易骗人的一种）：
   ```bash
   systemctl status campus-wall      # 注意看有没有 "Connection in use: ('127.0.0.1', 5000)"
   ss -lntp | grep ':5000'           # 谁在监听
   systemctl show -p MainPID campus-wall
   ```
   如果 `ss` 报的 PID 和 `MainPID` **不是同一个**，说明有个不是 systemd 管的 gunicorn
   （之前手工 `gunicorn ...` 起的，或旧单元留下的）还活着，它一直在用**自己内存里的老代码**
   应答请求：模板改了也不会变、接口行为还是旧的、置顶还会返回网页而不是 JSON。
   `systemctl status` 这时可能仍显示 `active (running)`——那只是「启动→绑定失败→退出→
   3 秒后重试」循环里的一个瞬间，不是真的在服务。
   处理：
   ```bash
   systemctl stop campus-wall
   ss -lntp | grep ':5000'           # 还有输出就 kill 掉那个 PID
   kill <PID>
   systemctl reset-failed campus-wall
   systemctl start campus-wall
   ss -lntp | grep ':5000'           # 这次只应剩 systemd 的 MainPID
   ```
6. **不要手工起 gunicorn**（`gunicorn app:app &`、`nohup ...`）：它不受 systemd 管，
   下次 `restart` 必然撞端口，而且会一直悄悄用旧代码提供服务。要测就 `systemctl start/stop`。
7. **服务改过名 / 建过两份单元**（最容易踩，而且不看这一条根本想不到）：
   比如把 `campus_wall.service` 改名成 `campus-wall.service`——旧单元文件还留在
   `/etc/systemd/system/`、还是 enabled、进程还在跑，它照样占着 `127.0.0.1:5000`；
   你 `systemctl restart campus-wall` 起的新进程绑定失败，**请求一直由旧单元那套老代码应答**。
   症状和「手工起了个游离进程」完全一样：文件 md5 对得上、服务显示 running、页面还是旧版。
   ```bash
   systemctl list-unit-files | grep -i campus     # 有没有两个（带下划线和带横线的）
   systemctl list-units --type=service --state=running | grep -i campus
   systemctl disable --now campus_wall.service    # 旧的那个停掉并取消开机自启
   rm -f /etc/systemd/system/campus_wall.service
   systemctl daemon-reload
   systemctl reset-failed campus-wall
   systemctl start campus-wall
   ```
   判定标准永远是同一条：`ss -lntp | grep ':5000'` 出来的 PID == `systemctl show -p MainPID`。
   改服务名属于「一次性迁移」，务必把旧单元 `disable --now` 并删除文件，只改名不清理等于两套并存。

代码侧已经做的防护（本节相关部分已加，避免再靠猜）：

- 页面/JSON 响应一律 `Cache-Control: no-store`，并附带 `CDN-Cache-Control: no-store`、
  `Cloudflare-CDN-Cache-Control: no-store` —— 即使 CF 规则没拦住 HTML，边缘也不该存它。
- 所有响应带 `X-CW-Build`；前端拿到接口响应时会比对页面版本，不一致就直接提示
  「页面是旧缓存（页面 v…/服务器 v…），请按 Ctrl+F5 强刷」，不再是一句含糊的“操作失败”。
- 页脚显示版本号，肉眼就能确认自己看的是哪一版。
- `/static/`、`/media/post/<id>` 仍然允许缓存（内容不变，省流量、省数据库读取）。

---

## 本机开发（Windows / Linux）

```bash
# 设置环境变量（Windows PowerShell）
$env:DB_HOST="127.0.0.1"; $env:DB_USER="campus_wall"; $env:DB_PASSWORD="..."; $env:SECRET_KEY="dev"
# 初始化
python app.py initdb
# 启动
python app.py
```

---

## 前端两条铁律（踩过坑，别再犯）

1. **表单字段不要叫 `action`、`submit`、`method`、`id` 这类名字**。
   HTML 规范里表单里的同名输入框会**顶替** `form.action` / `form.submit` 等属性：
   `form.action` 会返回那个 `<input>` 对象本身，`fetch(form.action)` 拼出来的地址就是
   `/object HTMLInputElement`，服务端回 404「接口不存在：/object HTMLInputElement」。
   置顶按钮就栽在这上面（删除没事，因为它的表单没有这个字段，所以现象是"删除正常、置顶抽风"）。
   - 置顶的隐藏字段已改名 `pin_act`；服务端 `set_pin` 仍兼容旧的 `action` 字段。
   - 前端统一用 `form.getAttribute('action')` 取地址，不用 `form.action`。
2. **同一段 `<script>` 里不要重复声明同名变量**（比如两处 `const postForm`）：
   整段脚本直接语法失败，表现是"日志干净、页面能加载、所有按钮全死"。
   改完模板用 `node --check` 跑一遍（`<script>` 内容抽出来、Jinja 标签替换掉即可）。
3. **要在前端切换显示的元素，必须始终渲染在 DOM 里**（默认加 `d-none`），
   不要用 `{% if %}` 让它"不置顶时压根不存在"：`card.querySelector('.pin-badge')`
   会返回 `null`，`null.classList` 直接抛 `Cannot read properties of null`。
   置顶徽标就踩过这个坑（接口已经成功、界面报错，看起来像接口又坏了）。
   前端 toggle 时再补一层 `if (badge) …` 兜底。

---

## 功能与安全规则（与 v4 一致）

- **登录**：学号 11 位纯数字、姓名 2–4 个中文、年级 1–2 位数字、班级 1–10 位中文/数字/字母，
  前后端双校验；学号已存在则四要素必须完全一致，不符统一回复"登录失败，请检查身份信息是否匹配"；
  新学号需答对随机 3 道校内题才注册。
- **登录防冒充**：同一学号连续 **5 次**「学号存在但年级/班级/姓名不符」即锁定 **1 小时**
  （计数落库在 `login_attempts`，换浏览器、清 Cookie 绕不过；表缺失时退化为会话内计数）。
  锁定期内即使信息正确也进不去。注意这条对**陌生学号**不生效——库里查不到的学号走注册流程，不计入。
  副作用要清楚：知道某人学号的人可以故意连错 5 次，把对方登录锁住 1 小时（而且能不断续期）。
  影响有限（等一小时即可，数据不受影响），要更温和就把 `LOGIN_LOCK_HOURS` 改小或只锁当前会话。
- **注册防爆破**：下发的 3 道题 ID 绑在当次会话里，提交时必须与之一致（防止固定题号慢慢套答案）；
  同一学号累计答错 **5 次** 即锁定 **1 小时**，计数落库在 `quiz_attempts`，换浏览器、清 Cookie 也绕不过。
  触发上限时会给出提示并退回登录页。
- **发帖**：Markdown 白名单清洗（`script/iframe/img/javascript:` 全部剥离）；
  排版类全开——标题、有序/无序列表、引用、代码块、表格、删除线、分隔线；
  **`img` 仍禁用**（外链图片会让每个浏览者的浏览器主动请求第三方地址，把 IP/Referer 送出去）；
  **`a` 已开放**，但站外链接会被改写成 `/link-out?u=...`，点开先看到
  「您即将访问 xxx，该页面与本站无关」的提示页，用户确认后才跳出去。
  站内链接（相对路径、`y4006948.com` 等本站域名、当前访问的 Host，含 Tor 域名）**不套提示页，直接跳转**。
  页脚那个「萌ICP备」链接是模板里写死的，同样不受影响。
  匿名帖显示"年级班级 ***"，年级班级保留；单图 ≤10MB（JPG/PNG/GIF/WEBP，魔数验真），
  前后端与 nginx（`client_max_body_size 12m`）三处上限已对齐。
  图片可以点「添加图片」选，也可以**直接在输入框里 Ctrl+V 粘贴截图、或把图片拖进发帖框**，
  三种方式走的是同一个 file input，校验和预览完全一致。
- **回复**：纯文本、≤200 字，**可勾选匿名**；匿名回复显示 `***`，
  否则姓名会通过"回复一句"整个暴露出去。回复不做 Markdown 渲染。
  普通账号**每小时最多 30 条**（发的时候不提示、不显示剩余次数，超了才拒绝并说明原因），
  管理员不受限制；计数直接查 `replies` 表最近一小时的记录，不额外落表。
- **草稿箱**：发帖框右侧「存为草稿」按钮，只存文字（图片不随草稿保存）；
  每人最多 **3 条**，超出自动删最早的一条；在草稿箱里可以改文字、**随时切换实名/匿名**、
  发布或删除；「发布」按输入框里的当前内容发出，同样受每日 10 条上限约束。
- **每日发帖上限**：普通账号每个**北京时间自然日**最多 **10 条**（0 点重置），
  管理员不受限制（实时查库确认身份，不依赖 Session 旧值）。
- **时间线分页**：一页 **15 条帖子**（回复只查询当前页这些帖子的，图片仍是访问时才单独请求），
  页码走 `/?page=N`；`page=0`、负数、非数字、超出末页都会自动钳到合法范围，不会 404。
  超过一页才显示「上一页 / 下一页」，总数不足一页时界面与以前完全一致。
  在第 2 页及以后发帖，前端会跳回第 1 页，让你看到刚发的那条（新帖属于最新一页）。
- **置顶**：仅管理员可用，最多 3 条，置顶帖按置顶时间倒序排在最前。
- **管理员管理**：超级管理员为学号 `25710202061`（注册时自动获得权限，永久不可被取消）。
  管理员登录后首页右上角只有一个「管理」下拉，里面是「管理员」和「题库管理」两项，不再占两个按钮位。
  点「管理员」进入 `/admin/admins`：输入对方 11 位学号即可提拔管理员，
  对方必须已注册过；提权后让对方**重新登录一次**即可看到管理入口，**取消管理员则即时生效**
  （管理权限每次请求实时查库，不存在旧 Session 残留）。超管不能被取消、也不能取消自己。
- **清理**：90 天前的帖子随访问自动删除（每台 Web 每天最多执行一次，幂等）；
  同一轮清理顺带删掉 30 天前的答题失败记录。
- **降级**：数据库连不上或查询中途断开时返回 **503** 友好页（`templates/unavailable.html`），
  AJAX 请求返回 `{"ok": false, "error": ...}`，不会把 Python 堆栈暴露给用户。
- **零日志**：库中无 IP/User-Agent 字段，不读取、不记录；gunicorn 与 nginx 均关闭 access log。

## 备份与恢复

图片已在库中，一个转储文件包含全部数据（用户、帖子、图片、回复、题库）：

```bash
# 备份
mysqldump -u root -p --single-transaction --default-character-set=utf8mb4 campus_wall > wall_backup.sql
# 恢复
mysql -u root -p --default-character-set=utf8mb4 campus_wall < wall_backup.sql
```
