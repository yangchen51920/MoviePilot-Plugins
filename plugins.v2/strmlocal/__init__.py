import os
import shutil
import threading
import traceback
from pathlib import Path
from typing import Any, List, Dict, Tuple, Optional

from apscheduler.schedulers.background import BackgroundScheduler
from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

from app.core.config import settings
from app.core.event import Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType

lock = threading.Lock()


class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类（单Observer多目录模式）
    """

    def __init__(self, sync: Any, **kwargs):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._sync = sync

    def _find_mon_path(self, event_path: str) -> str:
        """根据事件路径找到匹配的监控源目录"""
        for mon_path in self._sync._strm_dir_conf.keys():
            if event_path.startswith(mon_path):
                return mon_path
        return None

    def on_created(self, event):
        mon_path = self._find_mon_path(event.src_path)
        if mon_path:
            self._sync.event_handler(
                event=event,
                mon_path=mon_path,
                text="创建",
                event_path=event.src_path,
            )

    def on_moved(self, event):
        mon_path = self._find_mon_path(event.dest_path)
        if mon_path:
            self._sync.event_handler(
                event=event,
                mon_path=mon_path,
                text="移动",
                event_path=event.dest_path,
            )

    def on_deleted(self, event):
        mon_path = self._find_mon_path(event.src_path)
        if mon_path:
            self._sync.event_handler(
                event=event,
                mon_path=mon_path,
                text="删除",
                event_path=event.src_path,
            )


class StrmLocal(_PluginBase):

    # 插件名称
    plugin_name = "本地Strm助手"
    # 插件描述
    plugin_desc = "监控本地目录文件变化，生成包含本地文件路径的strm文件。"
    # 插件图标
    plugin_icon = ""
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "yangchen51920"
    # 作者主页
    author_url = "https://github.com/yangchen51920"
    # 插件配置项ID前缀
    plugin_config_prefix = "StrmLocal_"
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
    _strm_dir_conf = {}
    _observer = None
    _rmt_mediaext = None
    _path_replacements = {}
    _scheduler = None

    def init_plugin(self, config: dict = None):
        old_strm_dir_conf = self._strm_dir_conf.copy() if self._strm_dir_conf else {}

        # 清空配置
        self._strm_dir_conf = {}
        self._path_replacements = {}

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._monitor = config.get("monitor")
            self._cover = config.get("cover")
            self._monitor_confs = config.get("monitor_confs")

            self._rmt_mediaext = config.get(
                "rmt_mediaext") or ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v"

            # 读取路径替换规则
            if config.get("path_replacements"):
                for replacement in str(config.get("path_replacements")).split("\n"):
                    if replacement and ":" in replacement:
                        source, target = replacement.split(":", 1)
                        self._path_replacements[source.strip()] = target.strip()

            # 解析目录配置
            # 格式: 本地媒体路径#strm生成路径
            # 可选: 本地媒体路径#strm生成路径$手工执行
            # 可选: 本地媒体路径#strm生成路径@分类标签
            if self._monitor_confs:
                monitor_confs = str(self._monitor_confs).split("\n")
                for monitor_conf in monitor_confs:
                    if not monitor_conf:
                        continue
                    if not monitor_conf.startswith("#") and "#" in monitor_conf:
                        confs = monitor_conf.split("#")
                        if len(confs) >= 2:
                            source_path = confs[0].strip()
                            strm_output_path = confs[1].strip()

                            manual_only = False
                            if "$" in strm_output_path:
                                strm_output_path = strm_output_path.replace("$", "")
                                manual_only = True
                            if "@" in strm_output_path:
                                strm_output_path = strm_output_path.split("@")[0]

                            if manual_only:
                                logger.info(f"目录配置 {source_path} 设置为仅手工执行，跳过自动监控")
                            else:
                                self._strm_dir_conf[source_path] = strm_output_path
                                logger.info(f"加载目录配置: {source_path} -> {strm_output_path}")

            # 检测配置变更，清理已移除/变更映射的旧strm文件
            if old_strm_dir_conf:
                self.__cleanup_removed_configs(old_strm_dir_conf)

            # 启动定时器
            if self._enabled:
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                # 全量扫描（每天凌晨3点）
                self._scheduler.add_job(
                    self.scan,
                    "cron",
                    hour=3,
                    minute=0,
                    second=0,
                    id="strm_local_full_scan"
                )
                self._scheduler.start()
                logger.info("本地Strm助手定时任务已启动")

                # 启动目录监控（单个Observer监控所有目录）
                if self._monitor:
                    valid_paths = [p for p in self._strm_dir_conf.keys() if Path(p).exists()]
                    if valid_paths:
                        self._observer = PollingObserver(timeout=10)
                        handler = FileMonitorHandler(self)
                        for mon_path in valid_paths:
                            self._observer.schedule(handler, path=mon_path, recursive=True)
                            logger.info(f"已添加目录监控: {mon_path}")
                        self._observer.start()
                        logger.info(f"目录监控已启动，共监控 {len(valid_paths)} 个目录")
                    else:
                        logger.warning("没有有效的监控目录")

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
        for mon_path, strm_dir in self._strm_dir_conf.items():
            if not Path(mon_path).exists():
                logger.warning(f"源目录不存在，跳过: {mon_path}")
                continue

            logger.info(f"全量扫描: {mon_path} -> {strm_dir}")

            for root, dirs, files in os.walk(mon_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    try:
                        self.__handle_file(
                            event_path=str(file_path),
                            mon_path=mon_path,
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

        for mon_path in self._strm_dir_conf.keys():
            if str(file_path).startswith(str(mon_path)):
                self.__handle_file(
                    event_path=str(file_path),
                    mon_path=mon_path,
                )
                break

    def event_handler(self, event, mon_path: str, text: str, event_path: str):
        """
        处理文件变化事件
        """
        logger.info(f"检测到文件{text}: {event_path}")

        # 处理删除事件
        if text == "删除":
            strm_dir = self._strm_dir_conf.get(mon_path)
            if strm_dir:
                self.__delete_strm_file(
                    event_path=event_path,
                    mon_path=mon_path,
                    strm_dir=strm_dir,
                )
            return

        # 处理新增/移动事件
        self.__handle_file(event_path=event_path, mon_path=mon_path)

    def __handle_file(self, event_path: str, mon_path: str):
        """
        处理单个文件，生成strm或复制关联文件
        """
        try:
            if not Path(event_path).exists():
                return
            with lock:
                strm_dir = self._strm_dir_conf.get(mon_path)
                if not strm_dir:
                    return

                target_file = str(event_path).replace(mon_path, strm_dir)

                if Path(event_path).suffix.lower() in [ext.strip() for ext in
                                                       self._rmt_mediaext.split(",")]:
                    # 媒体文件：生成strm
                    strm_content = str(event_path).replace("\\", "/")
                    self.__create_strm_file(strm_file=target_file, strm_content=strm_content)

                    # 复制同名关联文件（nfo、jpg等）
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
                    # 非媒体文件：直接复制
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

    def __delete_strm_file(self, event_path: str, mon_path: str, strm_dir: str):
        """
        删除源文件对应的strm文件，并清理空目录
        """
        try:
            with lock:
                target_file = str(event_path).replace(mon_path, strm_dir)
                strm_file = os.path.join(
                    Path(target_file).parent,
                    f"{os.path.splitext(Path(target_file).name)[0]}.strm"
                )

                if Path(strm_file).exists():
                    os.remove(strm_file)
                    logger.info(f"已删除strm文件: {strm_file}")
                else:
                    logger.debug(f"strm文件不存在，跳过删除: {strm_file}")

                # 删除关联的非媒体文件
                try:
                    pattern = Path(event_path).stem.replace('[', '?').replace(']', '?')
                    for f in Path(target_file).parent.glob(f"{pattern}.*"):
                        if f.suffix.lower() != ".strm":
                            os.remove(str(f))
                            logger.info(f"已删除关联文件: {f}")
                except Exception as e:
                    logger.debug(f"清理关联文件失败: {e}")

                # 清理空目录
                self.__cleanup_empty_dirs(Path(strm_file).parent, Path(strm_dir))
        except Exception as e:
            logger.error(f"删除strm文件失败 {event_path}: {e}")

    def __cleanup_empty_dirs(self, target_dir: Path, root_strm_dir: Path):
        """
        递归清理空目录，直到strm根目录为止
        """
        try:
            current = target_dir
            while current != root_strm_dir and current.exists():
                try:
                    if any(current.iterdir()):
                        break
                    current.rmdir()
                    logger.info(f"已清理空目录: {current}")
                    current = current.parent
                except OSError:
                    break
        except Exception as e:
            logger.debug(f"清理空目录失败: {e}")

    def __cleanup_removed_configs(self, old_strm_dir_conf: dict):
        """
        检测配置变更，清理已移除或源路径变更的映射对应的旧strm文件
        """
        current_strm_dirs = set(self._strm_dir_conf.values())

        for old_mon_path, old_strm_dir in old_strm_dir_conf.items():
            if old_mon_path not in self._strm_dir_conf:
                if old_strm_dir not in current_strm_dirs:
                    logger.info(f"配置已移除，清理strm目录: 源={old_mon_path} → strm目录={old_strm_dir}")
                    self.__cleanup_strm_dir(old_strm_dir)
                else:
                    logger.info(f"检测到源路径变更，清理旧strm文件: 旧源={old_mon_path} → strm目录={old_strm_dir}")
                    self.__cleanup_strm_dir(old_strm_dir)

    def __cleanup_strm_dir(self, strm_dir: str):
        """
        删除指定strm输出目录下的所有.strm文件、关联文件，并清理空目录
        """
        try:
            path = Path(strm_dir)
            if not path.exists():
                logger.debug(f"strm目录不存在，跳过清理: {strm_dir}")
                return

            deleted_count = 0
            for strm_file in path.rglob("*.strm"):
                try:
                    strm_file.unlink()
                    deleted_count += 1
                    logger.debug(f"已清理strm文件: {strm_file}")
                except Exception as e:
                    logger.error(f"清理strm文件失败 {strm_file}: {e}")

            related_exts = {".nfo", ".jpg", ".jpeg", ".png", ".json", ".xml",
                            ".ass", ".ssa", ".srt", ".sub", ".sup", ".vtt"}
            for ext in related_exts:
                for related_file in path.rglob(f"*{ext}"):
                    try:
                        related_file.unlink()
                        deleted_count += 1
                        logger.debug(f"已清理关联文件: {related_file}")
                    except Exception as e:
                        logger.error(f"清理关联文件失败 {related_file}: {e}")

            self.__cleanup_empty_dirs_recursive(path, strm_dir)

            if path.exists():
                try:
                    if not any(path.iterdir()):
                        path.rmdir()
                        logger.info(f"已清理空strm根目录: {path}")
                except OSError:
                    pass

            logger.info(f"strm目录清理完成: {strm_dir}，共清理 {deleted_count} 个文件")
        except Exception as e:
            logger.error(f"清理strm目录失败 {strm_dir}: {e}")

    def __cleanup_empty_dirs_recursive(self, target_dir: Path, root_strm_dir: str):
        """
        从最深层向上递归清理所有空目录
        """
        try:
            root = Path(root_strm_dir)
            for dirpath in sorted(target_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if dirpath.is_dir() and dirpath != root:
                    try:
                        if not any(dirpath.iterdir()):
                            dirpath.rmdir()
                            logger.debug(f"已清理空目录: {dirpath}")
                    except OSError:
                        pass
        except Exception as e:
            logger.debug(f"递归清理空目录失败: {e}")

    def __create_strm_file(self, strm_file: str, strm_content: str):
        """
        生成strm文件
        """
        try:
            if not Path(strm_file).parent.exists():
                logger.info(f"创建目标文件夹 {Path(strm_file).parent}")
                os.makedirs(Path(strm_file).parent)

            strm_file = os.path.join(
                Path(strm_file).parent,
                f"{os.path.splitext(Path(strm_file).name)[0]}.strm"
            )

            if Path(strm_file).exists() and not self._cover:
                logger.info(f"目标文件 {strm_file} 已存在")
                return

            # 应用路径替换规则
            for source, target in self._path_replacements.items():
                if source in strm_content:
                    strm_content = strm_content.replace(source, target)
                    logger.debug(f"应用路径替换规则: {source} -> {target}")

            with open(strm_file, 'w', encoding='utf-8') as f:
                f.write(strm_content)

            logger.info(f"创建strm文件成功 {strm_file} -> {strm_content}")
            return True
        except Exception as e:
            logger.error(f"创建strm文件失败 {strm_file} -> {str(e)}")
        return False

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "monitor": self._monitor,
            "cover": self._cover,
            "onlyonce": self._onlyonce,
            "monitor_confs": self._monitor_confs,
            "rmt_mediaext": self._rmt_mediaext,
            "path_replacements": "\n".join(
                [f"{k}:{v}" for k, v in self._path_replacements.items()]
            ),
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/strm_local_scan",
                "event": EventType.PluginAction,
                "desc": "本地Strm助手-全量扫描",
                "category": "本地Strm助手",
                "data": {
                    "action": "strm_local_scan"
                }
            },
            {
                "cmd": "/strm_local_file",
                "event": EventType.PluginAction,
                "desc": "本地Strm助手-处理单个文件",
                "category": "本地Strm助手",
                "data": {
                    "action": "strm_local_file"
                }
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        return []

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
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '本地Strm助手：监控本地目录文件变化，自动生成包含本地文件路径的strm文件。'
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
                                            'model': 'cover',
                                            'label': '覆盖已存在文件',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ========== 第二行开关 ==========
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
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ========== 目录配置 ==========
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
                                            'label': '目录配置',
                                            'rows': 5,
                                            'placeholder': '本地媒体源路径#strm生成路径\n/mnt/nas/media/series#/mnt/nas/library/series\n/mnt/nas/media/movies#/mnt/nas/library/movies'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    # ========== 目录配置说明 ==========
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
                                            'text': '目录配置格式（每行一个目录对）：\n'
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
                                            'placeholder': '源路径:目标路径（每行一条规则）\n/mnt/nas:/media'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "monitor": False,
            "cover": False,
            "onlyonce": False,
            "monitor_confs": "",
            "rmt_mediaext": ".mp4, .mkv, .ts, .iso,.rmvb, .avi, .mov, .mpeg,.mpg, .wmv, .3gp, .asf, .m4v, .flv, .m2ts, .strm,.tp, .f4v",
            "path_replacements": "",
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
            if self._observer:
                self._observer.stop()
                self._observer.join()
                logger.info("目录监控已停止")
        except Exception as e:
            logger.error(f"停止目录监控失败: {e}")
