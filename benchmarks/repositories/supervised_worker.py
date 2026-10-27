"""Bounded fixed workers that inherit the approved oracle's process group/job.

Do not create a nested session: the outer Sprig command must own all ordinary
worker and multiprocessing descendants, including when its supervisor is killed.
This helper is intended only for a fixed oracle invoked through Tools.run.
"""

import math
import os
import subprocess
import threading
import time


def run_worker(argv, *, env, timeout, max_output=65536):
    if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120
            or type(max_output) is not int or not 1 <= max_output <= 1048576):
        raise ValueError("invalid fixed-worker limits")
    if os.name != "nt" and os.getpgrp() != os.getpid():
        raise ValueError("fixed oracle must be invoked as a supervised process-group leader")
    process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, shell=False, start_new_session=False)
    output = bytearray()
    done, limited, failed = threading.Event(), threading.Event(), threading.Event()
    def read():
        try:
            while True:
                block = os.read(process.stdout.fileno(), 8192)
                if not block:
                    break
                output.extend(block[:max_output + 1 - len(output)])
                if len(output) > max_output:
                    limited.set()
                    break
        except (OSError, ValueError):
            failed.set()
        finally:
            done.set()
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    timed_out, pipe_open = False, False
    try:
        while process.poll() is None:
            if limited.is_set():
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.02)
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        if not done.wait(max(0, deadline - time.monotonic())):
            pipe_open = True
        body = bytes(output[:max_output])
        result = {"returncode": process.returncode, "timed_out": timed_out, "output_limited": limited.is_set()}
        if pipe_open or failed.is_set():
            result["error"] = "fixed_worker_capture_incomplete"
        try:
            result["output"] = body.decode("utf-8")
        except UnicodeError:
            result.update(error="fixed_worker_invalid_utf8", output="")
        return result
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=2)
        # Closing a pipe while another thread is in ReadFile can block on
        # Windows when a descendant still owns its writer. Let the oracle exit
        # and its outer group/job reap those descendants; never wait unbounded.
        if done.is_set():
            process.stdout.close()
