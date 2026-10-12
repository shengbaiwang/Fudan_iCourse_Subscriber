"""Interrupt optional slides without swallowing the deadline in workers."""
from contextlib import contextmanager
import signal
import threading


class MaterialTimeout(BaseException):
    # Deliberately bypass broad Exception handlers in network/prefetch code.
    # Only LectureRunner's optional-material boundary catches this signal.
    pass


@contextmanager
def material_deadline(seconds):
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "SIGALRM"):
        yield
        return
    previous = signal.getsignal(signal.SIGALRM)

    def expired(_signum, _frame):
        raise MaterialTimeout()

    signal.signal(signal.SIGALRM, expired)
    timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if timer[0] or timer[1]:
            signal.setitimer(signal.ITIMER_REAL, *timer)
