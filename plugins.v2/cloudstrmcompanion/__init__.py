import json
import os
import re
import shutil
import threading
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, List, Dict, Tuple, Optional

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.core.metainfo import MetaInfoPath
from app.helper.mediaserver import MediaServerHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import MediaInfo
from app.schemas.types import EventType, NotificationType, MediaType
from app.utils.http import RequestUtils
from app.utils.string import StringUtils

lock = threading.Lock()


class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类
    """

    def __init__(self, monpath: str, sync: Any, **kwargs):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._monpath = monpath
        self._sync = sync

    def on_created(self, event):
        self._sync.event_handler(
            event=event,
            mon_path=self._monpath,
            text="创建",
            event_path=event.src_path,
        )

    def on_moved(self, event):
        self._sync.event_handler(
            event=event,
            mon_path=self._monpath,
            text="移动",
            event_path=event.dest_path,
        )


class CloudStrmCompanion(_PluginBase):

    # 插件名称
    plugin_name = "云盘Strm助手"
    # 插件描述
    plugin_desc = "实时监控云盘挂载目录文件变化，生成strm文件，支持本地路径strm生成。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/thsrite/MoviePilot-Plugins/main/icons/cloudstrm.png"
    # 插件版本
    plugin_version = "1.4.0"
    # 插件作者
    plugin_author = "thsrite"
    # 作者主页
    author_url = "https://github.com/thsrite"
    # 插件配置项ID前缀
    plugin_config_prefix = "cloudstrmcompanion_"
    # 加载顺序
    plugin_order = 27
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _monitor_confs = None
    _cover = False
    _monitor = False
    _onlyonce = False
    _copy_files = False
    _copy_subtitles = False
    _url = None
    _notify = False
    _refresh_emby = False
    _uriencode = False
    _strm_dir_conf = {}
    _cloud_dir_conf = {}
    _category_conf = {}
    _format_conf = {}
    _cloud_files = []
    _observer = []
    _medias = {}
    _rmt_mediaext = None
    _other_mediaext = None
    _interval = 10
    _mediaservers = None
    mediaserver_helper = None
    _emby_paths = {}
    _path_replacements = {}
    _cloud_files_json = "cloud_files.json"
    _headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Cookie": "",
    }
    _scheduler = None
    _event = threading.Event()

    # ========== 新增：本地路径Strm生成相关属性 ==========
    _local_strm_enabled = False
    _local_strm_confs = None
    _local_strm_monitor = False
    _local_strm_dir_conf = {}  # 源路径 → strm输出路径 映射
    _local_strm_cover = False
    # ========== 新增结束 ==========

    def init_plugin(self, config: dict = None):
        # 清空配置
        self._strm_dir_conf = {}
        self._cloud_dir_conf = {}
        self._format_conf = {}
        self._category_conf = {}
        self._path_replacements = {}
        self._cloud_files_json = os.path.join(self.get_data_path(), self._cloud_files_json)
        self.mediaserver_helper = MediaServerHelper()

        # ========== 新增：清空本地路径strm配置 ==========
        self._local_strm_dir_conf = {}
        # ========== 新增结束 ==========

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._interval = config.get("interval") or 10
            self._monitor = config.get("monitor")
            self._cover = config.get("cover")
            self._copy_files = config.get("copy_files")
            self._copy_subtitles = config.get("copy_subtitles")
            self._refresh_emby = config.get("refresh_emby")
            self._notify = config.get("notify")
            self._uriencode = config.get("uriencode")
            self._monitor_confs = config.get("monitor_confs")
            self._url = config.get("url")
            self._mediaservers = config.get("mediaservers") or []
            self._other_mediaext = config.get("other_mediaext")

            # ========== 新增：读取本地路径strm配置 ==========
            self._local_strm_enabled = config.get("local_strm_enabled", False)
            self._local_strm_monitor = config.get("local_strm_monitor", False)
            self._local_strm_confs = config.get("local_strm_confs")
            self._local_strm_cover = config.get("local_strm_cover", False)
            # ========== 新增结束 ==========

            # 读取路径替换规则
            if config.get("path_replacements"):
                for replacement in str(config.get("path_replacements")).split("\n"):
                    if replacement and ":" in replacement:
                        source, target = replacement.split(":", 1)
                        self._path_replacements[source.strip()] = target.strip()

            self._rmt_mediaext = config.get(
                "rmt_mediaext") or ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"

            if config.get("emby_path"):
                for path in str(config.get("emby_path")).split(","):
                    if ":" in path:
                        self._emby_paths[path.split(":")[0]] = path.split(":")[1]

            # 解析云盘目录配置
            if self._monitor_confs:
                monitor_confs = str(self._monitor_confs).split("\n")
                for monitor_conf in monitor_confs:
                    if not monitor_conf:
                        continue
                    # 格式: 本地挂载路径#strm生成路径#云盘路径#格式化字符串
                    # 可选: 本地挂载路径#strm生成路径#云盘路径#格式化字符串$手工执行  ($表示不自动监控)
                    # 可选: 本地挂载路径#strm生成路径#云盘路径#格式化字符串@动漫  (@表示分类标签)
                    if not monitor_conf.startswith("#") and "#" in monitor_conf:
                        confs = monitor_conf.split("#")
                        if len(confs) >= 4:
                            mon_path = confs[0].strip()
                            strm_path = confs[1].strip()
                            cloud_path = confs[2].strip()
                            format_str = confs[3].strip()

                            # 处理手工执行标记 $
                            category = None
                            manual_only = False
                            if "$" in format_str:
                                format_str = format_str.replace("$", "")
                                manual_only = True
                            if "@" in format_str:
                                parts = format_str.split("@")
                                format_str = parts[0]
                                category = parts[1] if len(parts) > 1 else None

                            if manual_only:
                                logger.info(f"目录配置 {mon_path} 设置为仅手工执行，跳过自动监控")
                            else:
                                self._strm_dir_conf[mon_path] = strm_path
                                self._cloud_dir_conf[mon_path] = cloud_path
                                self._format_conf[mon_path] = format_str
                                if category:
                                    self._category_conf[mon_path] = category
                                logger.info(f"加载云盘目录配置: {mon_path} -> {strm_path} (格式: {format_str})")

            # ========== 新增：解析本地路径strm配置 ==========
            # 格式: 本地媒体路径#strm生成路径
            # 可选: 本地媒体路径#strm生成路径$手工执行
            # 可选: 本地媒体路径#strm生成路径@分类标签
            if self._local_strm_enabled and self._local_strm_confs:
                local_confs = str(self._local_strm_confs).split("\n")
                for local_conf in local_confs:
                    if not local_conf:
                        continue
                    if not local_conf.startswith("#") and "#" in local_conf:
                        confs = local_conf.split("#")
                        if len(confs) >= 2:
                            source_path = confs[0].strip()
                            strm_output_path = confs[1].strip()

                            manual_only = False
                            category = None

                            # 处理手工执行标记 $
                            if "$" in strm_output_path:
                                strm_output_path = strm_output_path.replace("$", "")
                                manual_only = True
                            # 处理分类标签 @
                            if "@" in strm_output_path:
                                parts = strm_output_path.split("@")
                                strm_output_path = parts[0]
                                category = parts[1] if len(parts) > 1 else None

                            if manual_only:
                                logger.info(f"本地路径配置 {source_path} 设置为仅手工执行，跳过自动监控")
                            else:
                                # 本地路径模式：cloud_dir存空，format固定为{local_file}
                                self._strm_dir_conf[source_path] = strm_output_path
                                self._cloud_dir_conf[source_path] = source_path  # cloud_dir 在本地模式下不使用
                                self._format_conf[source_path] = "{local_file}"
                                self._local_strm_dir_conf[source_path] = strm_output_path
                                if category:
                                    self._category_conf[source_path] = category
                                logger.info(f"加载本地路径strm配置: {source_path} -> {strm_output_path} (本地路径模式)")
            # ========== 新增结束 ==========

            # 启动定时器
            if self._enabled:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                # 全量扫描
                self._scheduler.add_job(
                    self.scan,
                    "cron",
                    hour=3,
                    minute=0,
                    second=0,
                    id="cloudstrm_full_scan"
                )
                # 发送消息
                self._scheduler.add_job(
                    self.send_msg,
                    "interval",
                    minutes=int(self._interval),
                    id="cloudstrm_send_msg"
                )
                self._scheduler.start()
                logger.info("云盘Strm助手定时任务已启动")

                # 启动目录监控
                if self._monitor:
                    # 云盘目录监控
                    for mon_path in self._strm_dir_conf.keys():
                        if self._local_strm_enabled and mon_path in self._local_strm_dir_conf and not self._local_strm_monitor:
                            # 本地路径strm未开启监控，跳过
                            continue
                        if Path(mon_path).exists():
                            observer = PollingObserver(timeout=10)
                            observer.schedule(
                                FileMonitorHandler(mon_path, self),
                                path=mon_path,
                                recursive=True
                            )
                            observer.start()
                            self._observer.append(observer)
                            logger.info(f"已启动目录监控: {mon_path}")
                        else:
                            logger.warning(f"监控目录不存在，跳过: {mon_path}")

                # 立即运行一次
                if self._onlyonce:
                    self._onlyonce = False
                    self.__update_config()
                    threading.Thread(target=self.scan).start()

    def scan(self):
        """
        全量扫描生成strm文件
        """
        logger.info("开始全量扫描生成strm文件...")
        for mon_path in self._strm_dir_conf.keys():
            if not Path(mon_path).exists():
                logger.warning(f"源目录不存在，跳过: {mon_path}")
                continue

            # 判断是否为本地路径模式
            is_local = self._local_strm_enabled and mon_path in self._local_strm_dir_conf

            cloud_dir = self._cloud_dir_conf.get(mon_path)
            strm_dir = self._strm_dir_conf.get(mon_path)
            format_str = self._format_conf.get(mon_path)

            if is_local:
                logger.info(f"全量扫描本地路径: {mon_path} -> {strm_dir} (本地路径模式)")
            else:
                logger.info(f"全量扫描云盘路径: {mon_path} -> {strm_dir}")

            for root, dirs, files in os.walk(mon_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    try:
                        self.__handle_file(
                            event_path=str(file_path),
                            mon_path=mon_path,
                            cloud_dir=cloud_dir,
                            strm_dir=strm_dir,
                            format_str=format_str,
                        )
                    except Exception as e:
                        logger.error(f"处理文件失败 {file_path}: {e}")

        logger.info("全量扫描完成")

    def strm_one(self, event: Event = None):
        """
        处理单个文件（事件触发）
        """
        if not event:
            return
        event_data = event.event_data or {}
        file_path = event_data.get("path")
        if not file_path:
            logger.warning("strm_one: 未提供文件路径")
            return

        # 遍历所有监控目录，找到匹配的
        for mon_path in self._strm_dir_conf.keys():
            if str(file_path).startswith(str(mon_path)):
                cloud_dir = self._cloud_dir_conf.get(mon_path)
                strm_dir = self._strm_dir_conf.get(mon_path)
                format_str = self._format_conf.get(mon_path)
                self.__handle_file(
                    event_path=str(file_path),
                    mon_path=mon_path,
                    cloud_dir=cloud_dir,
                    strm_dir=strm_dir,
                    format_str=format_str,
                )
                break

    def event_handler(self, event, mon_path: str, text: str, event_path: str):
        """
        处理文件变化事件
        """
        logger.info(f"检测到文件{text}: {event_path}")

        if self._copy_files or self._copy_subtitles:
            # 检查是否为需要处理的媒体文件
            if Path(event_path).suffix.lower() in [ext.strip() for ext in self._rmt_mediaext.split(",")]:
                cloud_dir = self._cloud_dir_conf.get(mon_path)
                strm_dir = self._strm_dir_conf.get(mon_path)
                format_str = self._format_conf.get(mon_path)
                self.__handle_file(
                    event_path=event_path,
                    mon_path=mon_path,
                    cloud_dir=cloud_dir,
                    strm_dir=strm_dir,
                    format_str=format_str,
                )
            elif self._copy_files or (self._copy_subtitles and Path(event_path).suffix.lower() in [".ass", ".ssa", ".srt", ".sub", ".sup", ".vtt"]):
                # 非媒体文件复制
                target_file = str(event_path).replace(mon_path, self._strm_dir_conf.get(mon_path))
                self.__handle_other_files(event_path=event_path, target_file=target_file)
        else:
            cloud_dir = self._cloud_dir_conf.get(mon_path)
            strm_dir = self._strm_dir_conf.get(mon_path)
            format_str = self._format_conf.get(mon_path)
            self.__handle_file(
                event_path=event_path,
                mon_path=mon_path,
                cloud_dir=cloud_dir,
                strm_dir=strm_dir,
                format_str=format_str,
            )

    def __handle_file(self, event_path: str, mon_path: str, cloud_dir: str = None,
                      strm_dir: str = None, format_str: str = None):
        """
        同步一个文件，生成strm

        当format_str包含{local_file}时，生成的是本地路径strm文件
        当format_str包含{cloud_file}时，生成的是云盘路径strm文件
        """
        try:
            if not Path(event_path).exists():
                return
            # 全程加锁
            with lock:
                # 使用传入的参数或从配置获取
                if cloud_dir is None:
                    cloud_dir = self._cloud_dir_conf.get(mon_path)
                if strm_dir is None:
                    strm_dir = self._strm_dir_conf.get(mon_path)
                if format_str is None:
                    format_str = self._format_conf.get(mon_path)

                # 本地strm路径
                target_file = str(event_path).replace(mon_path, strm_dir)
                # 云盘文件路径
                cloud_file = str(event_path).replace(mon_path, cloud_dir) if cloud_dir else event_path

                # 只处理媒体文件
                if Path(event_path).suffix.lower() in [ext.strip() for ext in
                                                       self._rmt_mediaext.split(",")]:
                    # 生成strm文件内容
                    strm_content = self.__format_content(
                        format_str=format_str,
                        local_file=event_path,
                        cloud_file=str(cloud_file),
                        uriencode=self._uriencode,
                    )

                    if strm_content is None:
                        logger.error(f"格式化strm内容失败: format_str={format_str}")
                        return

                    # 生成strm文件
                    self.__create_strm_file(
                        strm_file=target_file,
                        strm_content=strm_content,
                    )

                    # nfo、jpg等同名文件
                    pattern = Path(event_path).stem.replace('[', '?').replace(']', '?')
                    files = list(Path(event_path).parent.glob(f"{pattern}.*"))
                    logger.debug(f"筛选到同名文件 {pattern}: {files}")
                    for file in files:
                        if str(file) != str(event_path):
                            target_f = str(file).replace(mon_path, strm_dir)
                            self.__handle_other_files(event_path=str(file), target_file=target_f)

                    # thumb图片
                    thumb_file = Path(event_path).parent / (Path(event_path).stem + "-thumb.jpg")
                    if thumb_file.exists():
                        target_f = str(thumb_file).replace(mon_path, strm_dir)
                        self.__handle_other_files(event_path=str(thumb_file), target_file=target_f)
                else:
                    if self._copy_files:
                        self.__handle_other_files(event_path=event_path, target_file=target_file)
        except Exception as e:
            logger.error("目录监控发生错误：%s - %s" % (str(e), traceback.format_exc()))

    def __handle_other_files(self, event_path: str, target_file: str):
        """
        处理非媒体文件（复制nfo、jpg等）
        """
        try:
            if not Path(event_path).exists():
                return
            target_dir = Path(target_file).parent
            if not target_dir.exists():
                os.makedirs(str(target_dir))
                logger.info(f"创建目标文件夹 {target_dir}")

            target_path = os.path.join(str(target_dir), Path(event_path).name)
            if Path(target_path).exists() and not self._cover:
                logger.debug(f"目标文件已存在: {target_path}")
                return

            shutil.copy2(event_path, target_path)
            logger.info(f"复制非媒体文件: {event_path} -> {target_path}")
        except Exception as e:
            logger.error(f"复制非媒体文件失败 {event_path}: {e}")

    @staticmethod
    def __format_content(format_str: str, local_file: str, cloud_file: str, uriencode: bool):
        """
        格式化strm内容

        {local_file} → 生成本地路径strm
        {cloud_file} → 生成云盘路径strm
        """
        if "{local_file}" in format_str:
            # 本地路径模式：直接使用本地文件路径
            local_path = str(local_file).replace("\\", "/")
            return format_str.replace("{local_file}", local_path)
        elif "{cloud_file}" in format_str:
            if uriencode:
                # 对盘符之后的所有内容进行url转码
                cloud_file = urllib.parse.quote(cloud_file, safe='')
            else:
                # 替换路径中的\为/
                cloud_file = cloud_file.replace("\\", "/")
            return format_str.replace("{cloud_file}", cloud_file)
        else:
            # 纯文本格式，直接返回
            return format_str if format_str else None

    def __create_strm_file(self, strm_file: str, strm_content: str):
        """
        生成strm文件
        """
        try:
            # 确保目标文件夹存在
            if not Path(strm_file).parent.exists():
                logger.info(f"创建目标文件夹 {Path(strm_file).parent}")
                os.makedirs(Path(strm_file).parent)

            # 构造.strm文件路径
            strm_file = os.path.join(
                Path(strm_file).parent,
                f"{os.path.splitext(Path(strm_file).name)[0]}.strm"
            )

            # 检查是否已存在
            if Path(strm_file).exists() and not self._cover:
                logger.info(f"目标文件 {strm_file} 已存在")
                return

            # 应用自定义路径替换规则
            for source, target in self._path_replacements.items():
                if source in strm_content:
                    strm_content = strm_content.replace(source, target)
                    logger.debug(f"应用路径替换规则: {source} -> {target}")

            # 写入.strm文件
            with open(strm_file, 'w', encoding='utf-8') as f:
                f.write(strm_content)

            logger.info(f"创建strm文件成功 {strm_file} -> {strm_content}")

            # 任务推送url
            if self._url and Path(strm_content).suffix in settings.RMT_MEDIAEXT:
                RequestUtils(content_type="application/json").post(
                    url=self._url,
                    json={"path": str(strm_content), "type": "add"},
                )

            # 发送消息汇总
            if self._notify and Path(strm_content).suffix in settings.RMT_MEDIAEXT:
                file_meta = MetaInfoPath(Path(strm_file))

                pattern = r'tmdbid=(\d+)'
                match = re.search(pattern, strm_file)
                if match:
                    tmdbid = match.group(1)
                    file_meta.tmdbid = tmdbid

                key = f"{file_meta.cn_name} ({file_meta.year}){f' {file_meta.season}' if file_meta.season else ''}"
                media_list = self._medias.get(key) or {}
                if media_list:
                    episodes = media_list.get("episodes") or []
                    if file_meta.begin_episode:
                        if episodes:
                            if int(file_meta.begin_episode) not in episodes:
                                episodes.append(int(file_meta.begin_episode))
                        else:
                            episodes = [int(file_meta.begin_episode)]
                    media_list = {
                        "episodes": episodes,
                        "file_meta": file_meta,
                        "type": "tv" if file_meta.season else "movie",
                        "time": datetime.now()
                    }
                else:
                    media_list = {
                        "episodes": [int(file_meta.begin_episode)] if file_meta.begin_episode else [],
                        "file_meta": file_meta,
                        "type": "tv" if file_meta.season else "movie",
                        "time": datetime.now()
                    }
                self._medias[key] = media_list

            # 通知emby刷新
            if self._refresh_emby and self._mediaservers:
                time.sleep(0.1)
                self.__refresh_emby_file(strm_file)
            return True
        except Exception as e:
            logger.error(f"创建strm文件失败 {strm_file} -> {str(e)}")
        return False

    def __refresh_emby_file(self, strm_file: str):
        """
        通知emby刷新文件
        """
        try:
            if not self._mediaservers:
                return
            for mediaserver in self._mediaservers:
                emby_path = self.__get_path(self._emby_paths, strm_file)
                self.mediaserver_helper.get_service(mediaserver).refresh_library_by_file(
                    path=str(emby_path)
                )
                logger.info(f"已通知Emby刷新文件: {emby_path}")
        except Exception as e:
            logger.error(f"通知Emby刷新失败: {e}")

    def __get_path(self, paths, file_path: str):
        """
        路径转换
        """
        if paths and paths.keys():
            for library_path in paths.keys():
                if str(file_path).startswith(str(library_path)):
                    # 替换路径
                    return str(file_path).replace(
                        str(library_path),
                        str(paths.get(str(library_path)))
                    )
        # 未匹配到路径，返回原路径
        return file_path

    def send_msg(self):
        """
        定时检查是否有媒体处理完，发送统一消息
        """
        if not self._medias:
            return
        if not self._notify:
            return

        medias = self._medias.copy()
        self._medias = {}

        for key, media_info in medias.items():
            try:
                file_meta = media_info.get("file_meta")
                episodes = media_info.get("episodes")
                media_type = media_info.get("type")

                if media_type == "tv":
                    episodes_str = " ".join([f"第{e}集" for e in sorted(episodes)])
                    msg_title = f"{key} {episodes_str}"
                else:
                    msg_title = key

                self.send_transfer_message(
                    msg_title=msg_title,
                    file_count=len(episodes) if episodes else 1,
                    image=None,
                )
            except Exception as e:
                logger.error(f"发送消息失败: {e}")

    def send_transfer_message(self, msg_title, file_count, image):
        """
        发送消息
        """
        try:
            eventmanager.send_event(
                EventType.NoticeMessage,
                {
                    "title": f"云盘Strm助手 - 新增{file_count}个文件",
                    "text": msg_title,
                    "image": image,
                    "type": NotificationType.Plugin,
                },
            )
        except Exception as e:
            logger.error(f"发送消息失败: {e}")

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "monitor": self._monitor,
            "cover": self._cover,
            "onlyonce": self._onlyonce,
            "copy_files": self._copy_files,
            "copy_subtitles": self._copy_subtitles,
            "url": self._url,
            "notify": self._notify,
            "refresh_emby": self._refresh_emby,
            "uriencode": self._uriencode,
            "monitor_confs": self._monitor_confs,
            "rmt_mediaext": self._rmt_mediaext,
            "other_mediaext": self._other_mediaext,
            "interval": self._interval,
            "mediaservers": self._mediaservers,
            "path_replacements": "\n".join(
                [f"{k}:{v}" for k, v in self._path_replacements.items()]
            ),
            # ========== 新增：本地路径strm配置持久化 ==========
            "local_strm_enabled": self._local_strm_enabled,
            "local_strm_monitor": self._local_strm_monitor,
            "local_strm_confs": self._local_strm_confs,
            "local_strm_cover": self._local_strm_cover,
            # ========== 新增结束 ==========
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/cloudstrm_scan",
                "event": EventType.PluginAction,
                "desc": "云盘Strm助手-全量扫描",
                "category": "云盘Strm助手",
                "data": {
                    "action": "cloudstrm_scan"
                }
            },
            {
                "cmd": "/cloudstrm_file",
                "event": EventType.PluginAction,
                "desc": "云盘Strm助手-处理单个文件",
                "category": "云盘Strm助手",
                "data": {
                    "action": "cloudstrm_file"
                }
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "event",
                "event_type": EventType.PluginAction,
                "handler": self.strm_one,
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'warning',
                                            'variant': 'tonal',
                                            'text': '云盘实时监控任何问题不予处理，请自行消化。本地路径Strm生成仅生成包含本地文件路径的strm文件。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ========== 基础开关行 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'monitor',
                                            'label': '实时监控',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'copy_files',
                                            'label': '复制非媒体文件',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ========== 功能开关行 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'cover',
                                            'label': '覆盖已存在文件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'uriencode',
                                            'label': 'url编码',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    # ========== 更多开关行 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'refresh_emby',
                                            'label': '刷新媒体库（Emby）',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'copy_subtitles',
                                            'label': '复制字幕文件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    # ========== 消息延迟 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'interval',
                                            'label': '消息延迟',
                                            'placeholder': '10'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ========== 云盘目录配置 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'monitor_confs',
                                            'label': '云盘目录配置',
                                            'rows': 5,
                                            'placeholder': 'MoviePilot中云盘挂载本地的路径#MoviePilot中strm生成路径#alist/cd2上115路径#strm格式化'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ========== 云盘配置说明 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': 'MoviePilot中云盘挂载本地的路径：/mnt/media/series/国产剧/雪迷宫 (2024)；'
                                                    'MoviePilot中strm生成路径：/mnt/library/series/国产剧/雪迷宫 (2024)；'
                                                    '云盘路径：/cloud/media/series/国产剧/雪迷宫 (2024)；'
                                                    '则目录配置为：/mnt/media#/mnt/library#/cloud/media#{local_file}'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ========== strm格式化说明 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': 'strm格式化方式，自行把()替换为alist/cd2上路径：'
                                                    '1.本地源文件路径：{local_file}。'
                                                    '2.alist路径：http://192.168.31.103:5244/d/115{cloud_file}。'
                                                    '3.cd2路径：http://192.168.31.103:19798/static/http/192.168.31.103:19798/False/115{cloud_file}。'
                                                    '4.其他api路径：http://192.168.31.103:2001/{cloud_file}'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ========== 新增：本地路径Strm配置分隔线 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VDivider',
                                        'props': {}
                                    }
                                ]
                            }
                        ]
                    },
                    # ========== 新增：本地路径Strm标题 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'success',
                                            'variant': 'tonal',
                                            'text': '【本地路径Strm生成】将本地媒体文件生成包含本地文件路径的strm文件。适用于不依赖云盘的纯本地媒体库场景。'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ========== 新增：本地路径Strm开关行 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'local_strm_enabled',
                                            'label': '启用本地路径Strm生成',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'local_strm_monitor',
                                            'label': '本地路径实时监控',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'local_strm_cover',
                                            'label': '本地strm覆盖已存在',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    # ========== 新增：本地路径Strm目录配置 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'local_strm_confs',
                                            'label': '本地路径配置',
                                            'rows': 5,
                                            'placeholder': '本地媒体源路径#strm生成路径\n/mnt/nas/media/series#/mnt/nas/library/series\n/mnt/nas/media/movies#/mnt/nas/library/movies'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ========== 新增：本地路径配置说明 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '本地路径Strm配置格式（每行一个目录对）：\n'
                                                    '本地媒体源路径#strm生成路径\n\n'
                                                    '示例：/mnt/nas/media/series#/mnt/nas/library/series\n'
                                                    'strm文件内容将自动使用本地文件路径，如：/mnt/nas/media/series/国产剧/雪迷宫/Season 1/雪迷宫 S01E01.mp4\n\n'
                                                    '可选标记：\n'
                                                    '  $ → 仅手工执行（不自动监控）\n'
                                                    '  @ → 添加分类标签'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ========== 视频格式 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'rmt_mediaext',
                                            'label': '视频格式',
                                            'rows': 2,
                                            'placeholder': ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ========== 非媒体文件格式 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'other_mediaext',
                                            'label': '非媒体文件格式',
                                            'rows': 2,
                                            'placeholder': ".nfo, .jpg"
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ========== 媒体服务器 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'model': 'mediaservers',
                                            'label': '媒体服务器',
                                            'items': [{"title": config.name, "value": config.name}
                                                      for config in self.mediaserver_helper.get_configs().values() if
                                                      config.type == "emby"]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 8
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'emby_path',
                                            'rows': '1',
                                            'label': '媒体库路径映射',
                                            'placeholder': 'MoviePilot本地文件路径:Emby文件路径（多组路径英文逗号拼接）'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ========== 路径替换规则 ==========
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'path_replacements',
                                            'label': '路径替换规则',
                                            'rows': 3,
                                            'placeholder': '源路径:目标路径（每行一条规则）'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ========== 任务推送url ==========
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {
                                    "cols": 12,
                                },
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "url",
                                            "label": "任务推送url",
                                            "placeholder": "post请求json方式推送path和type(add)字段",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ]
            }
        ], {
            "enabled": False,
            "notify": False,
            "monitor": False,
            "cover": False,
            "onlyonce": False,
            "copy_files": False,
            "uriencode": False,
            "copy_subtitles": False,
            "refresh_emby": False,
            "mediaservers": [],
            "monitor_confs": "",
            "emby_path": "",
            "interval": 10,
            "url": "",
            "other_mediaext": ".nfo, .jpg, .png, .json",
            "rmt_mediaext": ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v",
            "path_replacements": "",
            # ========== 新增：本地路径strm默认值 ==========
            "local_strm_enabled": False,
            "local_strm_monitor": False,
            "local_strm_confs": "",
            "local_strm_cover": False,
            # ========== 新增结束 ==========
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.shutdown()
                logger.info("定时任务已停止")
        except Exception as e:
            logger.error(f"停止定时任务失败: {e}")

        try:
            for observer in self._observer:
                observer.stop()
                observer.join()
            logger.info("目录监控已停止")
        except Exception as e:
            logger.error(f"停止目录监控失败: {e}")

    def export_dir(self, fid, destination_id="0"):
        """
        获取目录导出id（115网盘相关）
        """
        pass

    def remote_sync_one(self, event: Event = None):
        """
        远程定向同步
        """
        pass

    @staticmethod
    def __find_related_paths(base_path):
        """
        查找关联路径并按修改时间倒排
        """
        return []

    def __handle_limit(self, path, limit, mon_path, event):
        """
        处理文件数量限制
        """
        pass
