"""Run callables on Krita's GUI thread.

libkis (and Qt underneath it) is not thread safe: touching a Document or Node
from a worker thread corrupts state or crashes outright.  The HTTP bridge
serves requests on worker threads, so every Krita call has to be handed back
to the thread that owns those objects.

The mechanism is a queued signal.  ``MainThreadInvoker`` is constructed on the
GUI thread, so it lives there.  A worker emits ``_submit``; because emitter and
receiver are on different threads Qt delivers it through the GUI thread's event
loop, and the slot therefore runs where it is safe to.  The worker blocks on a
``threading.Event`` until the slot finishes, with a timeout so a stalled GUI
(a modal dialog, say) surfaces as an error rather than a hung socket.
"""

import threading
import traceback

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal


class MainThreadTimeout(Exception):
    """The GUI thread did not run the job before the deadline."""


class MainThreadError(Exception):
    """The job raised on the GUI thread.

    Only a description of the failure is carried across, never the exception
    object. An exception keeps its traceback, the traceback keeps the frames,
    and those frames keep whatever libkis wrappers the failing operation had
    in scope -- Documents and Nodes among them. Holding one of those past the
    point where Krita destroys the underlying C++ object turns the next
    garbage collection into a use-after-free, which showed up as Krita dying
    a few operations after a close. Keeping strings only avoids that entirely.
    """

    def __init__(self, kind, type_name, message, formatted):
        super().__init__(message or type_name)
        self.kind = kind            # OpError.kind, or None for anything else
        self.type_name = type_name
        self.message = message
        self.formatted = formatted


class _Job(object):
    __slots__ = ("fn", "event", "result", "failed", "kind", "type_name",
                 "message", "formatted")

    def __init__(self, fn):
        self.fn = fn
        self.event = threading.Event()
        self.result = None
        self.failed = False
        self.kind = None
        self.type_name = ""
        self.message = ""
        self.formatted = ""


class MainThreadInvoker(QObject):
    _submit = pyqtSignal(object)

    def __init__(self, parent=None):
        super(MainThreadInvoker, self).__init__(parent)
        # Queued explicitly rather than relying on auto-connection, so this
        # keeps working even if a future caller happens to be on the GUI thread.
        self._submit.connect(self._execute, Qt.QueuedConnection)
        self._home_ident = threading.get_ident()
        self._busy = False

    def _execute(self, job):
        if self._busy:
            # Another job is running and is pumping the event loop (the
            # settle after a document close does this), which is how we got
            # delivered mid-operation. Re-entering libkis underneath a call
            # that is already in progress is not safe, so hand the job back
            # to the queue and pick it up once the outer one has finished.
            QTimer.singleShot(0, lambda: self._execute(job))
            return

        self._busy = True
        try:
            job.result = job.fn()
        except BaseException as exc:  # reported to the caller, never swallowed
            job.failed = True
            job.kind = getattr(exc, "kind", None)
            job.type_name = type(exc).__name__
            job.message = str(exc)
            job.formatted = traceback.format_exc()
            # Drop the frames now rather than leaving them for the collector:
            # they may hold libkis wrappers (see MainThreadError).
            exc.__traceback__ = None
        finally:
            self._busy = False
            job.event.set()

    def call(self, fn, timeout=30.0):
        """Run ``fn`` on the GUI thread and return its value.

        Raises MainThreadTimeout if the GUI thread is unresponsive, or
        MainThreadError wrapping whatever ``fn`` raised.
        """
        if threading.get_ident() == self._home_ident:
            # Already home. Emitting would deadlock: a queued signal would not
            # be delivered until we returned to the event loop.
            return fn()

        job = _Job(fn)
        self._submit.emit(job)
        if not job.event.wait(timeout):
            raise MainThreadTimeout(
                "Krita's UI thread did not respond within {0:g}s. It is most "
                "likely blocked by a modal dialog or a long running "
                "operation.".format(timeout)
            )
        if job.failed:
            raise MainThreadError(job.kind, job.type_name, job.message,
                                  job.formatted)
        return job.result
