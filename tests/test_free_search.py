import pytest

from assignment_agent.free_search import FreeWebSearch
from assignment_agent.research_types import ResearchError


class Network:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        if "duckduckgo" in url:
            raise ResearchError("公开资料服务返回 HTTP 429")
        return (
            url,
            "application/rss+xml",
            (
                b"<rss><channel><item><title>Windows NT microkernel architecture</title>"
                b"<link>https://publisher.example/article</link>"
                b"<description>Windows NT design</description>"
                b"</item></channel></rss>"
            ),
        )

    def json(self, url, params=None):
        self.calls.append(url)
        return {"results": []}


def test_rate_limit_switches_engine_and_cooldown_is_reusable():
    network = Network()
    search = FreeWebSearch(network)
    found = search.search("Windows NT microkernel architecture")
    assert len(found) == 1 and found[0].provider == "bing"
    assert found[0].metadata_pending and not found[0].authors
    assert [e["status"] for e in search.events] == ["failed", "completed", "completed"]
    assert search.events[-1]["engine"] == "microsoft-learn"
    restored = FreeWebSearch(network)
    restored.cooldowns = search.cooldowns
    assert restored.search("Windows NT microkernel architecture")
    assert restored.events[0]["status"] == "cooldown"
    assert sum("duckduckgo" in call for call in network.calls) == 1


def test_empty_results_continue_to_official_site_and_filter_irrelevant_hits():
    class OfficialNetwork(Network):
        def get(self, url, **kwargs):
            self.calls.append(url)
            return url, "text/html", b"<html/>" if "duckduckgo" in url else b"<rss><channel/></rss>"

        def json(self, url, params=None):
            self.calls.append(url)
            return {
                "results": [
                    {
                        "title": "Windows Server kernel architecture",
                        "url": "https://learn.microsoft.com/en-us/example",
                        "description": "Kernel components",
                    },
                    {
                        "title": "Windows wallpaper",
                        "url": "https://irrelevant.example/",
                        "description": "Pictures",
                    },
                ]
            }

    network = OfficialNetwork()
    found = FreeWebSearch(network).search("Windows Server kernel architecture")
    assert len(found) == 1 and found[0].provider == "microsoft-learn"
    assert len(network.calls) == 3


def test_all_channels_blocked_do_not_return_model_inventions():
    class Blocked(Network):
        def get(self, url, **kwargs):
            raise ResearchError("公开资料服务返回 HTTP 429")

    with pytest.raises(ResearchError, match="后备渠道未取得结果"):
        FreeWebSearch(Blocked()).search("science evidence")


def test_rss_external_entity_and_internal_urls_cannot_become_candidates():
    class Unsafe(Network):
        def get(self, url, **kwargs):
            if "duckduckgo" in url:
                raise ResearchError("公开资料服务返回 HTTP 202")
            return (
                url,
                "application/xml",
                b"""<!DOCTYPE rss [<!ENTITY secret SYSTEM "file:///etc/passwd">]>
                <rss><channel><item><title>science evidence &secret;</title>
                <link>http://127.0.0.1/private</link><description>science evidence</description>
                </item></channel></rss>""",
            )

    assert FreeWebSearch(Unsafe()).lookup("bing", "science evidence") == []
    with pytest.raises(ResearchError, match="后备渠道未取得结果"):
        FreeWebSearch(Unsafe()).search("science evidence")
