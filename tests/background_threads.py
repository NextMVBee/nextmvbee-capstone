"""An uncaught exception in a background thread fails the suite. Every suite installs the hook at its start and finishes with
finish(): an uncaught exception in any background thread is printed as a FAIL line naming the thread and the exception
(with its traceback on stderr at the moment it happens), and the suite exits nonzero even when every check passed.

finish() first waits for every other non-daemon thread, so a worker that fails late is still counted;
an exception recorded after finish() has returned ends the process nonzero at once, its FAIL line printed.
finish() takes its snapshot of the failures and sets the finished flag in one step under the lock, so no
exception can land between them.

install() also raises the soft descriptor limit to NOFILE (or the hard limit when
lower), because a suite holds every fake runner open until its end (about eight descriptors each) and a large suite
passes the 256 descriptors of a macOS shell."""
import os, resource, sys, threading, traceback

_failures = []
_lock = threading.Lock()
_finished = False
JOIN_SECONDS = 60.0
NOFILE = 4096


def install() -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = NOFILE if hard == resource.RLIM_INFINITY else min(NOFILE, hard)
    if soft < want:
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    def hook(args):
        name = args.thread.name if args.thread is not None else "unknown thread"
        line = f"FAIL  background thread {name}: {args.exc_type.__name__}: {args.exc_value}"
        with _lock:
            _failures.append(line); late = _finished
        print(line, file=sys.stderr)
        traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback, file=sys.stderr)
        if late:                                                                           # after finish(): the only exit path left that can carry the failure
            print(line); print("1 background thread exception(s) after the suite finished: the suite fails"); sys.stdout.flush(); sys.stderr.flush()
            os._exit(1)
    threading.excepthook = hook


def failures() -> list[str]:
    with _lock:
        return list(_failures)


def finish() -> None:
    """After the suite's own PASS lines and count: wait for the other non-daemon threads (bounded), then one FAIL line per
    background exception and exit 1 if there was any."""
    global _finished
    for t in threading.enumerate():
        if t is not threading.current_thread() and not t.daemon:
            t.join(timeout=JOIN_SECONDS)
    with _lock:
        lines = list(_failures); _finished = True                                      # one step: an exception recorded from here on takes the late path above
    for line in lines:
        print(line)
    if lines:
        print(f"{len(lines)} background thread exception(s): the suite fails")
        sys.exit(1)
