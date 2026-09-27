---
name: deterministic-cross-process-race
description: Make a flaky cross-process concurrency test (file lock, read-modify-write, atomic-write) deterministic by replacing probabilistic timing overlap with a file-based startup barrier, so a defect like a lost update ALWAYS reproduces.
---

# Deterministic cross-process race reproduction

## When to use
A concurrency test spawns N separate **subprocesses** (or processes) that contend on a
shared resource (a file, a counter, a JSON blob) and the test is **flaky green**: when the
guard/lock is *disabled* (mutation path) the test sometimes still passes because the race
window is too small for the OS scheduler to actually interleave the processes. Example: 8
processes each do read-modify-write on a counter expecting `final == N*iters`; with an
unlocked path and only a tiny `time.sleep(0.01)` in the critical section, the lost update
occasionally does not manifest → guard looks "green" even though the lock is broken.

Do NOT "fix" this by just bumping `sleep` or iteration counts — that only lowers the flake
rate, it never eliminates it. Use a **barrier** to force the interleaving deterministically.

## The pattern (file-based cross-process startup barrier)
Works on Windows/Linux with no extra deps (uses only `os`, `time`, sentinel files).

1. Pass `n_procs` into the child as an argv (the child otherwise doesn't know how many peers
   it must wait for).
2. In the child, BEFORE the RMW loop, create a sentinel file
   `<barrier_dir>/ready_<pid>` and then poll `os.listdir(barrier_dir)` until the count of
   `ready_*` files `>= n_procs` (with a timeout, e.g. 30s, so a dead peer can't hang the run).
3. **Critical detail for determinism**: right after the barrier releases, each child must
   `read` the shared value *first* (front of the critical section), THEN `sleep`, THEN
   `write`. Because every child reads before any child writes (all are sleeping, none has
   written yet), they all capture the *same* stale value and then overwrite each other →
   first iteration alone loses `n_procs-1` updates. The lost update is now **guaranteed**,
   independent of machine load / CPU count / scheduler.
4. Only activate the barrier + sleep on the **mutation/defect path**. The positive (real
   guard) path must NOT enter the barrier and must keep `sleep=0`, so the real lock still
   serializes everything and `final == N` (test stays green when the code is correct).

### Child RMW template (Python)
```python
import contextlib, os, sys, time
repo, lock_path, counter_path, iters, n_procs = sys.argv[1:6]
# ... setup, import file_lock ...
if os.environ.get("MUTATE_DROP_LOCK"):          # only on the defect path
    @contextlib.contextmanager
    def file_lock(*a, **k): yield
    _mut_sleep = 0.03                            # widen critical section
else:
    _mut_sleep = 0                               # real path: no barrier, no sleep

_barrier_dir = None
if _mut_sleep:                                  # barrier ONLY on mutation path
    _barrier_dir = os.path.join(os.path.dirname(lock_path), ".barrier")
    os.makedirs(_barrier_dir, exist_ok=True)
    open(os.path.join(_barrier_dir, "ready_%d" % os.getpid()), "w").write("1")
    deadline = time.time() + 30
    while time.time() < deadline:
        if len([n for n in os.listdir(_barrier_dir) if n.startswith("ready_")]) >= int(n_procs):
            break
        time.sleep(0.005)

for _ in range(int(iters)):
    with file_lock(lock_path):
        v = 0
        if os.path.exists(counter_path):
            v = int(open(counter_path).read().strip() or "0")
        if _mut_sleep:
            time.sleep(_mut_sleep)               # read happens BEFORE this sleep
        open(counter_path, "w").write(str(v + 1))
```

## Verification checklist
- Positive (real lock): run standalone → must still PASS, `final == N*iters`.
- Mutation (lock dropped + barrier): run the guard **≥3 times** and/or in a loop of 6 →
  must turn RED every single time (`final < expected`). No single green run allowed.
- Then run it inside the full suite once to confirm it doesn't disturb the overall count.

## Why this beats the naive approach
A bare `sleep` only *widens* a window; whether two processes actually overlap is still a
scheduling coin-flip that fails under light load or a fast machine. The barrier forces a
**synchronized entry** so overlap is structural, not probabilistic. This is the same reason
the codebase's in-process tests use `threading.Barrier` — here we replicate it across
processes with sentinel files because there is no shared `threading` object.

## Caveats
- Sentinel files live in a temp dir; they vanish with the tempdir, no cleanup needed, but
  the barrier dir must be writable by all child processes (put it next to `lock_path`).
- Always add a timeout on the poll so a crashed child can't deadlock the suite.
- Keep the positive path 100% untouched — the goal is only to make the *defect* reproducible,
  never to change what "correct" looks like.
- Windows file semantics: `os.replace` rename storms / transient `OSError` during concurrent
  open are noise, count them separately and do NOT treat them as failures.
