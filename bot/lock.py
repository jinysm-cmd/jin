"""같은 맥에서 같은 봇이 두 번 켜지는 것을 막는 잠금.

폴더가 달라도 홈 폴더의 잠금 파일 하나를 공유하므로, 두 번째로 켠 봇은 바로 종료된다.
(10월에 4시간봉 봇이 두 폴더에서 동시에 돌아 같은 신호에 두 배로 들어간 사고 재발 방지)
"""
import fcntl
import os

_held = []


def single_instance(name: str):
    path = os.path.join(os.path.expanduser("~"), f".jinbot_{name}.lock")
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        info = fh.read().strip()
        raise SystemExit(f"이미 같은 봇이 실행 중입니다 ({info}). 두 개를 동시에 돌리면 주문이 두 배로 들어갑니다.\n"
                         "먼저 켜 둔 창을 찾아 Control+C 로 끈 뒤 다시 실행하세요.")
    fh.seek(0)
    fh.truncate()
    fh.write(f"pid={os.getpid()} 폴더={os.getcwd()}")
    fh.flush()
    _held.append(fh)   # 프로세스가 살아 있는 동안 잠금 유지
