"""Validate story campaign JSON files."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def validate_campaign(path: Path) -> list[str]:
    errors: list[str] = []
    data = json.loads(path.read_text(encoding="utf-8"))
    scenes = {scene["id"]: scene for scene in data.get("scenes", [])}
    root = data.get("root_scene")
    if root not in scenes:
        errors.append(f"Root scene '{root}' missing in {path.name}")

    for scene in scenes.values():
        for choice in scene.get("choices", []):
            target = choice.get("next_scene")
            if target not in scenes:
                errors.append(
                    f"Choice '{choice.get('id')}' in scene '{scene['id']}' targets missing scene '{target}'"
                )
            check = choice.get("check")
            if check:
                for field in ("success_scene", "failure_scene"):
                    target_scene = check.get(field)
                    if target_scene and target_scene not in scenes:
                        errors.append(
                            f"Check {field} '{target_scene}' missing (scene '{scene['id']}', choice '{choice.get('id')}')"
                        )
    return errors


def main() -> None:
    base = Path("assets/story")
    if not base.exists():
        print("No story assets found.")
        sys.exit(0)

    errors: list[str] = []
    for path in base.rglob("*.json"):
        errors.extend(validate_campaign(path))

    if errors:
        print("Story validation failed:")
        for err in errors:
            print(f" - {err}")
        sys.exit(1)

    print("All story campaigns look consistent.")


if __name__ == "__main__":
    main()
