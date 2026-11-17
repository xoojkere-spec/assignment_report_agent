"""迁移签名覆盖脚本、结构定义及使用的历史归档算法。"""

import json
from pathlib import Path

from assignment_agent.migrations import legacy_v0, v001
from assignment_agent.migrations.catalog import BUSINESS_SQL, SEARCH_INDEX_SQL
from assignment_agent.migrations.runner import Migration


def migrations():
    source = Path(v001.__file__).read_text(encoding="utf-8")
    source += json.dumps(BUSINESS_SQL, sort_keys=True) + SEARCH_INDEX_SQL
    source += Path(legacy_v0.__file__).read_text(encoding="utf-8")
    return [Migration(1, "adopt_existing_business_schema", source, v001.apply)]
