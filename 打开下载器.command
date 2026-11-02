#!/bin/zsh
set -u
base_dir="${0:A:h}"
cd -- "$base_dir" || exit 1
python_bin="$(command -v python3)"
if [[ -z "$python_bin" ]]; then
  print '未找到 Python 3，请先安装 Python 3.9 或更新版本。'
  read -r '?按回车退出…'
  exit 1
fi
trap 'exit 130' INT TERM
while true; do
  print '\n三大交易所 IPO 文件下载器'
  print '1  查看本地状态（不联网）'
  print '2  检查官网接口（不下载附件）'
  print '3  更新全部文件清单（不下载附件）'
  print '4  开始下载 / 增量更新'
  print '5  开启每日 19:30 自动下载更新'
  print '6  关闭每日自动更新'
  print '0  退出'
  read -r 'choice?请选择：'
  case "$choice" in
    1) "$python_bin" ipo_downloader.py status ;;
    2) "$python_bin" ipo_downloader.py doctor ;;
    3) "$python_bin" ipo_downloader.py scan ;;
    4) "$python_bin" ipo_downloader.py sync --download ;;
    5) "$python_bin" ipo_downloader.py schedule install --download ;;
    6) "$python_bin" ipo_downloader.py schedule remove ;;
    0) exit 0 ;;
    *) print '请输入 0～6。' ;;
  esac
done
