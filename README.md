# netconsole 发送端（Ubuntu 24.04）

把 Ubuntu 上的日志主动推给接收端（配套的 `netconsole_receiver.py`，Windows / Linux 都能跑）。
**默认 TCP：由本机主动 `connect` 接收端**；也可以 `--udp` 走无连接发送，兼容内核 netconsole 的习惯。

- 环境：Ubuntu 24.04（22.04+ 也可），系统自带 Python 3.12，**不需要 pip 装任何东西**
- 只主动向外发，Ubuntu 侧不需要开入站端口；要放行的是接收端那台机器的入站

## 文件

| 文件 | 用途 |
| --- | --- |
| `netconsole_sender.py` | 发送端主程序（纯标准库） |
| `install.sh` | 一键安装成 systemd 服务，开机自启、断线自动重连 |
| `netconsole-sender.service.in` | systemd 单元模板（install.sh 用它生成实际单元） |
| `README.md` | 本说明 |

## 快速开始

1. 把整个 `netconsole-sender` 目录传到 Ubuntu（Windows 上）：

   ```powershell
   scp -r .\netconsole-sender\ user@192.168.1.10:/home/user/
   ```

2. 先确认数据源（不发送，只打印）：

   ```bash
   cd ~/netconsole-sender
   python3 netconsole_sender.py --host 192.168.50.164 --journal --dry-run
   ```

3. 正式发送（前台的 journal 全量日志）：

   ```bash
   python3 netconsole_sender.py --host 192.168.50.164 --journal
   ```

4. 接收端（Windows）窗口里应开始出现日志：

   ```
   2026-11-04 18:02:11.345  TCP  192.168.1.10:49212   INFO    2026-11-04T18:02:11+0800 ubuntu sudo[1234]: ...
   ```

> `192.168.50.164` 换成接收端启动横幅里打印的那个 IP；接收端默认同时监听 UDP + TCP 的 52221。

## 数据源

四选一，不给就用标准输入。所有模式都是**逐行、字节原样透传**，UTF-8 / GBK / 二进制都不会让程序崩掉。

| 模式 | 写法 | 说明 |
| --- | --- | --- |
| journald | `--journal`、`--journal nginx ssh` | `journalctl -f -o short-iso`，可跟 systemd 单元名 |
| 文件 | `-f /var/log/syslog` | `tail -F` 语义，自动处理日志轮转、截断、删除重建 |
| 命令 | `-c "ping 8.8.8.8"` | 用 shell 执行，stdout+stderr 一起转发，命令退出即结束 |
| 内核日志 | `--dmesg` | `dmesg -w`，需要 root（Ubuntu 默认 `kernel.dmesg_restrict=1`） |
| 管道 | `journalctl -f \| python3 netconsole_sender.py --host ...` | 最灵活，任何能产出文本的命令都行 |

常用组合：

```bash
# 只看 nginx
python3 netconsole_sender.py --host 192.168.50.164 --journal nginx
# 从文件开头读，并给每行加 INFO 级别和主机标签
python3 netconsole_sender.py --host 192.168.50.164 -f /var/log/syslog --from-start --level 6 --tag web01
# 长期跑一个命令
python3 netconsole_sender.py --host 192.168.50.164 -c "tail -F /var/log/myapp.log"
# 最快的连通性测试
python3 netconsole_sender.py --host 192.168.50.164 -c "echo hello from ubuntu; sleep 1"
```

## 装成 systemd 服务（推荐长期用）

```bash
sudo ./install.sh 192.168.50.164            # 全量 journal
sudo ./install.sh 192.168.50.164 52221 nginx ssh   # 只发 nginx 和 ssh
```

它会：把脚本装到 `/usr/local/bin/netconsole_sender.py`，按你的 IP/端口生成
`/etc/systemd/system/netconsole-sender.service`，然后 `enable --now`。

```bash
systemctl status netconsole-sender --no-pager   # 状态
journalctl -u netconsole-sender -f              # 看发送端自己的日志
sudoedit /etc/systemd/system/netconsole-sender.service   # 改完记得 daemon-reload + restart
```

## 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 接收端 IP / 主机名 |
| `--port` | `52221` | 接收端端口 |
| `--udp` | 关 | 改用 UDP（默认 TCP 主动连接） |
| `--timeout` | `5` | 连接超时（秒） |
| `-f, --file PATH` | — | 跟踪文件 |
| `--from-start` | 关 | 配合 `--file`，从文件头开始读 |
| `-c, --command CMD` | — | 执行命令并转发输出 |
| `--journal [UNIT...]` | — | 跟随 journald |
| `--dmesg` | 关 | 跟随内核日志 |
| `--level N` | 关 | 每行加 `<N>` 前缀（0=EMERG … 7=DEBUG），接收端显示彩色级别 |
| `--tag NAME` | 关 | 每行加 `[NAME] ` 标签，多台机器时便于区分 |
| `--backlog N` | `2000` | 断线期间最多缓存多少行，连上后补发 |
| `--queue-size N` | `10000` | 内部队列，满了会给数据源施加反压（不丢数据） |
| `--drain-timeout SEC` | `120` | 数据源结束后再等多久把缓存发完，0 = 一直等 |
| `--stats SEC` | `60` | 统计打印间隔，0 = 关闭 |
| `--dry-run` | 关 | 只打印不发送 |
| `--quiet` | 关 | 不打印状态提示 |

## 断线、重连与缓存

- 接收端没开 / 网络抖动：连接失败后按 **1→2→4→…→30 秒** 退避重试，连上后自动把缓存的日志补发。
- 缓存上限 `--backlog`（默认 2000 行），超出丢最旧的，结束时会打印丢弃行数。
- 发送端检测到长时间写不出去会主动断开重连（`TCP_USER_TIMEOUT` 15 秒），不会卡死在半开连接上。
- `Ctrl+C` / `systemctl stop` 会优雅退出：先尽量把缓存发完，再关闭连接。

## 和内核 netconsole 的分工

| | 本脚本（用户态） | 内核 netconsole（modprobe） |
| --- | --- | --- |
| 协议 | TCP（可 UDP） | 只能 UDP |
| 能抓 | journald、文件、任意命令输出 | printk（含 oops / panic / 启动早期） |
| 依赖 | Python、网络栈已就绪 | 内核模块加载后即可，不需要用户态 |
| 端口配置 | `--port 52221` | `netconsole=...,52221@接收端IP/MAC` |

两者可以同时用：接收端 UDP 收内核、TCP 收脚本，互不影响。内核侧配置见
[接收端说明](../netconsole-receiver/README.md) 的「Linux 内核 netconsole」一节。

## 权限

| 数据源 | 需要 |
| --- | --- |
| `--journal` | root，或用户在 `systemd-journal` / `adm` 组：`sudo usermod -aG systemd-journal $USER`（重新登录生效） |
| `--dmesg` | root，或临时放开：`sudo sysctl -w kernel.dmesg_restrict=0` |
| `-f /var/log/xxx` | 对应文件可读（`/var/log/syslog` 属 `adm` 组） |

`install.sh` 生成的服务默认以 root 运行，开箱即用；想降权就改单元里的 `User=`/`SupplementaryGroups=`。

## 排查

1. **`[!] 连接 x.x.x.x:52221 失败：Connection refused`**：接收端没启动或端口不对。
2. **`timed out`**：多半是接收端防火墙没放行入站（Windows 要加 UDP/TCP 52221 入站规则），
   或两台机器不在同一网段。先在 Ubuntu 上 `nc -vz 接收端IP 52221` 验证 TCP 可达。
3. **连上了但接收端没输出**：用 `--dry-run` 确认数据源真的在产出；`--journal` 没数据多半是权限。
4. **中文乱码**：脚本原样转发字节，接收端用 `--encoding gbk` 解析即可。
5. **UDP 模式偶发丢行**：UDP 不重传，属正常；要可靠就用默认 TCP。
6. **`journalctl: command not found`**：本机不是 systemd 系统，换 `--file` / `--command` / 管道模式。
