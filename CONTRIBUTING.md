# 贡献说明

## 本地开发

需要 Python 3.9+、curl 和 openssl，支持 macOS/Linux。无需安装第三方 Python 包。

```bash
python3 ipo_downloader.py init
python3 -m unittest discover -s tests -v
```

测试仅使用保留的元数据样本和本机临时 HTTPS 服务，不访问交易所、不下载真实披露附件，也不创建定时任务。

## 修改接口适配

- `ipo_archive/exchanges.py`：项目列表、分页和文件分类。
- `ipo_archive/transport.py`：官方域名限制、Cookie、请求间隔、重试和冷却。
- `ipo_archive/store.py`：SQLite 索引与增量检查。
- `ipo_archive/download.py`：文件校验、续传、网页附件及共享文件复用。
- `ipo_archive/engine.py`：扫描和下载流程。
- `ipo_archive/cli.py`：命令行、配置和定时任务。

接口变更请附带最小化元数据样本，保留字段名称、字段类型和嵌套结构，并补充回归测试。不要提交 Cookie、真实下载附件、本地配置、数据库或日志。

请保留默认限速和暂停逻辑；自动测试不得调用实际官网接口。提交问题时可以提供已移除本地路径及 Cookie 的错误摘要、命令参数、系统和 Python 版本。
