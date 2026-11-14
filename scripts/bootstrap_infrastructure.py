"""Create a container config only on a fresh data volume; never overwrite one."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.shared_config import build_default_config, get_app_config_path


def main():
    path = Path(get_app_config_path())
    if path.exists():
        return
    config = build_default_config()
    example = json.loads((Path(__file__).resolve().parents[1] / "deploy/app_config.example.json").read_text(encoding="utf-8"))
    config["bot"].update(example["bot"])
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as output:
            json.dump(config, output, ensure_ascii=False, indent=2)
    except FileExistsError:
        pass  # another bootstrap owns the existing configuration


if __name__ == "__main__":
    main()
