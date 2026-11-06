import shutil
from datetime import time
from pathlib import Path

import pytest

import config

EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"


def test_example_config_is_valid(tmp_path):
    shutil.copy(EXAMPLE, tmp_path / "config.yaml")

    cfg = config.load(tmp_path)

    assert cfg.timezone.key == "Europe/Berlin"
    assert cfg.sync_times == (time(8, 45), time(13, 0), time(21, 0))
    assert cfg.sources["body"] == ["withings", "garmin"]


@pytest.mark.parametrize(
    "sources, message",
    [("bdoy: [garmin]", "Kennzahl"), ("body: [fitbit]", "Quelle")],
)
def test_typos_in_sources_are_rejected(tmp_path, sources, message):
    (tmp_path / "config.yaml").write_text(
        f'timezone: Europe/Berlin\nsync_times: ["08:45"]\nsources:\n  {sources}\n'
    )

    with pytest.raises(ValueError, match=message):
        config.load(tmp_path)
