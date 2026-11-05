"""Check aggregate arithmetic, frozen source hashes and public file boundaries."""
import hashlib
import json
from pathlib import Path
import re
import sys

root = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    data = json.loads((root / "benchmarks/school-82.json").read_text())
    sys.path.insert(0, str(root))
    from contracts.ege_v1.validate import validate
    states = json.loads((root / "contracts/ege_v1/examples/principles_states.json").read_text())
    for case in states["examples"]:
        assert case["input"]["provenance"]["dataset_role"] == "synthetic"
        assert not validate(case["result"], case["input"]), "Invalid synthetic scenario"
    for number in (13, 15):
        source = json.loads((root / f"contracts/ege_v1/examples/input_{number}.json").read_text())
        result = json.loads((root / f"contracts/ege_v1/examples/result_{number}.json").read_text())
        assert source["provenance"]["dataset_role"] == "synthetic"
        errors = validate(result, source)
        assert not errors, f"Invalid synthetic example {number}: {errors}"
    total = data["dataset"]["works"]
    assert sum(data["dataset"]["reference_distribution"].values()) == total
    for name in ("baseline", "deepseek_reader_experiment"):
        row = data[name]
        assert row["matches"] + row["higher"] + row["lower"] == row["scored"]
        assert row["scored"] + row["unscored"] == total
        assert sum(row["unscored_reasons"].values()) == row["unscored"]
    assert data["baseline"]["full_review"]["equal_score_with_other_observations"] + data["baseline"]["full_review"]["no_material_discrepancy_observed"] == data["baseline"]["matches"]
    for manifest in (root / "RELEASE.json", root / "planning/m05/manifest.json"):
        value = json.loads(manifest.read_text())
        for name, expected in value.get("source_files", value.get("files", {})).items():
            assert digest(root / name) == expected, f"Source drift: {name}"
        for name, evidence in value.get("adapted_files", {}).items():
            assert digest(root / name) == evidence["exported_sha256"], f"Adapted source drift: {name}"

    excluded = {".git", ".venv", "__pycache__", "data_local", ".pytest_cache"}
    files = [p for p in root.rglob("*") if p.is_file() and not excluded.intersection(p.relative_to(root).parts)]
    token = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AIza[A-Za-z0-9_-]{35}|sk-[A-Za-z0-9]{32,})")
    for path in files:
        relative = path.relative_to(root)
        assert not path.is_symlink(), f"Symlink: {relative}"
        assert not path.name.startswith(".env"), f"Environment file: {relative}"
        assert path.suffix not in {".sqlite3", ".db", ".pdf", ".zip", ".jpg", ".jpeg", ".webp", ".otf", ".woff", ".woff2"}, f"Excluded asset: {relative}"
        if path.suffix == ".png":
            assert str(relative) == "docs/assets/benchmark.png", f"Unexpected image: {relative}"
        else:
            text = path.read_text()
            assert not token.search(text), f"Credential-like value: {relative}"
            assert not re.search(r"/Users/[^/]+/(?:Documents|\.codex|homework_checker)/", text), f"Personal local path: {relative}"

    docs = [p for p in files if p.suffix == ".md"]
    for doc in docs:
        for target in re.findall(r"\]\(([^)]+)\)", doc.read_text()):
            if re.match(r"[a-z]+:", target) or target.startswith("#"):
                continue
            resolved = doc.parent / target.split("#")[0]
            assert resolved.exists(), f"Broken link in {doc.name}: {target}"
    print(json.dumps(dict(public_files=len(files), arithmetic="passed", source_hashes="passed",
                          privacy_boundaries="passed", documentation_links="passed"), sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
