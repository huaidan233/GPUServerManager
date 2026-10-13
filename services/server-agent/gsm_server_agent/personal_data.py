"""Data-disk file operations anchored by directory descriptors (Linux)."""
import base64
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat

CHUNK = 256 * 1024
UPLOAD_LIMIT = 100 * 1024 * 1024


def segments(path):
    if not isinstance(path, str) or len(path) > 2048 or "\\" in path or "\0" in path:
        raise ValueError("文件路径无效。")
    parts = path.split("/") if path else []
    if any(part in {"", ".", ".."} or part.startswith(".gsm-upload-") for part in parts):
        raise ValueError("文件路径无效。")
    return parts


def storage_root():
    value = os.environ.get("GSM_PERSONAL_DATA_ROOT")
    if not value:
        raise ValueError("个人数据目录尚未配置。")
    root = Path(value)
    marker = root / ".gsm-data-volume"
    if not root.is_absolute() or not root.is_dir() or not marker.is_file() or marker.is_symlink():
        raise ValueError("数据盘未就绪，已阻止写入。请管理员检查挂载。")
    expected_device = os.getenv("GSM_PERSONAL_DATA_DEVICE")
    device = root.stat().st_dev
    if root.resolve() != root or (expected_device and expected_device != f"{os.major(device)}:{os.minor(device)}"):
        raise ValueError("数据目录不在指定的数据盘上，已阻止写入。")
    return root


@contextmanager
def directory(username, parts=()):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", username):
        raise ValueError("成员目录无效。")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(storage_root(), flags)
    current = root_fd
    try:
        try:
            os.mkdir(username, 0o750, dir_fd=root_fd)
            os.chown(username, int(os.getenv("GSM_DATA_UID", "10000")), int(os.getenv("GSM_DATA_GID", "10000")), dir_fd=root_fd, follow_symlinks=False)
        except FileExistsError:
            pass
        for part in [username, *parts]:
            child = os.open(part, flags, dir_fd=current)
            if current != root_fd:
                os.close(current)
            current = child
        yield current
    finally:
        if current != root_fd:
            os.close(current)
        os.close(root_fd)


def regular_fd(parent, name, flags):
    fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o640, dir_fd=parent)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise ValueError("只允许普通文件；不支持链接、设备或套接字。")
    return fd


def operate(username, operation, payload):
    parts = segments(payload.get("path", ""))
    with directory(username, parts if operation in {"list", "capacity"} else parts[:-1]) as parent:
        if operation == "capacity":
            disk = os.fstatvfs(parent)
            return {"total_bytes": disk.f_blocks * disk.f_frsize, "available_bytes": disk.f_bavail * disk.f_frsize,
                    "disk_used_bytes": (disk.f_blocks - disk.f_bfree) * disk.f_frsize,
                    "data_root": str(storage_root() / username), "container_path": "/workspace", "mount_ready": True,
                    "private": True}
        if operation == "list":
            entries = []
            with os.scandir(parent) as iterator:
                for entry in iterator:
                    if entry.name.startswith(".gsm-upload-"):
                        continue
                    info = entry.stat(follow_symlinks=False)
                    kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 else "unsupported"
                    entries.append({"name": entry.name, "kind": kind, "size": info.st_size, "modified_at": info.st_mtime})
                    if len(entries) > 2000:
                        raise ValueError("目录超过 2000 项，请使用 SFTP 管理。")
            return {"entries": sorted(entries, key=lambda e: (e["kind"] != "directory", e["name"].lower())),
                    "path": payload.get("path", ""), "container_path": "/workspace" + ("/" + payload["path"] if parts else "")}
        if not parts:
            raise ValueError("请选择文件名或目录名。")
        name = parts[-1]
        if operation == "rename":
            target = segments(payload.get("new_name"))
            if len(target) != 1:
                raise ValueError("新名称不能包含路径。")
            info = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not (stat.S_ISDIR(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)):
                raise ValueError("只允许重命名普通文件或目录。")
            # Linux renameat2 preserves directory anchoring and atomically refuses overwrite.
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            rename = libc.renameat2
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(parent, os.fsencode(name), parent, os.fsencode(target[0]), 1) != 0:
                error = ctypes.get_errno()
                raise OSError(error, os.strerror(error))
            os.fsync(parent)
            return {"renamed": True, "container_path": "/workspace/" + "/".join([*parts[:-1], target[0]])}
        if operation == "mkdir":
            os.mkdir(name, 0o750, dir_fd=parent)
            os.chown(name, int(os.getenv("GSM_DATA_UID", "10000")), int(os.getenv("GSM_DATA_GID", "10000")), dir_fd=parent, follow_symlinks=False)
            return {"created": True}
        if operation == "read":
            fd = regular_fd(parent, name, os.O_RDONLY)
            try:
                offset = int(payload.get("offset", 0))
                if offset < 0:
                    raise ValueError("读取位置无效。")
                info = os.fstat(fd)
                version = f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}:{info.st_size}"
                if payload.get("version") and payload["version"] != version:
                    raise ValueError("文件在下载过程中发生变化，请重试。")
                if info.st_size > UPLOAD_LIMIT:
                    raise ValueError("超过 100 MiB 的文件请使用 SFTP 下载。")
                os.lseek(fd, offset, os.SEEK_SET)
                block = os.read(fd, CHUNK)
                if os.fstat(fd).st_mtime_ns != info.st_mtime_ns:
                    raise ValueError("文件在下载过程中发生变化，请重试。")
                return {"data": base64.b64encode(block).decode(), "offset": offset, "size": info.st_size, "version": version, "eof": offset + len(block) >= info.st_size}
            finally:
                os.close(fd)
        upload_id = payload.get("upload_id", "")
        if not re.fullmatch(r"[a-f0-9]{32}", upload_id):
            raise ValueError("上传标识无效。")
        temporary = ".gsm-upload-" + upload_id
        if operation == "begin":
            if not 0 <= int(payload.get("size", -1)) <= UPLOAD_LIMIT:
                raise ValueError("网页上传上限为 100 MiB，大文件请使用 SFTP。")
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
                raise ValueError("文件已存在，请更改文件名后上传。")
            except FileNotFoundError:
                pass
            fd = regular_fd(parent, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            os.close(fd)
            return {"upload_id": upload_id}
        if operation == "cancel":
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            return {"cancelled": True}
        if operation in {"chunk", "finish"}:
            fd = regular_fd(parent, temporary, os.O_RDWR)
            try:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
                if operation == "chunk":
                    try:
                        block = base64.b64decode(payload.get("data", ""), validate=True)
                    except Exception:
                        raise ValueError("文件块编码无效。") from None
                    size = os.fstat(fd).st_size
                    if len(block) > CHUNK or size != payload.get("offset") or size + len(block) > min(UPLOAD_LIMIT, int(payload.get("size", UPLOAD_LIMIT))):
                        raise ValueError("上传位置或文件块大小无效，请重新上传。")
                    disk = os.fstatvfs(parent)
                    reserve = int(os.getenv("GSM_DATA_RESERVE_BYTES", str(20 * 1024**3)))
                    if disk.f_bavail * disk.f_frsize < reserve + len(block):
                        raise ValueError("数据盘空间不足，已停止写入。")
                    os.lseek(fd, 0, os.SEEK_END)
                    view = memoryview(block)
                    while view:
                        written = os.write(fd, view)
                        view = view[written:]
                    return {"offset": size + len(block)}
                if os.fstat(fd).st_size != payload.get("size"):
                    raise ValueError("文件尚未完整上传。")
                os.fchown(fd, int(os.getenv("GSM_DATA_UID", "10000")), int(os.getenv("GSM_DATA_GID", "10000")))
                os.fsync(fd)
                current = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                opened = os.fstat(fd)
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    raise ValueError("上传临时文件已变化，请重新上传。")
                # Link is an atomic no-overwrite publish; never replace a user's file.
                os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
                os.unlink(temporary, dir_fd=parent)
                os.fsync(parent)
                return {"container_path": "/workspace/" + "/".join(parts)}
            finally:
                os.close(fd)
        raise ValueError("不支持的数据操作。")
