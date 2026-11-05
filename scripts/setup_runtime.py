"""Install the pinned symbolic worker locally; never calls a model."""
from pathlib import Path
import subprocess
import venv

root = Path(__file__).resolve().parents[1]
target = root / "data_local/live_runtime"
venv.EnvBuilder(with_pip=True).create(target)
subprocess.run([str(target / "bin/python"), "-m", "pip", "install", "-r",
                str(root / "app/condition-requirements.txt")], check=True)
