"""Small fcntl-compatible file lock for Linux and Windows."""

try:
    import fcntl as fcntl  # type: ignore
except ImportError:  # Windows
    import msvcrt

    class _Fcntl:
        LOCK_EX = 2
        LOCK_NB = 4
        LOCK_UN = 8

        @staticmethod
        def flock(handle, operation):
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write("\0")
                handle.flush()
            handle.seek(0)
            mode = msvcrt.LK_UNLCK if operation & _Fcntl.LOCK_UN else msvcrt.LK_NBLCK
            try:
                msvcrt.locking(handle.fileno(), mode, 1)
            except OSError as exc:
                if operation & _Fcntl.LOCK_UN:
                    raise
                raise BlockingIOError(str(exc)) from exc

    fcntl = _Fcntl()
