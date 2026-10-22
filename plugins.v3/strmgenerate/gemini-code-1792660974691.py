"""
STRM 生成插件 (StrmGenerate) - MoviePilot V3 规范高性能版

功能特性：
  1. 目录镜像：源目录（如 /media/video1/...）镜像生成到 NVMe 目标目录（如 /media/nvme/.../video1）。
  2. 视频文件 -> 同名 .strm 文件（内容为源文件绝对路径，支持路径替换规则）。
  3. 附属文件（字幕/海报/nfo/json等） -> 按相对路径原样复制到对应 strm 目录。
  4. 运行模式：
     - 查漏补缺（missing）：只生成缺失的 .strm 和缺失的附属文件。
     - 覆盖生成（overwrite）：重新生成所有 .strm 并覆盖复制所有附属文件。
     - 监控同步（sync）：补齐缺失 + 源文件修改时间更新时覆盖附属文件 + 清理已删源对应的孤儿 .strm 及文件夹。
  5. 安全边界：
     - 删掉配置中的某行目录对，绝不影响已生成的 strm。
     - 绝不触碰目标配置目录之外的任何文件与文件夹。
     - 源文件夹仍在时：只删除源视频已删的孤儿 .strm，保留所有 json/jpg/srt/nfo（含手动添加的文件）。
     - 源文件夹已删时：将本插件镜像生成的对应目标子文件夹（含内部 .strm、.json 等所有文件）整目录删除。
"""

import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from threading import Event, Lock
from typing import Any, Dict, List, Set, Tuple

from apscheduler.triggers.interval import IntervalTrigger

from app.sdk import scheduler as scheduler_sdk
from app.sdk. logging import logger
from app.sdk.plugin import _PluginBase

DEFAULT_VIDEO_EXTS = "mkv,mp4,ts,m2ts,avi,mov,wmv,flv,rmvb,iso,mpg,mpeg,m4v,webm,vob,rm,mts,tp"
DEFAULT_COPY_EXTS = "srt,ass,ssa,sub,idx,sup,pgs,vtt,nfo,jpg,jpeg,png,gif,webp,bmp,tbn,json,xml,yaml,yml"

# 运行模式常量
MODE_MISSING = "missing"      # 查漏补缺：只生成缺失的 strm / 复制缺失的附属文件
MODE_OVERWRITE = "overwrite"  # 覆盖生成：重新生成所有 strm / 覆盖复制所有附属文件
MODE_SYNC = "sync"            # 监控同步：补缺失 + 源更新时间较新时覆盖附属文件 + 清理孤儿文件与目录

# 并发处理目录对的最大线程数（50+ 目录时兼顾 NVMe 写入与源盘并发读取）
MAX_WORKERS = 8

# 一次性任务固定 ID
ONCE_JOB_ID = "strm_generate_once"


class StrmGenerate(_PluginBase):
    """本地媒体目录 -> STRM 库目录 高性能镜像生成插件（V3）。"""

    # 插件元数据
    plugin_name = "STRM生成"
    plugin_desc = "将本地媒体目录镜像为STRM库：视频生成.strm，字幕/海报/nfo/json等原样复制，支持一次性生成与定时监控同步。"
    plugin_icon = "strm.png"
    plugin_version = "1.1.0"
    plugin_author = "bao"
    author_url = ""
    plugin_config_prefix = "strmgenerate_"
    plugin_order = 20
    auth_level = 1
    user_level = 1

    def __init__(self):
        super().__init__()
        self._event = Event()
        self._run_lock = Lock()

        self._enabled: bool = False
        self._onlyonce: bool = False
        self._monitor: bool = False
        self._monitor_interval: int = 10
        self._mode: str = MODE_MISSING
        self._dirs: str = ""
        self._video_exts: str = DEFAULT_VIDEO_EXTS
        self._copy_exts: str = DEFAULT_COPY_EXTS
        self._strm_replace: str = ""

        # 解析后的运行时状态
        self._video_ext_set: Set[str] = set()
        self._copy_ext_set: Set[str] = set()
        self._replace_rules: List[Tuple[str, str]] = []

    # ====================== 生命周期 ======================
    def init_plugin(self, config: dict | None = None) -> None:
        """读取配置并初始化插件状态，支持重复调用。"""
        # 先清理可能待执行的一次性任务并重置中断信号
        self._remove_once_job()
        self._event.clear()

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

        # 重建扩展名集合与替换规则
        self._video_ext_set = self.__parse_exts(self._video_exts)
        self._copy_ext_set = self.__parse_exts(self._copy_exts)
        self._replace_rules = self.__parse_replace(self._strm_replace)

        # 处理「立即生成一次」（遵循 V3 规范：使用宿主 add_plugin_once_job，不自建 BackgroundScheduler）
        if self._onlyonce:
            logger.info("STRM生成：已安排 3 秒后立即运行一次（模式：%s）", self.__mode_label(self._mode))
            self._onlyonce = False
            self.update_config(self.__current_config())

            add_once_fn = getattr(scheduler_sdk, "add_plugin_once_job", None)
            if callable(add_once_fn):
                add_once_fn(
                    self.__class__.__name__,
                    ONCE_JOB_ID,
                    self.__run_once,
                    f"STRM生成-立即运行({self.__mode_label(self._mode)})",
                    delay_seconds=3,
                )
            else:
                logger.warning("STRM生成：当前宿主环境不支持 add_plugin_once_job，立即直接执行")
                self.__run_once()

    def get_state(self) -> bool:
        """返回插件启用状态。"""
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """暂不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """暂不注册后端 API。"""
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        """遵循 V3 规范注册定时监控服务，由宿主统一调度。"""
        if not (self._enabled and self._monitor):
            return []
        return [
            {
                "id": f"{self.__class__.__name__}.Monitor",
                "name": "STRM生成-定时监控同步",
                "trigger": IntervalTrigger(minutes=self._monitor_interval),
                "func": self.__run_sync,
                "kwargs": {},
            }
        ]

    def stop_service(self) -> None:
        """停止插件后台任务并释放信号。"""
        self._event.set()
        self._remove_once_job()

    def _remove_once_job(self) -> None:
        """移除尚未执行的一次性任务。"""
        try:
            remove_once_fn = getattr(scheduler_sdk, "remove_plugin_once_job", None)
            if callable(remove_once_fn):
                remove_once_fn(self.__class__.__name__, ONCE_JOB_ID)
        except Exception as e:
            logger.debug("STRM生成：移除一次性任务时忽略异常：%s", e)

    # ====================== 配置与解析工具 ======================
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
    def __parse_exts(text: str) -> Set[str]:
        """将逗号分隔的扩展名解析为小写、无点集合。"""
        result: Set[str] = set()
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
        """解析每行「查找#替换」的路径替换规则。"""
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
        """解析每行「本地媒体源路径#strm生成路径」的目录对。"""
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

    # ====================== 任务执行入口 ======================
    def __run_once(self) -> None:
        """一次性运行入口（按所选运行模式执行）。"""
        logger.info("STRM生成：开始一次性运行（模式：%s）", self.__mode_label(self._mode))
        self.__process_all(self._mode)

    def __run_sync(self) -> None:
        """定时监控入口（固定以监控同步模式执行）。"""
        logger.info("STRM生成：开始定时监控同步")
        self.__process_all(MODE_SYNC)

    def __process_all(self, mode: str) -> None:
        if self._event.is_set():
            return

        # 防重入保护：如果上一轮任务尚未结束，跳过本轮触发
        if not self._run_lock.acquire(blocking=False):
            logger.warning("STRM生成：上一轮扫描任务仍在运行中，本次触发（%s）自动跳过", self.__mode_label(mode))
            return

        try:
            pairs = self.__parse_dirs()
            if not pairs:
                logger.warning("STRM生成：未配置有效目录对，跳过执行")
                return

            start_ts = datetime.now()
            logger.info("STRM生成：共 %s 个目录对，开始并发处理（模式：%s）…", len(pairs), self.__mode_label(mode))
            total = {"strm": 0, "copy": 0, "delete": 0, "skip": 0, "error": 0}
            details: List[dict] = []

            # 读取所有权清单：{目标目录键: 本插件镜像过的目录相对路径集合}
            # 并记录上次的替换规则签名，用于判断是否需要校验已有 strm 内容
            manifest, last_replace_sig = self.__load_manifest()
            current_replace_sig = json.dumps(self._replace_rules, ensure_ascii=False)
            replace_rule_changed = (current_replace_sig != last_replace_sig)

            max_workers = min(MAX_WORKERS, len(pairs))
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {}
                for src, dst in pairs:
                    dst_key = self.__dst_key(dst)
                    owned = set(manifest.get(dst_key, set()))
                    futures[
                        pool.submit(self.__process_pair, src, dst, mode, owned, replace_rule_changed)
                    ] = (src, dst, dst_key)

                for fut in as_completed(futures):
                    src, dst, dst_key = futures[fut]
                    try:
                        stat, new_owned = fut.result()
                    except Exception as e:
                        logger.error("STRM生成：处理目录对异常 %s -> %s：%s", src, dst, e)
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

            self.__save_manifest(manifest, current_replace_sig)
            elapsed = (datetime.now() - start_ts).total_seconds()

            logger.info(
                "STRM生成：完成（%s，耗时 %.2f 秒）—— 生成strm %s，复制 %s，删除 %s，跳过 %s，错误 %s",
                self.__mode_label(mode), elapsed,
                total["strm"], total["copy"], total["delete"], total["skip"], total["error"],
            )
            self.save_data("status", {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "elapsed": f"{elapsed:.2f}s",
                "mode": mode,
                "total": total,
                "details": details,
            })
        finally:
            self._run_lock.release()

    @staticmethod
    def __mode_label(mode: str) -> str:
        return {
            MODE_MISSING: "查漏补缺",
            MODE_OVERWRITE: "覆盖生成",
            MODE_SYNC: "监控同步",
        }.get(mode, str(mode))

    # ====================== 单个目录对处理 ======================
    def __process_pair(
        self,
        src_root: Path,
        dst_root: Path,
        mode: str,
        owned: Set[str],
        replace_rule_changed: bool,
    ) -> Tuple[dict, Set[str]]:
        stat = {
            "src": str(src_root), "dst": str(dst_root),
            "strm": 0, "copy": 0, "delete": 0, "skip": 0, "error": 0,
        }

        src_root = Path(os.path.abspath(str(src_root)))
        dst_root = Path(os.path.abspath(str(dst_root)))

        if not src_root.is_dir():
            # 源目录整体不存在：监控模式下清理该目标目录内由本插件生成的镜像内容
            if mode == MODE_SYNC:
                logger.warning("STRM生成：源目录已不存在，开始清理对应生成目录 %s -> %s", src_root, dst_root)
                self.__cleanup_orphans(dst_root, set(), set(), owned, stat)
                return stat, owned
            else:
                logger.warning("STRM生成：源目录不存在 %s", src_root)
                stat["error"] = 1
                return stat, owned

        # 内存集合：单次遍历同时收集源子目录与预期生成的 .strm 相对路径，避免二次遍历源盘和逐个读文件
        src_dirs: Set[str] = {""}
        expected_strms: Set[str] = set()
        created_dirs: Set[Path] = set()

        # 1) 单次遍历源目录：生成 .strm 与复制附属文件
        for dirpath, _dirnames, filenames in os.walk(src_root):
            if self._event.is_set():
                return stat, owned

            rel_dir_str = os.path.relpath(dirpath, src_root).replace("\\", "/")
            if rel_dir_str == ".":
                rel_dir_str = ""
            else:
                src_dirs.add(rel_dir_str)
                # 只要源目录存在该子文件夹，且内部有需要处理的文件或目标已存在对应目录，后续会纳入所有权管理

            rel_dir_path = Path(rel_dir_str) if rel_dir_str else Path()

            for fn in filenames:
                if self._event.is_set():
                    return stat, owned
                ext = os.path.splitext(fn)[1].lower().lstrip(".")
                if ext not in self._video_ext_set and ext not in self._copy_ext_set:
                    continue

                src_file = Path(dirpath) / fn
                rel_file = rel_dir_path / fn if rel_dir_str else Path(fn)

                try:
                    if ext in self._video_ext_set:
                        rel_strm = rel_file.with_suffix(".strm")
                        expected_strms.add(str(rel_strm).replace("\\", "/"))
                        self.__handle_strm(
                            src_file, dst_root, rel_strm, mode, stat, created_dirs, replace_rule_changed
                        )
                        self.__add_owned(owned, rel_dir_str)
                    elif ext in self._copy_ext_set:
                        self.__handle_copy(src_file, dst_root, rel_file, mode, stat, created_dirs)
                        self.__add_owned(owned, rel_dir_str)
                except Exception as e:
                    logger.error("STRM生成：处理文件失败 %s：%s", src_file, e)
                    stat["error"] += 1

        # 2) 监控同步模式下清理孤儿：
        #    - 源文件夹被删 -> 整个删除对应镜像文件夹（含内部 .strm、.json、.jpg、.srt 等所有文件）
        #    - 源文件夹仍在 -> 只删源视频已不存在的孤儿 .strm，保留所有 json/jpg/srt/nfo
        if mode == MODE_SYNC:
            self.__cleanup_orphans(dst_root, src_dirs, expected_strms, owned, stat)

        return stat, owned

    def __ensure_parent_dir(self, parent: Path, created_dirs: Set[Path]) -> None:
        """带内存缓存的目录创建，减少高频系统调用开销。"""
        if parent not in created_dirs:
            parent.mkdir(parents=True, exist_ok=True)
            created_dirs.add(parent)

    def __handle_strm(
        self,
        src_file: Path,
        dst_root: Path,
        rel_strm: Path,
        mode: str,
        stat: dict,
        created_dirs: Set[Path],
        replace_rule_changed: bool,
    ) -> None:
        target = dst_root / rel_strm

        need_write = False
        content = None

        if mode == MODE_OVERWRITE:
            need_write = True
        elif not target.exists():
            need_write = True
        elif mode == MODE_SYNC and replace_rule_changed:
            # 仅当用户修改过路径替换规则时，才读取已有 .strm 校验内容是否需要更新
            content = self.__strm_content(src_file)
            try:
                need_write = target.read_text(encoding="utf-8") != content
            except Exception:
                need_write = True

        if need_write:
            if content is None:
                content = self.__strm_content(src_file)
            self.__ensure_parent_dir(target.parent, created_dirs)
            target.write_text(content, encoding="utf-8")
            stat["strm"] += 1
            logger.info("STRM生成：生成 %s <- %s", target, src_file)
        else:
            stat["skip"] += 1

    def __handle_copy(
        self,
        src_file: Path,
        dst_root: Path,
        rel_file: Path,
        mode: str,
        stat: dict,
        created_dirs: Set[Path],
    ) -> None:
        target = dst_root / rel_file

        if mode == MODE_OVERWRITE:
            need_copy = True
        elif not target.exists():
            need_copy = True
        elif mode == MODE_SYNC:
            # 监控模式下：当源文件的修改时间比目的文件更新时，覆盖它
            need_copy = self.__is_src_newer(src_file, target)
        else:
            need_copy = False

        if need_copy:
            self.__ensure_parent_dir(target.parent, created_dirs)
            shutil.copy2(src_file, target)
            stat["copy"] += 1
            logger.info("STRM生成：复制 %s <- %s", target, src_file)
        else:
            stat["skip"] += 1

    @staticmethod
    def __is_src_newer(src: Path, dst: Path) -> bool:
        """判断源文件修改时间是否比目标文件更新（预留 1 毫秒容差避免跨文件系统精度抖动）。"""
        try:
            s_mtime = src.stat().st_mtime_ns
            d_mtime = dst.stat().st_mtime_ns
            return s_mtime > (d_mtime + 1_000_000)
        except OSError:
            return True

    def __strm_content(self, src_file: Path) -> str:
        """生成 .strm 文件内容：源文件绝对路径（统一正斜杠），并应用路径替换规则。"""
        path = str(src_file).replace("\\", "/")
        for find, replace in self._replace_rules:
            path = path.replace(find, replace)
        return path

    # ====================== 所有权清单与孤儿清理 ======================
    @staticmethod
    def __dst_key(dst_root: Path) -> str:
        """目标目录规范化唯一键。"""
        return os.path.normpath(os.path.abspath(str(dst_root))).replace("\\", "/").rstrip("/")

    def __manifest_path(self) -> Path:
        """所有权清单路径（保存在插件独立数据目录，遵循 V3 规范）。"""
        return Path(self.get_data_path()) / "manifest.json"

    def __load_manifest(self) -> Tuple[Dict[str, Set[str]], str]:
        """读取目录级所有权清单及上次路径替换规则签名。"""
        try:
            raw = json.loads(self.__manifest_path().read_text(encoding="utf-8"))
        except Exception:
            return {}, ""

        if not isinstance(raw, dict):
            return {}, ""

        # 兼容旧格式与新格式
        replace_sig = str(raw.get("__replace_sig__", ""))
        dirs_data = raw.get("__dirs__", raw) if "__dirs__" in raw else raw

        result: Dict[str, Set[str]] = {}
        for key, items in dirs_data.items():
            if key.startswith("__"):
                continue
            if isinstance(items, list):
                result[str(key)] = {str(i) for i in items}
        return result, replace_sig

    def __save_manifest(self, manifest: Dict[str, Set[str]], replace_sig: str) -> None:
        """持久化目录级所有权清单。"""
        try:
            payload = {
                "__replace_sig__": replace_sig,
                "__dirs__": {k: sorted(v) for k, v in manifest.items() if v},
            }
            path = self.__manifest_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error("STRM生成：保存所有权清单失败：%s", e)

    @staticmethod
    def __add_owned(owned: Set[str], rel_dir_str: str) -> None:
        """将相对目录及其所有父级相对目录记入插件镜像所有权集合。"""
        if not rel_dir_str or rel_dir_str == ".":
            return
        parts = [p for p in rel_dir_str.split("/") if p and p != "."]
        acc = ""
        for p in parts:
            acc = f"{acc}/{p}" if acc else p
            owned.add(acc)

    @staticmethod
    def __remove_owned_prefix(owned: Set[str], deleted_rel_dir: str) -> None:
        """当某个镜像目录被整目录删除后，从所有权清单中同步移除该目录及其子目录记录。"""
        prefix = deleted_rel_dir + "/"
        to_discard = {d for d in owned if d == deleted_rel_dir or d.startswith(prefix)}
        owned.difference_update(to_discard)

    def __cleanup_orphans(
        self,
        dst_root: Path,
        src_dirs: Set[str],
        expected_strms: Set[str],
        owned: Set[str],
        stat: dict,
    ) -> None:
        """清理孤儿文件与目录（仅在 MODE_SYNC 下调用）：
        1. 若源目录对应的子文件夹仍然存在：
           - 只删除已不在 expected_strms 中的孤儿 .strm 文件；
           - 文件夹内的 json/jpg/srt/nfo 等一切非 .strm 文件（无论复制来的还是手动放入的）全部保留。
        2. 若源目录对应的子文件夹已被删除：
           - 若该子文件夹在插件 owned 清单中（或里面含有 .strm 文件），则整目录连同里面的 json/jpg/srt/strm 一并删除；
           - 若是用户自己建的且不含 .strm 的无关文件夹，则保留不动。
        3. 目标根目录 dst_root 本身永不删除。
        """
        if not dst_root.is_dir():
            return

        to_remove_dirs: List[Tuple[Path, str]] = []
        orphan_strm_files: List[Path] = []

        for dirpath, dirnames, filenames in os.walk(dst_root):
            if self._event.is_set():
                return

            rel_dir = os.path.relpath(dirpath, dst_root).replace("\\", "/")
            if rel_dir == ".":
                rel_dir = ""

            if rel_dir in src_dirs:
                # 源文件夹仍然存在：只清理源视频已被删除的孤儿 .strm，其余附属文件一律不动
                for fn in filenames:
                    if fn.lower().endswith(".strm"):
                        rel_file_str = f"{rel_dir}/{fn}" if rel_dir else fn
                        if rel_file_str not in expected_strms:
                            orphan_strm_files.append(Path(dirpath) / fn)
            else:
                # 源文件夹已不存在
                if rel_dir == "":
                    # 目标根目录本身不删除，仅清理根目录下的孤儿 .strm
                    for fn in filenames:
                        if fn.lower().endswith(".strm"):
                            if fn not in expected_strms:
                                orphan_strm_files.append(Path(dirpath) / fn)
                elif rel_dir in owned or any(fn.lower().endswith(".strm") for fn in filenames):
                    # 属于本插件镜像生成的目录（或包含 strm 的目录）：源文件夹已删，连同内部 json/jpg/srt 等整目录删除
                    to_remove_dirs.append((Path(dirpath), rel_dir))
                    dirnames[:] = []  # 整目录删除，无需继续向下遍历子目录

        # 执行孤儿 .strm 删除
        for target_file in orphan_strm_files:
            try:
                target_file.unlink()
                stat["delete"] += 1
                logger.info("STRM生成：删除孤儿 strm 文件 %s", target_file)
            except OSError as e:
                logger.warning("STRM生成：删除孤儿 strm 失败 %s：%s", target_file, e)
                stat["error"] += 1

        # 执行已删源文件夹对应的目标子目录整目录删除
        for target_dir, rel_dir in to_remove_dirs:
            try:
                shutil.rmtree(target_dir)
                self.__remove_owned_prefix(owned, rel_dir)
                stat["delete"] += 1
                logger.info("STRM生成：源文件夹已删除，同步清理目标文件夹 %s", target_dir)
            except OSError as e:
                logger.warning("STRM生成：删除目标文件夹失败 %s：%s", target_dir, e)
                stat["error"] += 1

    # ====================== 前端页面 ======================
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
                                    {"component": "VSwitch", "props": {"model": "monitor", "label": "开启监控（定时同步）"}},
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
                                            "label": "一次性运行模式（影响「立即生成一次」）",
                                            "items": [
                                                {"title": "查漏补缺（只生成缺失的 strm 与附属文件）", "value": "missing"},
                                                {"title": "覆盖生成（重新生成所有 strm 并覆盖附属文件）", "value": "overwrite"},
                                                {"title": "监控同步（补缺失+更新较新附属文件+清理已删源孤儿）", "value": "sync"},
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
                                            "label": "监控扫描间隔（分钟）",
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
                                            "label": "目录配置（每行一个目录对：本地媒体源路径#strm生成路径）",
                                            "rows": 10,
                                            "placeholder": "/media/video1/movie/外语电影#/media/nvme/nvme-video/nvme1/movie/外语电影/video1",
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
                                            "label": "STRM生成视频格式（逗号分隔，仅此类格式生成.strm）",
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
                                            "label": "同步复制格式（逗号分隔，字幕/海报/nfo/json等直接复制）",
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
                                            "text": (
                                                "1. 监控需「启用插件」与「开启监控」同时打开才生效；仅需手动执行时，选好模式打开「立即生成一次」保存即可。\n"
                                                "2. 监控同步规则：源文件修改时间比目标文件更新时自动覆盖附属文件（nfo/jpg/srt/json等）。\n"
                                                "3. 安全删除边界：源文件夹仍在时，只删除源视频已删的孤儿 .strm，目标文件夹内的 json/jpg/srt/nfo 等一律保留；"
                                                "源文件夹被整个删除时，对应的目标 strm 文件夹（连同里面的 json/jpg/srt 等）会一并删除。删掉上方配置行不会影响已生成的 strm。"
                                            ),
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
            f"监控状态：已开启（每 {self._monitor_interval} 分钟自动同步一次）"
            if monitor_on
            else "监控状态：未开启（需「启用插件」+「开启监控」同时打开）"
        )
        if not status:
            text = (
                f"{monitor_line}\n"
                "尚未运行过。可选择运行模式后打开「立即生成一次」保存执行。"
            )
        else:
            total = status.get("total") or {}
            elapsed = status.get("elapsed") or "-"
            text = (
                f"{monitor_line}\n"
                f"上次运行：{status.get('time', '-')}（模式：{self.__mode_label(status.get('mode'))}，耗时：{elapsed}）\n"
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