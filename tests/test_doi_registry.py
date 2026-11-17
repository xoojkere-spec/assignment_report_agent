import pytest

from assignment_agent.research import ResearchCollector
from assignment_agent.research_types import Paper, ResearchError


class Registries:
    def __init__(self, **changes):
        self.changes = changes
        self.requests = []

    def json(self, url):
        self.requests.append(url)
        if "crossref" in url:
            raise ResearchError("公开资料服务返回 HTTP 404")
        return {
            "data": {
                "attributes": {
                    "doi": "10.48550/arxiv.2502.08802",
                    "titles": [{"title": "Microkernel architecture &amp; implementation"}],
                    "publicationYear": 2025,
                    **self.changes,
                }
            }
        }


def paper():
    return Paper(
        title="Microkernel architecture & implementation",
        year=2025,
        doi="10.48550/arxiv.2502.08802",
        url="https://arxiv.org/abs/2502.08802",
        provider="openalex",
    )


def test_datacite_registered_doi_is_checked_when_crossref_does_not_have_it():
    network = Registries()
    ResearchCollector(None, fetcher=network).verify_doi(paper())
    assert len(network.requests) == 2
    assert network.requests[-1] == "https://api.datacite.org/dois/10.48550/arxiv.2502.08802"


@pytest.mark.parametrize(
    "changes",
    [
        {"doi": "10.48550/arxiv.9999.00000"},
        {"titles": [{"title": "Unrelated biology"}]},
        {"publicationYear": 2024},
        {"publicationYear": None},
    ],
)
def test_datacite_mismatch_or_incomplete_metadata_does_not_pass(changes):
    with pytest.raises(ResearchError):
        ResearchCollector(None, fetcher=Registries(**changes)).verify_doi(paper())


def test_crossref_service_failure_is_not_silently_treated_as_another_registry():
    class Offline(Registries):
        def json(self, url):
            assert "crossref" in url
            raise ResearchError("公开资料服务返回 HTTP 429")

    with pytest.raises(ResearchError, match="429"):
        ResearchCollector(None, fetcher=Offline()).verify_doi(paper())
