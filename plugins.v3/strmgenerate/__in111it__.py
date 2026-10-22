"""
STRM 生成插件 (StrmGenerate)

把一个或多个「本地媒体源目录」镜像为一个 STRM 库目录：
  - 视频文件(mkv/mp4/...)  -> 同名 .strm 文件（内容为源文件绝对路径，可选替换规则）
  - 字幕/海报/nfo/json 等侧边文件 -> 原样复制到对应目录

能力：
  - 一次性生成：查漏补缺（只生成缺失的） / 覆盖生成（重新生成所有）
  - 定时监控：新增自动生成、源删除自动清理对应 strm 文件及空目录

设计说明：
  - 目录镜像：源目录内的文件夹结构原样复制到生成目录。
  - 视频文件替换扩展名为 .strm；侧边文件按原相对路径复制。
  - .strm 内容默认写入源视频的绝对路径（正斜杠），可配置「查找#替换」规则。
  - 多目录对用线程池并发处理，提升大批量目录性能。
"""

import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.schedulers.background import BackgroundScheduler

from app.plugins import _PluginBase
from app.sdk.config import settings
from app.sdk.logging import logger

DEFAULT_VIDEO_EXTS = "mkv,mp4,ts,m2ts,avi,mov,wmv,flv,rmvb,iso,mpg,mpeg,m4v,webm,vob,rm,mts,tp"
DEFAULT_COPY_EXTS = "srt,ass,ssa,sub,idx,sup,pgs,vtt,nfo,jpg,jpeg,png,gif,webp,bmp,tbn,json,xml,yaml,yml"

# 运行模式
MODE_MISSING = "missing"      # 查漏补缺：只生成缺失的 strm / 复制缺失的侧边文件
MODE_OVERWRITE = "overwrite"  # 覆盖生成：重新生成所有 strm / 覆盖复制所有侧边文件
MODE_SYNC = "sync"            # 监控同步：补缺失 + 更新变更 + 清理孤儿 + 删除空目录

# 并发处理目录对的最大线程数（50+ 目录时避免过载）
MAX_WORKERS = 8


class StrmGenerate(_PluginBase):
    """本地媒体目录 -> STRM 库目录 镜像生成插件。"""

    # 插件元数据
    plugin_name = "STRM生成"
    plugin_desc = "将本地媒体目录镜像为STRM库：视频生成.strm，字幕/海报/nfo等侧边文件原样复制，支持一次性生成与定时监控同步。"
    plugin_icon = "strm.png"
    plugin_version = "1.0.6"
    plugin_author = "bao"
    author_url = ""
    plugin_config_prefix = "strmgenerate_"
    plugin_order = 20
    user_level = 1

    # 私有属性
    _scheduler = None
    _event = Event()

    _enabled = False
    _onlyonce = False
    _monitor = False
    _monitor_interval = 10
    _mode = MODE_MISSING
    _dirs = ""
    _video_exts = DEFAULT_VIDEO_EXTS
    _copy_exts = DEFAULT_COPY_EXTS
    _strm_replace = ""

    # 解析后的运行时状态
    _video_ext_set: set = set()
    _copy_ext_set: set = set()
    _replace_rules: List[Tuple[str, str]] = []

    # ====================== 生命周期 ======================
    def init_plugin(self, config: dict = None) -> None:
        """读取配置并重建一次性生成 / 定时监控任务。允许重复调用。"""
        if config:
            self._enabled = bool(config.get("enabled"))
            self._onlyonce = bool(config.get("onlyonce"))
            self._monitor = bool(config.get("monitor"))
            self._monitor_interval = self.__to_int(config.get("monitor_interval"), 10)
            self._mode = config.get("mode") or MODE_MISSING
            self._dirs = config.get("dirs") or ""
            self._video_exts = config.get("video_exts") or DEFAULT_VIDEO_EXTS
            self._copy_exts = config.get("copy_exts") or DEFAULT_COPY_EXTS
            self._strm_replace = config.get("strm_replace") or ""

        # 重建解析结果（config 为 None 时也基于当前值重建）
        self._video_ext_set = self.__parse_exts(self._video_exts)
        self._copy_ext_set = self.__parse_exts(self._copy_exts)
        self._replace_rules = self.__parse_replace(self._strm_replace)

        # 停止现有任务
        self.stop_service()

        # 一次性生成 / 定时监控（二者可同时生效，互不影响）
        need_run_once = self._onlyonce
        need_monitor = self._enabled and self._monitor

        if need_run_once or need_monitor:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if need_run_once:
                logger.info("STRM生成：安排立即运行一次（模式：%s）", self.__mode_label(self._mode))
                self._scheduler.add_job(
                    func=self.__run_once,
                    trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="STRM生成-立即运行",
                )
                # 关闭一次性开关
                self._onlyonce = False
                self.update_config(self.__current_config())

            if need_monitor:
                logger.info("STRM生成：监控已启动，扫描间隔 %s 分钟", self._monitor_interval)
                self._scheduler.add_job(
                    func=self.__run_sync,
                    trigger="interval",
                    minutes=self._monitor_interval,
                    id="StrmGenerate.Monitor",
                    name="STRM生成-定时监控",
                )

            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def stop_service(self) -> None:
        """释放后台调度器。"""
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            logger.error(str(e))

    # ====================== 配置与工具 ======================
    def __current_config(self) -> dict:
        return {
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "monitor": self._monitor,
            "monitor_interval": self._monitor_interval,
            "mode": self._mode,
            "dirs": self._dirs,
            "video_exts": self._video_exts,
            "copy_exts": self._copy_exts,
            "strm_replace": self._strm_replace,
        }

    @staticmethod
    def __to_int(value: Any, default: int) -> int:
        try:
            v = int(value)
            return v if v > 0 else default
        except (TypeError, ValueError):
            return default

    @staticmethod
    def __parse_exts(text: str) -> set:
        """把「逗号分隔的扩展名」解析为小写、无点的集合。"""
        result = set()
        if not text:
            return result
        normalized = text.replace("，", ",").replace("；", ",").replace(" ", ",").replace("\n", ",")
        for part in normalized.split(","):
            part = part.strip().lstrip(".").lower()
            if part:
                result.add(part)
        return result

    @staticmethod
    def __parse_replace(text: str) -> List[Tuple[str, str]]:
        """解析「每行：查找#替换」的路径替换规则。"""
        rules: List[Tuple[str, str]] = []
        if not text:
            return rules
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            if "#" in line:
                find, replace = line.split("#", 1)
                rules.append((find, replace))
            else:
                logger.warning("STRM生成：路径替换规则格式错误（缺少 #）：%s", line)
        return rules

    def __parse_dirs(self) -> List[Tuple[Path, Path]]:
        """解析「每行：源目录#生成目录」的目录对。"""
        pairs: List[Tuple[Path, Path]] = []
        if not self._dirs:
            return pairs
        for line in self._dirs.splitlines():
            line = line.strip()
            if not line:
                continue
            if "#" not in line:
                logger.warning("STRM生成：目录配置格式错误（缺少 #）：%s", line)
                continue
            src, dst = line.split("#", 1)
            src = src.strip()
            dst = dst.strip()
            if not src or not dst:
                logger.warning("STRM生成：目录配置源或目标为空：%s", line)
                continue
            pairs.append((Path(src).expanduser(), Path(dst).expanduser()))
        return pairs

    # ====================== 入口 ======================
    def __run_once(self) -> None:
        """一次性生成，使用配置里的运行模式（查漏补缺 / 覆盖生成 / 监控同步）。"""
        logger.info("STRM生成：开始一次性运行（模式：%s）", self.__mode_label(self._mode))
        self.__process_all(self._mode)

    def __run_sync(self) -> None:
        """定时监控：补缺失 + 更新变更 + 清理孤儿。"""
        logger.info("STRM生成：开始定时监控同步")
        self.__process_all(MODE_SYNC)

    def __process_all(self, mode: str) -> None:
        if self._event.is_set():
            return
        pairs = self.__parse_dirs()
        if not pairs:
            logger.warning("STRM生成：未配置目录，跳过")
            return

        logger.info("STRM生成：共 %s 个目录对，开始处理…", len(pairs))
        total = {"strm": 0, "copy": 0, "delete": 0, "skip": 0, "error": 0}
        details: List[dict] = []

        # 目录级所有权清单：{目标目录键: 本插件镜像过的目录相对路径集合}
        manifest = self.__load_manifest()

        max_workers = min(MAX_WORKERS, len(pairs))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {}
            for src, dst in pairs:
                dst_key = self.__dst_key(dst)
                owned = set(manifest.get(dst_key, set()))
                futures[pool.submit(self.__process_pair, src, dst, mode, owned)] = (src, dst, dst_key)
            for fut in as_completed(futures):
                src, dst, dst_key = futures[fut]
                try:
                    stat, new_owned = fut.result()
                except Exception as e:
                    logger.error("STRM生成：处理目录对失败 %s -> %s：%s", src, dst, e)
                    stat = {
                        "src": str(src), "dst": str(dst),
                        "strm": 0, "copy": 0, "delete": 0, "skip": 0, "error": 1,
                    }
                    new_owned = manifest.get(dst_key, set())
                if new_owned:
                    manifest[dst_key] = new_owned
                else:
                    manifest.pop(dst_key, None)
                details.append(stat)
                total["strm"] += stat.get("strm", 0)
                total["copy"] += stat.get("copy", 0)
                total["delete"] += stat.get("delete", 0)
                total["skip"] += stat.get("skip", 0)
                total["error"] += stat.get("error", 0)

        self.__save_manifest(manifest)

        logger.info(
            "STRM生成：完成（%s）—— 生成strm %s，复制 %s，删除 %s，跳过 %s，错误 %s",
            self.__mode_label(mode),
            total["strm"], total["copy"], total["delete"], total["skip"], total["error"],
        )
        self.save_data("status", {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": mode,
            "total": total,
            "details": details,
        })

    @staticmethod
    def __mode_label(mode: str) -> str:
        return {
            MODE_MISSING: "查漏补缺",
            MODE_OVERWRITE: "覆盖生成",
            MODE_SYNC: "监控同步",
        }.get(mode, str(mode))

    # ====================== 单个目录对 ======================
    def __process_pair(self, src_root: Path, dst_root: Path, mode: str, owned: set) -> Tuple[dict, set]:
        stat = {
            "src": str(src_root), "dst": str(dst_root),
            "strm": 0, "copy": 0, "delete": 0, "skip": 0, "error": 0,
        }

        src_root = Path(os.path.abspath(str(src_root)))
        dst_root = Path(os.path.abspath(str(dst_root)))

        if not src_root.is_dir():
            # 源目录整体被删除：监控模式下清理本插件在该生成目录里创建的内容
            if mode == MODE_SYNC:
                logger.warning("STRM生成：源目录不存在，清理对应生成目录 %s", src_root)
                self.__cleanup_orphans(src_root, dst_root, owned, stat)
                return stat, set()
            else:
                logger.warning("STRM生成：源目录不存在 %s", src_root)
                stat["error"] = 1
                return stat, owned

        # 1) 生成 strm / 复制侧边文件，并把涉及的目标目录记为「插件镜像目录」
        for dirpath, _dirnames, filenames in os.walk(src_root):
            if self._event.is_set():
                return stat, owned
            for fn in filenames:
                if self._event.is_set():
                    return stat, owned
                src_file = Path(dirpath) / fn
                try:
                    rel = src_file.relative_to(src_root)
                except ValueError:
                    continue
                ext = src_file.suffix.lower().lstrip(".")
                try:
                    if ext in self._video_ext_set:
                        self.__handle_strm(src_file, dst_root, rel, mode, stat)
                        self.__add_owned(owned, rel.with_suffix(".strm").parent)
                    elif ext in self._copy_ext_set:
                        self.__handle_copy(src_file, dst_root, rel, mode, stat)
                        self.__add_owned(owned, rel.parent)
                except Exception as e:
                    logger.error("STRM生成：处理文件失败 %s：%s", src_file, e)
                    stat["error"] += 1

        # 2) 监控模式下清理孤儿（源文件夹删除 -> 整个镜像目录删除；源文件夹仍在 -> 只删孤儿 strm）
        if mode == MODE_SYNC:
            self.__cleanup_orphans(src_root, dst_root, owned, stat)

        return stat, owned

    def __handle_strm(self, src_file: Path, dst_root: Path, rel: Path, mode: str, stat: dict) -> None:
        rel_strm = rel.with_suffix(".strm")
        target = dst_root / rel_strm
        content = self.__strm_content(src_file)

        need_write = False
        if mode == MODE_OVERWRITE:
            need_write = True
        elif not target.exists():
            need_write = True
        elif mode == MODE_SYNC:
            try:
                need_write = target.read_text(encoding="utf-8") != content
            except Exception:
                need_write = True
        # MODE_MISSING 且已存在 -> 不写

        if need_write:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            stat["strm"] += 1
            logger.info("STRM生成：生成 %s <- %s", target, src_file)
        else:
            stat["skip"] += 1
            logger.debug("STRM生成：跳过（已存在）%s", target)

    def __handle_copy(self, src_file: Path, dst_root: Path, rel: Path, mode: str, stat: dict) -> None:
        target = dst_root / rel

        if mode == MODE_OVERWRITE:
            need_copy = True
        elif not target.exists():
            need_copy = True
        elif mode == MODE_SYNC:
            need_copy = self.__file_changed(src_file, target)
        else:
            need_copy = False

        if need_copy:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, target)
            stat["copy"] += 1
            logger.info("STRM生成：复制 %s <- %s", target, src_file)
        else:
            stat["skip"] += 1
            logger.debug("STRM生成：跳过（已存在）%s", target)

    @staticmethod
    def __file_changed(src: Path, dst: Path) -> bool:
        """侧边文件大小变化或源更新则需重新复制。"""
        try:
            s = src.stat()
            d = dst.stat()
            return s.st_size != d.st_size or s.st_mtime_ns > d.st_mtime_ns
        except OSError:
            return True

    def __strm_content(self, src_file: Path) -> str:
        """.strm 内容：源文件绝对路径（正斜杠），按配置规则替换。"""
        path = str(src_file).replace("\\", "/")
        for find, replace in self._replace_rules:
            path = path.replace(find, replace)
        return path

    @staticmethod
    def __dst_key(dst_root: Path) -> str:
        """目标目录的规范化键（用于所有权清单）。"""
        return os.path.normpath(os.path.abspath(str(dst_root))).replace("\\", "/").rstrip("/")

    def __manifest_path(self) -> Path:
        """所有权清单文件路径（写入插件数据目录，不污染源码/媒体目录）。"""
        return Path(self.get_data_path()) / "manifest.json"

    def __load_manifest(self) -> Dict[str, set]:
        """读取目录级所有权清单：{目标目录键: 本插件镜像过的目录相对路径集合}。"""
        try:
            data = json.loads(self.__manifest_path().read_text(encoding="utf-8"))
        except Exception:
            return {}
        result: Dict[str, set] = {}
        for key, items in data.items():
            if isinstance(items, list):
                result[str(key)] = {str(i) for i in items}
        return result

    def __save_manifest(self, manifest: Dict[str, set]) -> None:
        """持久化所有权清单。"""
        try:
            payload = {k: sorted(v) for k, v in manifest.items() if v}
            path = self.__manifest_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error("STRM生成：保存所有权清单失败：%s", e)

    @staticmethod
    def __add_owned(owned: set, rel_parent: Path) -> None:
        """把 rel 的父目录及其所有祖先目录记为「本插件镜像目录」。"""
        parts = [p for p in rel_parent.parts if p not in ("", ".", "/", "\\")]
        acc = ""
        for p in parts:
            acc = f"{acc}/{p}" if acc else p
            owned.add(acc)

    @staticmethod
    def __collect_src_dirs(src_root: Path) -> set:
        """收集源目录里存在的所有文件夹相对路径（含根目录 ""）。"""
        dirs = {""}
        for dirpath, _dirnames, _filenames in os.walk(src_root):
            rel = os.path.relpath(dirpath, src_root).replace("\\", "/")
            if rel != ".":
                dirs.add(rel)
        return dirs

    def __cleanup_orphans(self, src_root: Path, dst_root: Path, owned: set, stat: dict) -> None:
        """清理孤儿内容（sync 模式）。

        以目的目录里的子目录为单位判定：
        - 源目录里对应的文件夹仍然存在 -> 只删除「源视频已删除」的孤儿 .strm；
          json/jpg/srt 等侧边文件一律保留（无论是否由本插件复制）。
        - 源目录里对应的文件夹已不存在：
            * 该目录在本插件所有权清单内 -> 整个删除（strm + 侧边文件 + 目录一并删除）；
            * 不在清单内（用户自建）-> 保留。
        - 目的目录根目录本身永不删除。
        """
        if not dst_root.is_dir():
            return

        if src_root.is_dir():
            src_dirs = self.__collect_src_dirs(src_root)
        else:
            src_dirs = set()  # 源根目录已删除：所有目的子目录都视为「源文件夹不存在」

        to_remove_dirs: List[Path] = []    # 整个删除的镜像目录
        orphan_strm_files: List[Path] = []  # 单独删除的孤儿 strm

        for dirpath, dirnames, filenames in os.walk(dst_root):
            if self._event.is_set():
                return
            rel_dir = os.path.relpath(dirpath, dst_root).replace("\\", "/")
            if rel_dir == ".":
                rel_dir = ""

            if rel_dir in src_dirs:
                # 源文件夹仍在：只删孤儿 strm，侧边文件保留
                for fn in filenames:
                    target_file = Path(dirpath) / fn
                    if target_file.suffix.lower().lstrip(".") == "strm" and self.__is_orphan_strm(src_root, target_file):
                        orphan_strm_files.append(target_file)
            else:
                # 源文件夹已不存在
                if rel_dir == "":
                    # 根目录永不删除，仅清理其中的孤儿 strm
                    for fn in filenames:
                        target_file = Path(dirpath) / fn
                        if target_file.suffix.lower().lstrip(".") == "strm" and self.__is_orphan_strm(src_root, target_file):
                            orphan_strm_files.append(target_file)
                elif rel_dir in owned:
                    # 插件镜像目录：源文件夹已删，整个删除
                    to_remove_dirs.append(Path(dirpath))
                    dirnames[:] = []  # 不再深入遍历
                # else: 用户自建目录，保留

        for target_file in orphan_strm_files:
            try:
                target_file.unlink()
                stat["delete"] += 1
                logger.info("STRM生成：删除孤儿 strm %s", target_file)
            except OSError as e:
                logger.warning("STRM生成：删除失败 %s：%s", target_file, e)
                stat["error"] += 1

        for d in to_remove_dirs:
            try:
                shutil.rmtree(d)
                stat["delete"] += 1
                logger.info("STRM生成：删除孤儿目录（源文件夹已删除）%s", d)
            except OSError as e:
                logger.warning("STRM生成：删除失败 %s：%s", d, e)
                stat["error"] += 1

    def __is_orphan_strm(self, src_root: Path, target_file: Path) -> bool:
        """判断 .strm 是否为孤儿：内容指向 src_root 之内、且对应源文件已不存在。"""
        try:
            content = target_file.read_text(encoding="utf-8").strip()
        except Exception:
            return False
        if not content:
            return False
        src_path = self.__recover_source_path(content)
        if not src_path:
            return False
        if not self.__is_under(src_root, src_path):
            return False
        return not Path(src_path).exists()

    def __recover_source_path(self, content: str) -> Optional[str]:
        """把 strm 内容（可能已按规则替换）还原为真实源路径。"""
        path = content
        for find, replace in reversed(self._replace_rules):
            path = path.replace(replace, find)
        return path

    @staticmethod
    def __is_under(src_root: Path, path: str) -> bool:
        """判断路径是否位于 src_root 之下（含相等）。"""
        try:
            s = os.path.normpath(str(src_root)).replace("\\", "/").rstrip("/")
            p = os.path.normpath(path).replace("\\", "/").rstrip("/")
        except Exception:
            return False
        return p == s or p.startswith(s + "/")

    # ====================== 页面 ======================
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}},
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即生成一次"}},
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "monitor", "label": "开启监控（定时扫描）"}},
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "mode",
                                            "label": "运行模式（影响「立即生成一次」）",
                                            "items": [
                                                {"title": "查漏补缺（只生成缺失的）", "value": "missing"},
                                                {"title": "覆盖生成（重新生成所有）", "value": "overwrite"},
                                                {"title": "监控同步（补缺失+删除孤儿）", "value": "sync"},
                                            ],
                                        },
                                    },
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "monitor_interval",
                                            "label": "监控扫描间隔(分钟)",
                                            "type": "number",
                                            "placeholder": "10",
                                        },
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "dirs",
                                            "label": "目录配置（每行：源目录#生成目录）",
                                            "rows": 10,
                                            "placeholder": "/media/电影#/strm/电影\n/media/电视剧#/strm/电视剧",
                                        },
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "video_exts",
                                            "label": "视频格式（逗号分隔，生成strm）",
                                        },
                                    },
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "copy_exts",
                                            "label": "复制格式（逗号分隔，原样复制）",
                                        },
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "strm_replace",
                                            "label": "STRM路径替换（可选，每行：查找#替换）",
                                            "rows": 3,
                                            "placeholder": "/mnt/media#smb://NAS/media",
                                        },
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": "STRM文件内容为源视频绝对路径；填写路径替换规则后按「查找#替换」逐条替换。监控需与「启用插件」同时打开才生效。删除规则：源文件夹已删除时，本插件镜像生成的对应文件夹（含 strm 与 json/nfo/srt 等侧边文件）会整个删除；源文件夹仍在时，只清理「源视频已删」的孤儿 strm，json/jpg/srt 等侧边文件一律保留（无论复制来的还是你自己放的）。非本插件生成的目录/文件一律不动。把运行模式选为「监控同步」再点「立即生成一次」，可立即清理孤儿文件。",
                                        },
                                    },
                                ],
                            },
                        ],
                    },
                ],
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "monitor": False,
            "monitor_interval": 10,
            "mode": MODE_MISSING,
            "dirs": "",
            "video_exts": DEFAULT_VIDEO_EXTS,
            "copy_exts": DEFAULT_COPY_EXTS,
            "strm_replace": "",
        }

    def get_page(self) -> List[dict]:
        status = self.get_data("status") or {}
        monitor_on = bool(self._enabled and self._monitor)
        monitor_line = (
            f"监控状态：已开启（每 {self._monitor_interval} 分钟扫描一次）"
            if monitor_on
            else "监控状态：未开启（需「启用插件」+「开启监控」同时打开）"
        )
        if not status:
            text = (
                f"{monitor_line}\n"
                "尚未运行过。可点「立即生成一次」先全量生成；"
                "要清理已删除源对应的孤儿文件，可开启监控等待扫描，"
                "或把运行模式选为「监控同步」后点「立即生成一次」。"
            )
        else:
            total = status.get("total") or {}
            text = (
                f"{monitor_line}\n"
                f"上次运行：{status.get('time', '-')}（模式：{self.__mode_label(status.get('mode'))}）\n"
                f"生成strm {total.get('strm', 0)}，复制 {total.get('copy', 0)}，"
                f"删除 {total.get('delete', 0)}，跳过 {total.get('skip', 0)}，错误 {total.get('error', 0)}"
            )
        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "text": text}},
                        ],
                    },
                ],
            }
        ]
