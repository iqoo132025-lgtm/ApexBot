"""
قفل نسخة واحدة لحلقة الإنتاج (`run_top100.py --loop`).

القفل قفل نظام تشغيل على ملف بجانب قاعدة البيانات (`<db>.lock`):
msvcrt.locking على ويندوز، و fcntl.flock على غيره. النظام يحرّره حين تنتهي
العملية بأي طريقة — خروج عادي أو استثناء أو قتل أو انقطاع كهرباء — فلا يبقى
قفل ميت يمنع الاستعادة بعد انهيار. محتوى الملف (PID ووقت البدء) للتشخيص فقط؛
وجود الملف وحده لا يعني أن حلقة تعمل.

لا يلمس قاعدة البيانات ولا مخططها، ولا يحتاج شبكة ولا مكتبة خارجية.
"""
import os
import sys
import time
from typing import Optional

# EX_TEMPFAIL: نسخة أخرى تملك القفل — ليس خطأ، فلا شيء يجب إعادة تشغيله
ALREADY_RUNNING_EXIT = 75

# البايت 0 هو المقفول؛ معلومات المالك تُكتب بعده ليقرأها فحص الصحة دون أن يصطدم بالقفل
_INFO_OFFSET = 1

if sys.platform == "win32":
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def lock_path_for(db_path: str) -> str:
    return os.path.abspath(db_path) + ".lock"


class InstanceLock:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self._fd: Optional[int] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self) -> bool:
        """True إن أصبح القفل لهذه العملية، False إن كانت عملية أخرى تملكه."""
        if self._fd is not None:
            return True
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        if not _try_lock(fd):
            os.close(fd)
            return False
        info = f"pid={os.getpid()} started={time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
        os.lseek(fd, _INFO_OFFSET, os.SEEK_SET)
        os.write(fd, info.encode("ascii"))
        os.ftruncate(fd, _INFO_OFFSET + len(info))
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            _unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(f"القفل مملوك لعملية أخرى: {self.path}")
        return self

    def __exit__(self, *exc):
        self.release()
