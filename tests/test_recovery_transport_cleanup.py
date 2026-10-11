"""Failure cleanup cannot leave a generated TERM-resistant probe running."""
import importlib.util
from pathlib import Path
import select
import signal
import subprocess
import sys
import time
import unittest

spec = importlib.util.spec_from_file_location(
    'recovery_transport_cleanup_target',
    Path(__file__).with_name('recovery_transport_integration.py'))
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


@unittest.skipUnless(hasattr(signal, 'SIGKILL'), 'POSIX process signals required')
class ProbeCleanupTests(unittest.TestCase):
    def test_term_resistant_child_is_killed_reaped_and_pipes_closed(self):
        source = ('import signal,time\n'
                  'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                  'print("READY", flush=True)\n'
                  'time.sleep(60)\n')
        child = subprocess.Popen([sys.executable, '-c', source],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ready, _, _ = select.select([child.stdout], [], [], 3)
            self.assertTrue(ready, 'Generated child did not become ready')
            self.assertEqual(child.stdout.readline().strip(), 'READY')
            started = time.monotonic()
            harness.terminate_and_reap(child, timeout=.1)
            elapsed = time.monotonic() - started
            self.assertEqual(child.returncode, -signal.SIGKILL)
            self.assertLess(elapsed, 1.5)
            self.assertTrue(child.stdout.closed)
            self.assertTrue(child.stderr.closed)
            # wait() returns the already collected status, without another child.
            self.assertEqual(child.wait(timeout=.1), -signal.SIGKILL)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            for pipe in (child.stdout, child.stderr):
                if pipe is not None:
                    pipe.close()


if __name__ == '__main__':
    unittest.main()
