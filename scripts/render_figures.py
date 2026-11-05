"""Render the published aggregate comparison. No model or private data access."""
from pathlib import Path
import json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

root = Path(__file__).resolve().parents[1]
data = json.loads((root / "benchmarks/school-82.json").read_text())
total = data["dataset"]["works"]
rows = [data["baseline"], data["deepseek_reader_experiment"]]
labels = ["Основной маршрут\nоктябрь 2026", "Другой читатель\n4 ноября 2026"]
colors = ["#334dcc", "#eb9c98", "#dfe4ee"]
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 12,
                     "axes.spines.top": False, "axes.spines.right": False,
                     "axes.spines.left": False, "axes.spines.bottom": False})
fig, ax = plt.subplots(figsize=(12, 4.4), facecolor="#f8f9ff")
ax.set_facecolor("#f8f9ff")
for index, row in enumerate(rows):
    counts = [row["matches"], row["higher"] + row["lower"], row["unscored"]]
    assert sum(counts) == total
    start = 0
    y = 1 - index
    for count, color in zip(counts, colors):
        ax.barh(y, count, left=start, height=.38, color=color)
        ax.text(start + count / 2, y, str(count), ha="center", va="center",
                color="white" if color == colors[0] else "#222b42", fontweight="bold", fontsize=16)
        start += count
ax.set_yticks([1, 0], labels, color="#222b42")
ax.tick_params(axis="y", length=0, pad=16)
ax.set_xlim(0, total)
ax.set_ylim(-.6, 1.7)
ax.set_xticks([0, 20, 40, 60, 82])
ax.tick_params(axis="x", length=0, labelcolor="#66718a")
ax.grid(axis="x", color="#e9ecf5", zorder=0)
ax.set_axisbelow(True)
ax.set_title("Как распределились все 82 работы", loc="left", pad=23,
             fontsize=21, color="#202b49", fontweight="bold")
ax.legend(handles=[Patch(color=c, label=l) for c, l in zip(colors,
          ["Балл совпал", "Балл разошёлся", "Без балла"])],
          loc="upper left", bbox_to_anchor=(-.01, -.18), ncol=3,
          frameon=False, labelcolor="#39435d")
fig.text(.03, .025, "Известный набор разработки · только №15 · совпадение балла не гарантирует верный разбор",
         color="#66718a", fontsize=10)
fig.subplots_adjust(left=.24, right=.97, top=.79, bottom=.28)
assets = root / "docs/assets"
assets.mkdir(parents=True, exist_ok=True)
fig.savefig(assets / "benchmark.png", dpi=180, facecolor=fig.get_facecolor())
plt.close(fig)
print("docs/assets/benchmark.png")
