"""Per-asset range positions must carry asset identity so the LLM can't
mis-attribute one asset's number to another (e.g. SPX shown at QQQ's 93%)."""
import re
from morning_engine import data_assembly


def test_range_line_includes_asset_identity():
    src = open(data_assembly.__file__).read()
    assert re.search(r"\{asset\}[^\n]*20-bar range|range[^\n]*\{asset\}", src), \
        "the 20-bar range line must lead with the asset identity"
