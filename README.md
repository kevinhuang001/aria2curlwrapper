# aria2curl

> 一个 curl 的**透明代理**：命令行保持 curl 的用法与语义，实际下载交给 aria2 多线程加速。

```console
$ aria2curl -o ubuntu.iso https://releases.ubuntu.com/24.04/ubuntu-24.04-desktop-amd64.iso
```

`aria2curl` 解析你的 curl 命令行：

* **能忠实翻译** → 交给 `aria2c`（多连接分块、断点续传、现代终端进度面板）；
* **翻译不了**（POST 数据、上传、字节区间、`-o -`、HTTP/2、netrc、自定义请求方法……）→ **原封不动 `execv` 真正的 curl**，脚本行为、信号、退出码完全一致。

换句话说：**它不会破坏任何现有脚本，只会让普通下载更快。** 参数解析失败也一定回退到 curl，而不是报错。

---

## 目录

- [特性](#特性)
- [安装](#安装)
- [快速开始](#快速开始)
- [透明代理的行为边界](#透明代理的行为边界)
- [全局配置](#全局配置)
- [终端显示](#终端显示)
- [与 curl 的差异（重要）](#与-curl-的差异重要)
- [退出码](#退出码)
- [开发与测试](#开发与测试)
- [架构](#架构)
- [FAQ](#faq)

---

## 特性

| | |
|---|---|
| **透明回退** | 白名单式解析：只有能被忠实翻译的命令才走 aria2，其余一律 `execv curl`。新增的 curl 选项不会改变行为，只会回退。 |
| **多连接加速** | 命令行参数 `--split` / `-x`（分块数量、单服务器连接数）由**全局配置**提供，无需每次输入。 |
| **断点续传** | `auto_resume` 默认开启：目标文件已存在且非空时自动 `-c` 续传（aria2 以 1 MiB 分片为粒度）。服务器不支持续传时自动改为重新下载。 |
| **现代终端显示** | 多文件实时仪表盘：总进度条、单文件进度条、速度、ETA、连接数、彩色状态，结束打印汇总面板。可选 `plain` / `json` / `none` 引擎。 |
| **全局配置** | `aria2curl config list/get/set/unset/reset/edit`，TOML 持久化，带类型、范围、默认值与来源（file/env/cli）标注。 |
| **一键安装** | 用户级与系统级两个 `curl … | sh` 脚本：前者写你的 shell alias，后者装到 `/usr/local` + `/etc` 并提供全进程生效的 `curl` shim（带三重防递归），都可 `--uninstall`。 |
| **零业务依赖** | 只有 `rich`（可选）用于仪表盘；RPC 客户端基于标准库 `http.client`，不依赖 aria2 之外的任何东西。 |
| **精心测试** | 268 测试，自带支持 `Range` 的测试服务器，用真实 `aria2c` 跑端到端下载、续传、错误码与回退。 |

---

## 安装

提供**两个安装脚本**：`install-user.sh`（用户级）和 `install-system.sh`（系统级）。
`install.sh` 是用户级入口的便捷别名（本地目录直接转发，管道执行时自动拉取 `install-user.sh`）。

### 方式一：用户级安装（个人使用，推荐）

```console
$ curl -fsSL https://raw.githubusercontent.com/kevinhuang001/aria2curlwrapper/main/install-user.sh | sh
```

安装脚本会：

1. 检查 Python ≥ 3.11 与 curl；
2. 用 **curl** 下载源码包（`codeload.github.com`，无需 git）；
3. 在 `~/.local/share/aria2curl/venv` 创建独立虚拟环境；
4. 软链 `~/.local/bin/aria2curl` 与 `~/.local/bin/acurl`；
5. **写入 alias**（默认 `alias curl='aria2curl'`）到你的 shell rc 文件；
6. 若没装 aria2，会询问是否用系统包管理器安装。

只动你自己的家目录，不需要 root。常用参数：

```console
$ sh install-user.sh --alias both            # 同时写 alias curl 和 alias acurl
$ sh install-user.sh --alias acurl           # 只写 acurl，不劫持 curl
$ sh install-user.sh --alias none            # 不写 alias
$ sh install-user.sh --prefix ~/.local       # 安装根目录
$ sh install-user.sh --uv                    # 用 uv 建环境（更快，可选）
$ sh install-user.sh --source .              # 从本地源码目录安装
$ sh install-user.sh --with-aria2            # 顺便安装 aria2
$ sh install-user.sh --dry-run               # 只打印将要执行的动作
$ sh install-user.sh --uninstall             # 卸载（含删除 alias 块）
```

### 方式二：系统级安装（整机所有用户）

```console
$ curl -fsSL https://raw.githubusercontent.com/kevinhuang001/aria2curlwrapper/main/install-system.sh | sudo sh
```

系统级安装做四件事：

1. 私有 venv 放到 `/usr/local/lib/aria2curl/venv`，`aria2curl`/`acurl` 软链到 `/usr/local/bin`；
2. 写 `/etc/aria2curl/config.toml`——**系统配置层**，把 `curl_path` 钉死为真正的 curl（防递归的关键）；
3. 写 `/etc/profile.d/aria2curl.sh`，给所有用户的登录 shell 加 `alias curl='aria2curl'`；
4. 加 `--wrap-curl` 时再写一个 `/usr/local/bin/curl` 透明 shim——**连脚本、cron、第三方程序里的 curl 也会走 aria2**。

#### 系统级怎么"改别名"？两种手段

| 手段 | 生效范围 | 原理 |
|---|---|---|
| `/etc/profile.d/aria2curl.sh`（默认） | 所有用户的**交互式登录 shell** | 就是用户级 alias 的系统级版本 |
| `/usr/local/bin/curl` shim（`--wrap-curl`） | **所有进程**（脚本、cron、其它程序） | `/usr/local/bin` 默认排在 `/usr/bin` 之前，shim 脚本 `exec` 到 aria2curl |

**shim 为什么不会递归？** 三层保险：

1. **shim 直接把真 curl 的地址交给我们**：它 `export ARIA2CURL_REAL_CURL=/usr/bin/curl` 后再 `exec` aria2curl，回退时优先使用这个绝对路径。这同时解决了 `/usr/local/bin` 排在 `/usr/bin` 之前导致的 PATH 顺序问题。
2. **拒绝执行"像自己"的东西**：`resolve_curl` 逐个校验候选——`_looks_like_self()`（realpath 与自身比较，抓软链/入口别名）和 `_looks_like_shim()`（读文件头 8 KiB，含 `aria2curl` 字面量即拒绝）。按**内容**判断，不依赖文件名或 PATH 顺序。
3. **`ARIA2CURL_DEPTH` 计数器兜底**：`exec_curl` 在 `execv` 前把计数 +1（shim 自己**不**加，否则内层会跳过 aria2）；任何以 `DEPTH>0` 启动的 aria2curl 会跳过全部决策，并以 **strict 模式**解析 curl —— 此时不再信任 PATH 上的裸名字，只接受绝对路径或**真正的二进制**（`#!` 脚本一律拒绝），因为能被 PATH 命中的 wrapper 正是回环入口。

即使三层都被绕过（例如有人把 shim 写成不含任何字面量的脚本并遮蔽 PATH），最坏也只是多一跳就落到真 curl；若连一个可用的绝对路径 curl 都找不到，会明确报错 `refusing to exec curl: ... would re-enter aria2curl`，而不是静默死循环。

绕开 shim 的办法：直接用 `/usr/bin/curl`，或 `sh install-system.sh --uninstall` 卸载。

常用参数：

```console
$ sh install-system.sh --wrap-curl              # 全进程透明（脚本也加速）
$ sh install-system.sh --no-profile             # 不写 profile.d，只装命令
$ sh install-system.sh --prefix /usr/local --sysconfdir /etc
$ sh install-system.sh --allow-non-root \      # 打包/容器场景（DESTDIR 式安装）
      --prefix /tmp/pkg/usr/local --sysconfdir /tmp/pkg/etc
$ sh install-system.sh --with-aria2             # 顺便 apt/dnf/pacman 安装 aria2
$ sh install-system.sh --uninstall [--purge]     # 卸载（--purge 连 /etc/aria2curl 一起删）
```

> 系统级安装会把 aria2curl 自己的 venv 和系统配置写进 `/usr/local` 与 `/etc`；
> 卸载时只会删除自己写的东西：如果 `/usr/local/bin/curl` 不是 aria2curl 写的 shim，会拒绝覆盖（需 `--force`，且会先备份）。

### 安装 aria2

强烈建议安装真正的 aria2（否则 `aria2curl` 会一直回退到 curl，功能仍可用但没有加速）：

```console
$ sudo apt install aria2        # Debian/Ubuntu
$ sudo dnf install aria2        # Fedora
$ sudo pacman -S aria2          # Arch
$ brew install aria2            # macOS
```

**没有 root 也能用**：仓库自带 `tools/fetch-aria2.sh`，用 `apt-get download` + `dpkg-deb -x` 把 aria2 及其依赖解包到工作区的 `.aria2root/`，再用 `tools/aria2c` 启动（见 [开发与测试](#开发与测试)）。

---

## 快速开始

```console
# 普通下载（-o 指定文件名，语义与 curl 完全一致）
$ aria2curl -L -o archive.tar.gz https://example.com/archive.tar.gz

# 用远端文件名保存（等价于 curl -O）
$ aria2curl -O https://example.com/big.iso

# 多个 URL 各自保存（--remote-name-all 才会全部保存为文件名）
$ aria2curl --remote-name-all https://example.com/a.zip https://example.com/b.zip

# 断点续传（-C - 显式续传；auto_resume 默认已开启）
$ aria2curl -C - -o big.iso https://example.com/big.iso

# 限速、重试、代理、Header、认证
$ aria2curl --limit-rate 2M --retry 5 -x http://127.0.0.1:7890 -u user:pass -H 'X-Token: 1' -o f.bin https://example.com/f.bin

# 看一眼"如果跑 aria2 会执行什么命令"（不下载）
$ aria2curl --acurl-dry-run -o f.bin https://example.com/f.bin

# 为什么这次没有用 aria2？（不下载，只解释决策）
$ aria2curl --acurl-explain -X POST https://example.com/api
```

### 开启 `curl` 别名后

```console
$ alias curl='aria2curl'
$ curl -O https://example.com/big.iso     # 实际由 aria2 多线程下载
$ curl -X POST -d a=1 https://api.example # 自动回退到真正的 curl
$ curl --version                          # 原样交给真正的 curl
```

> **为什么用 alias 而不是软链？** 因为 shell alias 不会被 `execvp` 继承——`aria2curl` 内部回退时调用的是真正的 `curl`，不会递归回到自己。脚本里如果写了 `~/.local/bin/curl` 软链，反而会有递归风险（`aria2curl` 也做了递归保护，见下）。

### 别名管理

```console
$ aria2curl alias print               # 打印 alias 块
$ aria2curl alias install --name both # 写入 ~/.bashrc（幂等，可重复执行）
$ aria2curl alias install --shell fish
$ aria2curl alias status
$ aria2curl alias uninstall
```

---

## 透明代理的行为边界

解析器是**白名单**结构：每个 curl 选项只有三种命运。

### 1. 翻译为 aria2 参数

| curl | aria2 | 说明 |
|---|---|---|
| `-o FILE` / `-O` / `-J` / `--output-dir` | `--dir` + `--out` / `--content-disposition` | 单文件走命令行，多文件自动生成 `--input-file`（每个 URI 单独 `out=`/`dir=`） |
| `-C -` | `--continue=true` | 显式续传；`-C N`（字节偏移）无法表达 → 回退 |
| `-H` `-b` `-c` | `--header` `--load-cookies` `--save-cookies` | `-b name=value` 转为 `Cookie:` 头；`-b FILE` 转为 `--load-cookies` |
| `-u user:pass` | `--http-user/--http-passwd`（按 URL scheme 选 http/ftp） | 无冒号（需要交互输入密码）→ 回退 |
| `--basic` | `Authorization: Basic …` 头 | 抢先认证，等价于 curl 的预置 Basic |
| `-x` `--socks4/5` `-U` `--noproxy` | `--all-proxy` `--all-proxy-user/passwd` `--no-proxy` | 也翻译 `http_proxy`/`https_proxy`/`ALL_PROXY`/`NO_PROXY` 环境变量（curl 会读、aria2 不会） |
| `-k` `--cacert` `-E` `--key` `--tlsv1.2/1.3` | `--check-certificate` `--ca-certificate` `--certificate` `--private-key` `--min-tls-version` | |
| `--limit-rate` `--retry` `--retry-delay` | `--max-download-limit` `--max-tries` `--retry-wait` | curl 的 `--retry N` = 重试 N 次，映射为 `--max-tries N+1` |
| `-m/--max-time` | 由本程序监督 | aria2 没有"总时长上限"，由 supervisor 计时并用 `aria2.remove` 中断（退出码 28），语义与 curl 一致 |
| `-Y/--speed-limit` | `--lowest-speed-limit` | 采样窗口不同，README 已注明 |
| `--compressed` `-R` `-4` `-6` `--interface` | `--http-accept-gzip` `--remote-time` `--disable-ipv6` `--interface` | |
| `-Z/--parallel-max` | `--max-concurrent-downloads` | |

### 2. 明确无害 → 忽略并记录 note

`-L`（aria2 总是跟随重定向）、`-N`、`-#` `--progress-bar`、`-f/--fail`（aria2 遇 4xx/5xx 本来就失败）、`--proxytunnel`、`--tcp-nodelay`、`--retry-all-errors`…

### 3. 无法忠实表达 → 回退 curl

这些选项会改变"做什么"而不只是"多快"，一律回退，保证行为不变：

`-X/--request`、`-d/--data*`、`-F/--form`、`-T/--upload-file`、`-a/--append`、`-I/--head`、`-G/--get`、`-i/--include`、`-D/--dump-header`、`-w/--write-out`、`-v/--verbose`、`--trace*`、`-r/--range`、`-z/--time-cond`、`-n/--netrc*`、`-K/--config`、`-M/--manual`、`-j/--junk-session-cookies`、`--http2/--http3`、`--http1.0/1.1`（aria2 无 `--http-version`）、`--max-redirs`（aria2 无重定向上限）、`--tls-max`（aria2 只有 `--min-tls-version`）、`--max-filesize`、`--capath`、`--location-trusted`、`--socks4a/--socks5-hostname`、`--resolve/--connect-to`、`--unix-socket`、`--rate`、`--ciphers`、`--pinnedpubkey`、`--aws-sigv4`、`--netrc`、`--next`、`-o -`（写 stdout）、URL 通配 `{} []`、本地文件路径、`file://`/`scp://`/`imap://` 等 aria2 不支持的 scheme，以及**任何未收录的选项**。

回退前会在 stderr 用暗色打一行提示（仅 TTY 或 `--acurl-debug` / `--acurl-explain`）：

```console
aria2curl: using curl instead of aria2 — request body (-d/--data)
```

### 其他安全网

* **递归保护**：`execv curl` 前设置 `ARIA2CURL_DEPTH`；若解析到的 curl 指向 aria2curl 自身，直接报错而不是无限递归。
* **`~/.curlrc` 策略**：`curlrc_policy` = `warn`（默认，提示该文件在 aria2 路径下不生效）/ `fallback`（存在就一律用 curl）/ `ignore`。
* **`aria2.conf` 隔离**：总是传 `--no-conf=true`，避免用户 `~/.aria2/aria2.conf` 干扰翻译结果。
* **`--stop-with-process`**：默认开启，aria2curl 被强杀时 aria2 一起退出，不留孤儿进程。

---

## 全局配置

配置文件**分三层**：

| 层 | 路径 | 环境变量 |
|---|---|---|
| 系统层 | `/etc/aria2curl/config.toml` | `ARIA2CURL_SYSTEM_CONFIG` |
| 用户层 | `$XDG_CONFIG_HOME/aria2curl/config.toml`（默认 `~/.config/aria2curl/config.toml`） | `ARIA2CURL_CONFIG` |
| 环境/命令行 | `ARIA2CURL_<KEY>`、`--acurl-set k=v`、专用开关 | |

优先级（低 → 高）：**内置默认值 < 系统层 < 用户层 < 环境变量 < `--acurl-set` < 专用命令行开关**。
把某个路径设为空字符串即可禁用该层（例如 `ARIA2CURL_SYSTEM_CONFIG=` 忽略系统层）。`config list` 会标注每个值的来源（`default`/`system`/`file`/`env`/`cli`）。

### 查看

```console
$ aria2curl config list                 # 分组表格，含 aria2 选项、说明、来源
$ aria2curl config list --changed       # 只看与默认值不同的项
$ aria2curl config list --json          # 机器可读（含 type/constraint/source）
$ aria2curl config get split            # 只输出值，方便脚本
$ aria2curl config get split --explain  # 值 + 类型 + 来源 + 默认值 + 说明
$ aria2curl config path                  # 用户层路径
$ aria2curl config path --system         # 系统层路径（/etc/aria2curl/config.toml）
```

### 修改

```console
$ aria2curl config set split 16
$ aria2curl config set auto_resume false max_tries 10      # 一次改多个
$ aria2curl config set download.split=16                   # 支持 key=value 与分段前缀
$ aria2curl config set engine json --dry-run                # 只看会发生什么
$ aria2curl config unset split                              # 恢复默认
$ aria2curl config reset --yes                              # 全部恢复默认
$ aria2curl config edit                                     # $EDITOR 打开并校验
$ sudo aria2curl config set --system split 16               # 写系统层（所有用户的默认值）
```

非法值会被拒绝并给出范围提示：

```console
$ aria2curl config set split 999
aria2curl: split: must be <= 64 (got 999)
```

### 环境变量覆盖（单次运行）

```console
$ ARIA2CURL_SPLIT=32 aria2curl -O https://example.com/big.iso
$ ARIA2CURL_ENGINE=json aria2curl -o f.bin https://example.com/f.bin 2> events.ndjson
```

### 全部配置项

<!-- CONFIG-TABLE:START -->
运行 `aria2curl config list` 查看当前生效值；`aria2curl config list --json` 可拿到类型与约束。
<!-- CONFIG-TABLE:END -->

主要分组：

**download（下载调优，映射到 aria2c 选项）**

| key | 默认 | 说明 |
|---|---|---|
| `split` | `5` | 每服务器分块连接数（1 关闭分块） |
| `max_connection_per_server` | `5` | 单服务器最大连接数 |
| `min_split_size` | `1M` | 小于该值不再分块（aria2 要求 ≥ 1M） |
| `max_concurrent_downloads` | `5` | 同时下载的 URL 数 |
| `file_allocation` | `prealloc` | `none`/`prealloc`/`trunc`/`falloc` |
| `auto_resume` | `true` | **断点续传**：目标文件存在且非空时自动续传 |
| `allow_overwrite` | `true` | 覆盖已存在文件（curl 语义） |
| `auto_file_renaming` | `false` | 关掉 aria2 默认的 `file.1` 重命名 |
| `max_tries` / `retry_wait` | `5` / `2` | 尝试次数 / 间隔 |
| `timeout` / `connect_timeout` | `60` / `60` | 读超时 / 连接超时（秒） |
| `check_certificate` / `ca_certificate` | `true` / 空 | TLS 校验 |
| `user_agent` / `referer` / `headers` | 空 | 默认请求头 |
| `proxy` / `no_proxy` | 空 | 默认代理（curl 未给时生效） |
| `disable_ipv6` / `interface` / `disk_cache` | `false` / 空 / `0` | |
| `remote_time` / `content_disposition` / `conditional_get` / `http_accept_gzip` | `false` | |
| `dir` | 空 | **默认下载目录**：仅作用于"文件名来自 URL"的下载（`-O`/隐式），`-o 相对路径` 仍按 curl 语义相对当前目录 |
| `cookie_file` | 空 | 默认 cookie 文件 |
| `extra_args` | `[]` | 追加任意原始 aria2 参数（兜底后门） |

**engine（引擎选择与回退）**

| key | 默认 | 说明 |
|---|---|---|
| `mode` | `auto` | `auto` 自动 / `aria2` 绝不回退 / `curl` 总是用 curl |
| `aria2_path` / `curl_path` | `aria2c` / `curl` | 二进制路径 |
| `fallback_to_curl` | `true` | 无法翻译时回退 |
| `implicit_output` | `auto` | 没给 `-o`/`-O` 时：TTY 用远端文件名，管道回退 curl |
| `curlrc_policy` | `warn` | 见上文 |
| `honor_proxy_env` | `true` | 翻译代理环境变量 |
| `auto_resume_retry` | `true` | 续传不被支持时自动重下 |
| `rpc_start_timeout` / `poll_interval` | `10.0` / `0.3` | RPC 就绪超时 / 进度刷新间隔 |
| `stop_with_process` | `true` | 父进程死亡时结束 aria2 |
| `keep_aria2_output` | `false` | 关掉仪表盘，直接显示 aria2 原始输出 |
| `console_log_level` | `warn` | aria2 日志级别 |

**display（终端显示）**

| key | 默认 | 说明 |
|---|---|---|
| `engine` | `auto` | `auto`/`rich`/`plain`/`json`/`none` |
| `style` | `bar` | `bar` 或 `compact` |
| `color` / `unicode` | `auto` | `auto`/`always`/`never` |
| `refresh_per_second` | `10` | 刷新率 |
| `show_header` / `show_connections` / `show_speed` / `show_eta` / `show_summary` | `true` | 面板元素开关 |

**notices**

| key | 默认 | 说明 |
|---|---|---|
| `show_fallback_reason` | `true` | TTY 下解释为什么回退 |
| `quiet` | `false` | 静默 aria2curl 自身消息（进度仍受 `-s` 控制） |
| `debug` | `false` | 打印解析与 aria2c 命令细节 |

---

## 终端显示

四种引擎（`display.engine`）：

* **rich**（默认 `auto` 在真终端下选中）：实时仪表盘

```text
aria2curl 2 downloading · 6.21 MiB/s · 128.4 MiB / 512.0 MiB · elapsed 00:21 · ETA 01:02
██████████████████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
ubuntu.iso        ████████████████░░░░  78.5%   5.02 MiB/s  00:18   8
debian.iso        ██████░░░░░░░░░░░░░░  31.2%   1.19 MiB/s  02:54   4
╭──────────── aria2curl 2/2 ok · 512.0 MiB · 5.84 MiB/s · 01:28 ────────────╮
│ ✔ ubuntu.iso  512.0 MiB  /home/me/downloads/ubuntu.iso                    │
╰───────────────────────────────────────────────────────────────────────────╯
```

* **plain**：无第三方依赖，TTY 下单行自刷新 + 每个文件完成一行；stderr 重定向时只在结束时输出一行，脚本友好。
* **json**：stderr 输出 NDJSON（`{"event":"progress",...}` / `{"event":"summary",...}`），方便接入自己的 UI。
* **none**：完全静默（`-s` / `--no-progress-meter` 也会选它）。

`-s`、`--no-progress-meter` 会被识别；`-#` 选择 `bar` 样式。非 TTY 或 `TERM=dumb` 时 `auto` 自动降级为 `plain`，不会往日志里灌 ANSI。

---

## 与 curl 的差异（重要）

透明回退保证了"脚本兼容"，但在**走 aria2 路径**时，有几处刻意的差异：

1. **HTTP 错误码**：curl 默认会把 404 页面保存成文件并返回 0；aria2 遇 4xx/5xx 直接失败（`-f` 是 curl 的行为）。本程序返回 curl 的 22。
   *需要"保存错误页"语义时请用 `--acurl-mode=curl` 或让命令包含无法翻译的选项。*
2. **重定向上限**：aria2 无 `--max-redirs`，给了就回退 curl。
3. **`~/.curlrc`**：aria2 路径不读取（默认 `warn` 提示；`curlrc_policy=fallback` 可改为一律回退）。
4. **`auto_resume` 默认开启**：curl 默认覆盖重下，本程序默认续传。用 `config set auto_resume false` 恢复 curl 语义。
5. **续传粒度 1 MiB**：aria2 以分片（HTTP 默认 1 MiB）为单位续传，小于一个分片或未对齐的残留数据会被重下，文件仍保证正确。
6. **`--max-time`**：由 supervisor 计时（用 `aria2.remove` 中断），退出码 28，与 curl 一致。
7. **`-O url1 url2`**：curl 只对第一个 URL 用 `-O`，第二个写 stdout —— 本程序判定"输出个数与 URL 个数不匹配"并**回退 curl**，行为完全一致（想多文件请用 `--remote-name-all`）。
8. **文件名**：`-o` 目录不存在且未给 `--create-dirs` 时回退（让 curl 报出原生错误）。
9. **User-Agent**：aria2 默认 UA 是 `aria2/x.y.z`，curl 是 `curl/x.y.z`（除非命令或配置给了 `-A`）。需要 curl 的 UA 可 `config set user_agent 'curl/8'` 或用 `-A`。

---

## 退出码

aria2 的退出码会翻译回 curl 的编号，脚本判断 `$?` 依旧适用：

| aria2 | curl | 含义 |
|---|---|---|
| 0 | 0 | 成功 |
| 2 | 28 | 超时 |
| 3 / 4 | 22 | 资源不存在 / HTTP 错误 |
| 5 | 28 | 速度过低 |
| 6 | 7 | 网络问题 |
| 7 | 18 | 下载未完成（含被中断） |
| 8 | 36 | 服务器不支持续传 |
| 9 / 13 / 16 / 17 / 18 | 23 | 磁盘 / 文件写入错误 |
| 19 | 6 | 域名解析失败 |
| 23 | 47 | 重定向过多 |
| 24 | 67 | 认证失败 |
| 32 | 22 | 校验失败 |
| 信号 | 130 / 143 | Ctrl-C / SIGTERM |

---

## 开发与测试

```console
$ python -m pip install -e ".[dev]"
$ python -m pytest -q                        # 全部（无 aria2c 时自动跳过 E2E）
$ python -m pytest tests/test_curlparse.py -q # 只跑解析器
$ python -m pytest -k resume -q
```

测试自带一个**支持 `Range`** 的 HTTP 测试服务器（`tests/conftest.py`），因此续传、分块、404、重定向、Content-Disposition、Basic 认证、限速、超时都能端到端验证；`python -m http.server` 不支持 Range，这也是自建服务器的原因。

E2E 测试需要一个可用的 `aria2c`：

```console
$ export ARIA2CURL_TEST_ARIA2=$(command -v aria2c)   # 或忽略，工具按序查找
```

**没有 root / 没装 aria2 也能测**：

```console
$ sh tools/fetch-aria2.sh        # apt-get download + dpkg-deb -x 到 .aria2root/
$ tools/aria2c --version         # 用自带 LD_LIBRARY_PATH 的启动器
$ ARIA2CURL_TEST_ARIA2=$PWD/tools/aria2c python -m pytest -q
```

自检：

```console
$ aria2curl doctor
  ✔ python                 3.13.5
  ✔ curl                   curl 8.14.1 ...
  ✔ aria2c                 aria2 version 1.37.0
  ✔ rich                   13.9.4
  ✔ config file            /home/me/.config/aria2curl/config.toml (exists)
  ✔ aria2 JSON-RPC         ok (aria2 1.37.0 on port 45871)
  all good
```

`doctor` 会真的起一个 aria2c 实例验证 JSON-RPC 通路。

### CI

GitHub Actions（`.github/workflows/ci.yml`）：

* `test`：Python 3.11/3.12/3.13 矩阵，`apt install aria2 curl`，跑全量 pytest + `aria2curl doctor`；
* `lint`：ruff（正确性规则 E4/E7/E9/F）+ 格式检查，`sh -n` 与 shellcheck 检查安装脚本；
* `installer`：真实执行用户级安装脚本（`--source .`），验证 console script、配置读写、alias 写入与 `--uninstall` 的幂等性；
* `installer-system`：用 sudo 真实安装到 `/usr/local` + `/etc`，验证系统配置层、profile.d、curl shim 的透明性与 `--uninstall`；
* `fallback`：**不装 aria2** 的环境下必须仍然全绿（证明回退路径可用）。

---

## 架构

```text
src/aria2curl/
├── cli.py         # 入口：--acurl-* 解析、子命令分发、决策、execv curl、递归保护
├── curlparse.py   # curl 参数白名单解析器 → CurlCommand / FallbackNeeded
├── aria2args.py   # CurlCommand + Config → aria2c 参数、input-file、目标路径、续传判定
├── runner.py      # 起 aria2c、RPC 轮询、max-time 监督、信号、退出码裁决、重下重试
├── rpc.py         # 极简 aria2 JSON-RPC 客户端（http.client，长连接）
├── display.py     # rich / plain / json / none 四种进度引擎
├── config.py      # 配置注册表：类型、范围、别名、TOML 读写、env/cli 覆盖与来源
├── configcmd.py   # config 子命令实现
├── alias.py       # shell alias 块生成/安装/卸载（幂等）
├── exitcodes.py   # aria2 → curl 退出码映射
└── errors.py      # FallbackNeeded 等异常

install-user.sh      # 用户级安装：私有 venv + 写入你的 shell alias
install-system.sh    # 系统级安装：/usr/local + /etc + profile.d + 可选 curl shim
install.sh           # 用户级入口别名（转发到 install-user.sh）
```

设计要点：

* **解析即决策**：`curlparse` 只做"能不能翻译"，`aria2args` 只做"翻译成什么"，`runner` 只做"跑并汇报"，互不越界，全部可单测（`--acurl-dry-run` 就是这条链路的可视化）。
* **数据化计划**：`build_plan()` 返回纯数据（argv、input-file 文本、目标路径），不产生副作用，因此可在不启动任何进程的情况下断言翻译结果。
* **退出码自算**：不依赖 aria2 进程返回值，而是从 RPC 的每个下载最终 `errorCode` 裁决，`aria2.shutdown` 后依然准确。

---

## FAQ

**Q：为什么 `aria2curl -O a b` 没有加速？**
A：curl 语义下 `-O` 只作用于第一个 URL，第二个会写到 stdout。为保证行为一致，本程序回退 curl。多文件请用 `--remote-name-all` 或每个 URL 配一个 `-o`。

**Q：为什么续传时前面一点点又重新下了？**
A：aria2 以分片（HTTP 默认 1 MiB）为单位续传，未对齐的尾部会重下，文件内容始终正确。

**Q：怎么完全恢复 curl 行为？**
A：三种方式：`aria2curl config set mode curl`、`ARIA2CURL_MODE=curl`、或删掉 alias（`aria2curl alias uninstall`）。

**Q：能配合 `curl | bash` 这类脚本用吗？**
A：可以，但脚本里的 curl 通常不在交互 shell 中，alias 不生效（这正是安全的地方）。若确实要让所有 curl 走加速，可把 `~/.local/bin/curl` 做成包装脚本并置于 PATH 前面——本程序有递归保护，检测到会直接报错而不是死循环。

**Q：aria2 没装会怎样？**
A：自动回退 curl 并在 TTY 下提示一次；`aria2curl doctor` 会告诉你缺什么。

**Q：怎么卸载？**
A：用户级 `sh install-user.sh --uninstall`（等价于 `aria2curl alias uninstall` 加上删除 venv 与软链）；
系统级 `sudo sh install-system.sh --uninstall`（加 `--purge` 连 `/etc/aria2curl/config.toml` 一起删）。

---

## License

MIT
