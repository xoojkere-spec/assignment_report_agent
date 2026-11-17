"""跨检索轮次和恢复的 SQLite 搜索额度。"""

from assignment_agent.research_types import ResearchError

CHAPTER_LIMIT = 6
TOTAL_LIMIT = 24


class SearchBudgetReached(ResearchError):
    """下一次搜索无法预占额度。"""


class SearchBudget:
    def __init__(self, store, run_id, chapter=None, *, allow_chapter_overflow=False):
        self.store = store
        self.run_id = run_id
        self.chapter = chapter
        self.allow_chapter_overflow = allow_chapter_overflow

    def usage(self):
        with self.store.connect() as connection:
            return self._usage(connection)

    def _usage(self, connection):
        total, chapter = connection.execute(
            "SELECT COUNT(*), COALESCE(SUM(chapter=?),0) "
            "FROM research_search_attempts WHERE run_id=?",
            (self.chapter, self.run_id),
        ).fetchone()
        return {
            "chapter": self.chapter,
            "chapter_used": chapter if self.chapter is not None else None,
            "chapter_limit": CHAPTER_LIMIT,
            "total_used": total,
            "total_limit": TOTAL_LIMIT,
            "exhausted": total >= TOTAL_LIMIT
            or (
                not self.allow_chapter_overflow
                and self.chapter is not None
                and chapter >= CHAPTER_LIMIT
            ),
        }

    @staticmethod
    def notice(usage):
        chapter = (
            f"第 {usage['chapter']} 章 {usage['chapter_used']}/{usage['chapter_limit']} 次，"
            if usage["chapter"] is not None
            else ""
        )
        return (
            f"搜索预算已用尽（{chapter}全篇 {usage['total_used']}/{usage['total_limit']} 次）；"
            "恢复任务会复用已保存的证据，搜索额度不会重置"
        )

    def reserve(self, channel):
        with self.store.connect() as connection:
            # 检查和预占必须串行；请求失败或发出后进程中断，均不退回额度。
            connection.execute("BEGIN IMMEDIATE")
            usage = self._usage(connection)
            if usage["exhausted"]:
                raise SearchBudgetReached(self.notice(usage))
            connection.execute(
                "INSERT INTO research_search_attempts(run_id,chapter,channel) VALUES (?,?,?)",
                (self.run_id, self.chapter, channel),
            )
