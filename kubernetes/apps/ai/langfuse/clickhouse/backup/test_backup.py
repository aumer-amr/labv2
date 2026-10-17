import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class BackupTests(unittest.TestCase):
    def run_backup(self, succeeds):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / "archives"
            backup.mkdir()
            (backup / "latest.zip").write_text("previous completed backup")
            script = root / "backup.sh"
            source = Path(__file__).with_name("backup.sh").read_text()
            script.write_text(source.replace("/var/lib/clickhouse/backups", str(backup)))
            client = root / "clickhouse-client"
            client.write_text(
                "#!" + sys.executable + "\nimport sys,re\nfrom pathlib import Path\n"
                "path = re.search(r\"File\\('([^']+)'\\)\", sys.argv[-1]).group(1)\n"
                "Path(path).write_text('new completed backup')\n"
                "raise SystemExit(" + ("0" if succeeds else "1") + ")\n"
            )
            client.chmod(0o755)
            result = subprocess.run(["/bin/sh", str(script)], env={**os.environ, "PATH": str(root) + ":" + os.environ["PATH"]},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode == 0, succeeds)
            self.assertEqual((backup / "latest.zip").read_text(),
                             "new completed backup" if succeeds else "previous completed backup")
            self.assertEqual(list(backup.glob("pending-*.zip")), [])

    def test_success_atomically_replaces_archive(self):
        self.run_backup(True)

    def test_failed_backup_keeps_last_completed_archive(self):
        self.run_backup(False)


if __name__ == "__main__":
    unittest.main()
