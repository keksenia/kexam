"""A short, entirely synthetic demonstration of guarded stage persistence."""
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.guard_demo import run_demo

with tempfile.TemporaryDirectory(prefix="kexam-demo-") as folder:
    report = run_demo(folder)
print("Искусственные ответы. Вызовов моделей: 0. Расход: 0 ₽.\n")
for name, value in report["scenarios"].items():
    print(f"{name}: {value['first']['status']}; повтор: {value['duplicate']['status']}; "
          f"обращений к искусственному адаптеру: {value['transport_calls']}")
