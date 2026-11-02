# 三大交易所 IPO 文件下载器

[![Tests](https://github.com/caihaodong111/ipo-exchange-downloader/actions/workflows/tests.yml/badge.svg)](https://github.com/caihaodong111/ipo-exchange-downloader/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

适用于上交所主板/科创板、深交所主板/创业板、北交所公开发行并上市项目的**官网公开材料**。使用 Python 标准库、SQLite 和系统 curl，无须安装 pip 软件包。运行环境：macOS 或 Linux，Python 3.9 及以上、curl。macOS 的每日定时功能使用 launchd。

默认不会下载披露附件或启用定时任务。支持分类归档、增量更新、断点续传、文件校验和共享公告复用。

## 从哪里使用

先克隆仓库并初始化本地配置：

```bash
git clone https://github.com/caihaodong111/ipo-exchange-downloader.git
cd ipo-exchange-downloader
python3 ipo_downloader.py init
python3 ipo_downloader.py --help
```

macOS 用户初始化后可以双击 `打开下载器.command`，查看本地状态、检查接口、预览清单，或自行选择开始下载、开启每日更新。Linux 用户使用终端命令。

运行程序不需要第三方 Python 包。运行开发测试还需要 `openssl` 命令，用于生成仅供本机测试使用的临时证书。

不带子命令只显示帮助，不会联网。**只有 `sync --download`、`watch --download` 会下载正式材料。** `doctor`、`scan`、`status`、`verify`、`schedule template` 都不会下载披露附件。

## 保存目录

```text
资料库/
├── 上海证券交易所/
├── 深圳证券交易所/
└── 北京证券交易所/
    └── 烟台九目化学股份有限公司/
        ├── 信息披露/
        ├── 问询与回复/
        ├── 上市委会议公告与结果公告/
        ├── 终止审核/
        ├── 证监会注册结果/
        ├── 其他文件/
        └── 项目档案_711.json
```

目录在正式下载时创建。信息披露包含招股书、审计报告、法律意见书、保荐书等官网列出的文件。申报稿、上会稿、注册稿和其他修订版本全部保留。未知的新分类进入“其他文件”。上市委会议公告与会议结果公告放在同一个分类中。

文件名为 `披露日期_官方文件名称__项目编号_版本标识.pdf`，同名文件和不同版本不会互相覆盖。文件旁的 `.receipt.json` 记录来源、版本、SHA256 和大小，用于异常退出后的恢复。公司改名后保留原目录，新的全称写入项目档案，避免旧材料散落到两个目录。

运行状态单独保存在 `运行状态/`：

| 文件 | 用途 |
| --- | --- |
| `index.sqlite3` | 项目、文件版本、下载状态、重试时间与校验值 |
| `文件清单.csv` | 用 Excel 查看文件分类、链接、成功/待下载/失败状态 |
| `最近运行.json` | 最近一次扫描或更新的结果 |
| `接口检查.json` | 最近一次接口检查，不涉及附件下载 |
| `运行日志.log` | 自动轮转的详细运行日志 |
| `cookies.txt` | 官网正常跳转产生的临时 Cookie，仅本机保存 |

不要只删除数据库、保留资料库；两者应一起备份。若仅丢失个别文件，下次同步会补下载；`verify` 可进一步检查完整 SHA256。

## 先检查、预览，不下载

```bash
# 检查三所各一页项目列表及一个公司的详情
python3 ipo_downloader.py doctor

# 示例：检查北交所 711 号项目
python3 ipo_downloader.py doctor --url 'https://www.bse.cn/audit/project_news_detail.html?id=711'

# 将这个公司的文件名称、日期、链接写入清单；不请求 PDF
python3 ipo_downloader.py scan --url 'https://www.bse.cn/audit/project_news_detail.html?id=711'

# 只预览每所前 3 个项目
python3 ipo_downloader.py scan --max-projects 3

# 预览全部项目（首次涉及数千家公司，耗时较长）
python3 ipo_downloader.py scan

# 只读本地状态，不联网
python3 ipo_downloader.py status
```

扫描后的文件状态是 `pending`，即待下载；不会因为执行过预览就跳过未来的下载。

## 以后开始下载

**以下命令会实际下载披露附件。**

```bash
# 先试一个公司，最多尝试下载 5 个文件
python3 ipo_downloader.py sync --download \
  --url 'https://www.bse.cn/audit/project_news_detail.html?id=711' --max-downloads 5

# 下载三个交易所的所有公开 IPO 项目材料，之后同一命令用于增量更新
python3 ipo_downloader.py sync --download

# 只更新北交所
python3 ipo_downloader.py sync --download --exchange bse
```

`--exchange` 可重复填写，取值为 `sse`、`szse`、`bse`。`--max-projects` 限制**每所本轮检查的项目数**；`--max-downloads` 限制本轮尝试下载的文件数。后者不是项目范围过滤器：未指定 `--url` 时，也会处理此前索引中属于所选交易所的其他待下载文件。

按 Ctrl+C 可停止。数据库逐公司、逐文件提交，下次使用同样命令继续。下载临时文件使用 `.part` 后缀，只有校验完成才会改为正式文件名。

## 以后自动更新

### 方式一：macOS 每日更新

定时任务默认**未注册**。执行下面的命令才会启用：

```bash
# 每日本机时间 19:30 进行三所增量更新；安装时不会立即执行下载
python3 ipo_downloader.py schedule install --download --hour 19 --minute 30

# 查看状态
python3 ipo_downloader.py schedule status

# 停止每日自动更新
python3 ipo_downloader.py schedule remove
```

定时任务使用当前配置和索引，同样遵守限速、重试和排他锁。使用本机时区；本机设置为上海时区时即北京时间。需要机器开机、用户已登录并能联网；如果系统阻止后台进程读取桌面目录，任务错误会出现在“定时任务错误.log”中，可将整个程序移到允许访问的位置后重新生成配置、安装任务。更换目录或 Python 后也需重新安装。

`schedule template` 只生成 plist 模板；没有调用 `launchctl bootstrap`，不会启用自动下载。

### 方式二：常驻监控

```bash
# 立即执行一轮，再每 24 小时更新；关闭终端或 Ctrl+C 后停止
python3 ipo_downloader.py watch --download --interval-hours 24
```

两种方式通常选一种。程序有系统文件锁，即使误启动两次也不会并发重复采集。

## 更新与请求策略

1. 每轮分页检查各所项目列表，不依赖只看首页，也不因遇到一个旧日期就停止。
2. 新公司、更新时间/审核状态发生变化的项目获取详情；其他旧公司跳过详情请求。
3. 对前一次检查前后两天内有更新的项目补充复查，覆盖列表只精确到日期时的同日追加披露。
4. 所有旧项目默认每 7 天至少复查一次，补查官网未同步修改项目日期的材料。可用 `--force-check` 强制复查详情。
5. 文件按官方地址和版本标识去重。已完成版本不会重复下载；同一份会议公告被多个公司引用时，从已校验的本地文件复制，不重复请求官网。
6. 当前接口能够提供更新时间、版本号或修订标识时，同地址更新会生成新版本。如果官网在完全不改变任何元数据的情况下替换原文件，元数据增量模式无法立即识别，需人工核查。

默认全程串行，每次请求至少间隔 2.5 秒，再增加 0～0.75 秒随机间隔。网络临时失败最多尝试 3 次；连续失败暂停该交易所。403/429、Retry-After 和连续跳转均触发暂停，冷却状态跨进程保存；不破解验证码、不绕过访问限制。

只有服务器提供强 ETag 或 Last-Modified 且支持 Range 时续传，同时校验本地断点的大小与 SHA256；否则安全重下当前文件。服务器忽略 Range 时重新写入，避免重复拼接。校验文件签名、HTTP 长度及 SHA256，HTML 拦截页不会被当作 PDF。正式的 HTML 公告页则保留原网页，并提取其中指向支持的官方域名的附件，继续放到同一分类。

单个文件失败进入重试队列，不会让其他交易所停止。文件清单中的失败不会显示为成功。进程退出码：0 表示本轮正常结束，2 表示有未完成或错误，130 表示手动停止；带数量限制的正常运行不代表已经完成全量归档。

## 配置和维护

`python3 ipo_downloader.py init` 会创建本地 `config.json`，默认值也可参照仓库中的 `config.example.json`。修改本地配置可以调整资料目录、交易所范围、间隔、单文件大小上限、复查周期。相对目录以配置文件所在目录为基准。修改资料目录前，应把原资料库一起移动，否则会按文件缺失处理。

本地配置、Cookie、运行数据库、日志和下载资料已列入 `.gitignore`，不会随源码提交。保留这些本地文件即可继续之前的归档进度。

```bash
# 完整核验本地文件，不联网；发现损坏仅标回待下载
python3 ipo_downloader.py verify

# 重置文件/公司失败项的等待时间，不下载；官网限流冷却仍然有效
python3 ipo_downloader.py retry-failed --exchange bse

# 运行离线测试；测试下载只发生在本机临时 HTTPS 服务
python3 -m unittest discover -s tests -v
```

官网可能维护、限流或调整接口，无法承诺网络永远不出错。程序会明确记录这类情况、保留有效进度，并在后续运行重试；接口结构不符合预期时停止对应项目或交易所，不把缺页当成抓取完成。

## 接口依据

2026-11-02 核查的官网页面与公开接口：

- 上交所项目页：<https://www.sse.com.cn/listing/renewal/ipo/>；列表 `commonSoaQuery.do?sqlId=SH_XM_LB`，文件 `sqlId=GP_COMMON_FILE_SEARCH`。分类按官网 `fileTypeMap`，覆盖信息披露、问询、会议、终止和注册结果。
- 深交所项目页：<https://www.szse.cn/listing/projectdynamic/ipo/index.html>；列表 `/api/ras/projectrends/query`，详情 `/api/ras/projectrends/details`。递归读取附件子列表。
- 北交所项目页：<https://www.bse.cn/audit/project_news_detail.html?id=711>；列表 `/projectNewsController/infoResult.do`，详情 `/projectNewsController/infoDetailResult.do`。分类采用 `xxgkInfo`、`wxhfhInfo`、`hyggjgInfo`、`zzscInfo`、`hztzInfo`。

采集范围是上述官网 IPO 项目页面当前公开、能通过接口获取的材料；不含再融资、并购重组，也不承诺恢复官网已经移除的历史文件。

## 开发与许可证

欢迎提交接口适配修复与测试用例，详见 [贡献说明](CONTRIBUTING.md)。GitHub Actions 在 macOS、Linux 的 Python 3.9 和 3.13 环境执行离线测试；不会访问交易所或下载真实披露附件。

项目源码采用 [MIT 许可证](LICENSE)。仓库不包含实际披露附件；使用中下载的第三方文件，其权利归属不因源码许可证而改变。
