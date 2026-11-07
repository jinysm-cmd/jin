import subprocess
import sys


def test_second_instance_is_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    code = "import time,sys; sys.path.insert(0,%r); from bot.lock import single_instance; single_instance('t'); print('ok', flush=True); time.sleep(5)"
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    first = subprocess.Popen([sys.executable, "-c", code % root], stdout=subprocess.PIPE, text=True,
                             env={**os.environ, "HOME": str(tmp_path)})
    assert first.stdout.readline().strip() == "ok"
    second = subprocess.run([sys.executable, "-c", code % root], capture_output=True, text=True,
                            env={**os.environ, "HOME": str(tmp_path)}, cwd=str(tmp_path))
    first.kill()
    assert second.returncode != 0 and "이미 같은 봇이 실행 중" in second.stderr
