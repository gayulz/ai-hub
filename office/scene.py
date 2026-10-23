"""선택한 루프탑 장면의 고정된 방·역할·외형·자리."""

import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def scene() -> dict:
	return json.loads(Path(__file__).with_name("static").joinpath("scene.json").read_text(encoding="utf-8"))
