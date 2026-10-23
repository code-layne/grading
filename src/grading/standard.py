"""Standard API calls, several at once.

The pipeline decides what to send and stores every result, on its own
thread, the only one that touches the database (Pipeline._send_standard).
This module is what runs beside it:

  Limiter     how many calls may be on the wire, lowered by rate limits, and
              when the next may start (the server's Retry-After)
  send        one call from a worker thread, retried while sending it again
              may work
  Workers     the daemon threads that run send()
  Progress    a line per pass at least every PROGRESS_SECONDS, and a final one
  Interrupts  Ctrl-C asks the run to stop: nothing new is sent, and the calls
              in flight are waited for and stored. A second Ctrl-C abandons them.
"""

import queue
import signal
import threading
import time

from .apiclient import ApiError, Outcome, RateLimited, Retryable

PROGRESS_SECONDS = 15
RATE_LIMIT_RETRIES = 10     # per call; after that it waits for the next run
MAX_BACKOFF_SECONDS = 60
PASSES = ("read", "rubric", "verify")


def backoff(attempt: int) -> float:
    """1, 2, 4, ... seconds, at most MAX_BACKOFF_SECONDS: the wait when the
    server gave no Retry-After."""
    return float(min(MAX_BACKOFF_SECONDS, 2 ** (attempt - 1)))


def duration(seconds: float) -> str:
    """52s, 1m 52s, 2h 04m."""
    s = int(max(seconds, 0))
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m {s:02d}s" if m else f"{s}s"


class Stopped(Exception):
    """The run is stopping: a call that hasn't gone out yet is not sent."""


class Limiter:
    """The gate every attempt at a call passes through: at most `limit` calls
    on the wire at once, none before `not_before`, and none once the run is
    stopping. Each rate limit pushes not_before out by its Retry-After, and
    the first rate limit hit by a call started since the last cut halves the
    limit (down to 1) for the rest of the run, so eight calls limited at once
    cut it once, not three times. A rate-limited call goes back through the
    gate, so the cut applies to retries too."""

    def __init__(self, limit: int, clock=time.monotonic):
        self.limit = limit
        self.generation = 0
        self.not_before = 0.0
        self.active = 0
        self.stopped = False
        self.clock = clock
        self._cond = threading.Condition()

    def acquire(self) -> int:
        """Wait for a free slot; returns the generation the call starts in."""
        with self._cond:
            while True:
                if self.stopped:
                    raise Stopped
                wait = self.not_before - self.clock()
                if wait <= 0 and self.active < self.limit:
                    self.active += 1
                    return self.generation
                self._cond.wait(wait if wait > 0 else None)

    def release(self):
        with self._cond:
            self.active -= 1
            self._cond.notify_all()

    def throttle(self, generation: int, wait: float) -> int | None:
        """A call started in `generation` was rate limited. Returns the new
        limit when this lowered it."""
        with self._cond:
            self.not_before = max(self.not_before, self.clock() + wait)
            self._cond.notify_all()
            if generation != self.generation or self.limit == 1:
                return None
            self.generation += 1
            self.limit //= 2
            return self.limit

    def stop(self):
        with self._cond:
            self.stopped = True
            self._cond.notify_all()


def send(transport, limiter: Limiter, key, params, meta, *, sleep, max_retries: int, note) -> Outcome | None:
    """One standard call, from a worker thread; None when the run stopped
    before it went out. A rate limit (429, or overloaded) waits out the
    Retry-After and lowers the limit, up to RATE_LIMIT_RETRIES times; after
    that the call comes back `rate-limited`, which leaves it for the next run.
    A connection error or a 5xx is sent again up to max_retries times, then
    stops the run."""
    limited = failed = 0
    while True:
        try:
            generation = limiter.acquire()
        except Stopped:
            return None
        try:
            return transport.create(key, params, meta)
        except RateLimited as e:
            err, limited = e, limited + 1
            wait = e.retry_after if e.retry_after is not None else backoff(limited)
            # Before the slot is freed, so that no call slips in ahead of the wait.
            lowered = limiter.throttle(generation, wait)
        except Retryable as e:
            err = e
        finally:
            limiter.release()
        if isinstance(err, RateLimited):
            if limited > RATE_LIMIT_RETRIES:
                return Outcome(key, None, f"rate-limited {err}")
            if lowered is not None:
                note(f"{err}: waiting {wait:g}s, then {lowered} calls at a time for the rest of the run")
            continue
        failed += 1
        if failed > max_retries:
            raise ApiError(f"{err}, {max_retries} retries; rerun to resume")
        sleep(err.retry_after if err.retry_after is not None else backoff(failed))


class Workers:
    """n daemon threads running fn(key, params, meta). Results come back on
    one queue as (kind, key, value): ("done", key, outcome or None when it
    was never sent), ("error", key, exception) or ("note", None, text).
    Daemon threads, so a second Ctrl-C exits without waiting for calls still
    in flight."""

    def __init__(self, n: int, fn):
        self.fn = fn
        self.jobs = queue.Queue()
        self.results = queue.Queue()
        self.threads = [threading.Thread(target=self._run, daemon=True, name=f"grading-call-{i}")
                        for i in range(n)]
        for t in self.threads:
            t.start()

    def _run(self):
        while True:
            job = self.jobs.get()
            if job is None:
                return
            try:
                self.results.put(("done", job[0], self.fn(*job)))
            except BaseException as e:  # handed to the pipeline's thread, which stops the run
                self.results.put(("error", job[0], e))

    def submit(self, key, params, meta):
        self.jobs.put((key, params, meta))

    def note(self, text: str):
        self.results.put(("note", None, text))

    def get(self, timeout: float):
        try:
            return self.results.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        for _ in self.threads:
            self.jobs.put(None)


class Progress:
    """read: 41 of 95 done, 0 failed, 1m 52s, $0.83 so far

    One line per pass at least every PROGRESS_SECONDS, for every pass with
    calls done or still to come, and a final line when a pass has nothing
    left. The total is the calls done plus those still to come as far as is
    known now: until the reads are in, a rubric call is counted for every
    rubric crop, so the rubric and verify totals can only come down. Times
    count from the start of the run."""

    def __init__(self, out, clock):
        self.out, self.clock = out, clock
        self.start = self.last = clock()
        self.done = {p: 0 for p in PASSES}
        self.failed = {p: 0 for p in PASSES}
        self.usd = {p: 0.0 for p in PASSES}
        self.finished = set()

    def returned(self, call_type: str, ok: bool, usd: float | None):
        self.done[call_type] += 1
        self.failed[call_type] += not ok
        self.usd[call_type] += usd or 0.0

    def _line(self, p, remaining, end=""):
        spent = f"${self.usd[p]:.2f}" + ("" if end else " so far")
        return (f"{p}: {self.done[p]} of {self.done[p] + remaining} done, {self.failed[p]} failed, "
                f"{duration((self.clock() - self.start).total_seconds())}, {spent}{end}")

    def update(self, remaining: dict):
        """remaining: {pass: calls in flight or still to come}."""
        for p in PASSES:
            if p not in self.finished and self.done[p] and not remaining[p]:
                self.finished.add(p)
                self.out(self._line(p, 0, "; finished"))
        now = self.clock()
        if (now - self.last).total_seconds() < PROGRESS_SECONDS:
            return
        self.last = now
        for p in PASSES:
            if p not in self.finished and (self.done[p] or remaining[p]):
                self.out(self._line(p, remaining[p]))

    def end(self):
        """The run sent all it could: a final line for any pass that has none yet."""
        for p in PASSES:
            if p not in self.finished and self.done[p]:
                self.finished.add(p)
                self.out(self._line(p, 0, "; finished"))

    def stopped(self, remaining: dict):
        for p in PASSES:
            if p not in self.finished and (self.done[p] or remaining[p]):
                self.out(self._line(p, remaining[p], "; stopped"))

    def timeout(self) -> float:
        """How long the pipeline's thread may wait for a result: until the
        next progress line, and never more than a second, so that Ctrl-C is
        noticed promptly."""
        left = PROGRESS_SECONDS - (self.clock() - self.last).total_seconds()
        return min(1.0, max(left, 0.01))


class Interrupts:
    """Inside the block, the first Ctrl-C only sets .requested; the second
    raises KeyboardInterrupt as usual. Off the main thread (where signal
    handlers can't be set) Ctrl-C keeps its usual meaning."""

    def __init__(self):
        self.requested = False
        self._installed = False
        self._prev = None

    def __enter__(self):
        if threading.current_thread() is threading.main_thread():
            self._prev = signal.signal(signal.SIGINT, self._handle)
            self._installed = True
        return self

    def _handle(self, signum, frame):
        if self.requested:
            raise KeyboardInterrupt
        self.requested = True

    def __exit__(self, *exc):
        if self._installed:
            signal.signal(signal.SIGINT, signal.SIG_DFL if self._prev is None else self._prev)
        return False
