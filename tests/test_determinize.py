import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def test_selftest_passes():
    r = subprocess.run([sys.executable, str(HERE / "determinize.py"), "--selftest"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "selftest PASS" in r.stdout


def test_fixture_report_has_flows(tmp_path):
    r = subprocess.run(
        [sys.executable, str(HERE / "determinize.py"),
         str(HERE / "examples" / "fixture_session.jsonl"), "--out", str(tmp_path)],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    for kind in ("retry_loop", "repeat_read", "readonly_rerun", "env_preamble"):
        assert f"`{kind}`" in report, f"{kind} missing from report"
