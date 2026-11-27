from datetime import date
from pathlib import Path

import pytest
import yaml
from regagent_knowledge.manifest import ManifestEntry
from regagent_knowledge.normalize import split_clauses
from regagent_knowledge.parsing import DoclingParser
from regagent_knowledge.pipeline import PipelineDeps, process_job
from regagent_knowledge.references import find_document_references
from regagent_knowledge.watcher import scan

from .test_pipeline import LEVELS, Folder, Rag
from .test_pipeline import environment as environment

ROOT = Path(__file__).resolve().parents[3]
SUBCONTRACT = "Регламент подбора субподрядчика"
MARGIN = "Регламент контроля маржинальности сделок"


def entry(doc_id: str, title: str) -> ManifestEntry:
    return ManifestEntry(
        file=doc_id + ".docx",
        doc_id=doc_id,
        title=title,
        areas=["общий"],
        access="public",
        version=1,
        effective_from=date(2026, 9, 1),
    )


@pytest.mark.parametrize(
    "mention",
    [
        "регламент подбора субподрядчика",
        "регламентом подбора субподрядчика",
        "описано в регламенте подбора субподрядчика",
    ],
)
def test_declension_resolves_to_same_document(mention: str) -> None:
    refs = find_document_references(
        "source", split_clauses(f"1. См. {mention}."), [entry("sub", SUBCONTRACT)]
    )
    assert [(link.doc_id, link.clause) for link in refs.links] == [("sub", "1")]
    assert not refs.missing


def test_ambiguous_reference_belongs_to_child_clause_only() -> None:
    refs = find_document_references(
        "source",
        split_clauses("1. Раздел\n1.1. Согласно регламенту подбора субподрядчика."),
        [entry("a", SUBCONTRACT), entry("b", SUBCONTRACT)],
    )
    assert not refs.links
    [ambiguous] = refs.needs_review
    assert ambiguous.clause == "1.1" and ambiguous.candidates == ["a", "b"]


def test_reference_mentions_in_demo_samples() -> None:
    corpus = [entry("subcontractor-selection", SUBCONTRACT), entry("deal-margin-control", MARGIN)]
    parser = DoclingParser()
    references = {}
    for stem in ("dbrct-project-activity", "dib-presale-activity", "dib-project-activity"):
        clauses = split_clauses(parser.parse(ROOT / "data/samples" / f"{stem}.docx"))
        references[stem] = find_document_references(stem, clauses, corpus)
    assert ("subcontractor-selection", "4.1") in {
        (link.doc_id, link.clause) for link in references["dbrct-project-activity"].links
    }
    assert ("deal-margin-control", "14.4") in {
        (link.doc_id, link.clause) for link in references["dib-presale-activity"].links
    }
    missing = {
        mention.clause: mention.via.casefold()
        for mention in references["dib-project-activity"].missing
    }
    assert "инструкции по работе с системой управления проектами" in missing["4.4"]
    assert "регламенте контроля поставок в проектах" in missing["5.2.2"]
    assert "регламенту привлечения подрядчиков на коммерческие проекты" in missing["7.4.1"]


async def test_indexing_writes_links_to_each_revision(
    environment: tuple[Folder, PipelineDeps, Rag],
) -> None:
    folder, deps, _ = environment
    manifest = folder.path / "manifest.yaml"
    for version, target, title in [(3, "sub", SUBCONTRACT), (4, "margin", MARGIN)]:
        folder.manifest(version)
        data = yaml.safe_load(manifest.read_text())
        data["documents"].append(entry(target, title).model_dump(mode="json"))
        manifest.write_text(yaml.safe_dump(data, allow_unicode=True))
        (folder.path / f"{target}.docx").write_text("1. Описание")
        folder.document(f"1. Порядок установлен в документе «{title}».")
        job = next(
            job for job in await scan(folder.path, LEVELS, deps.registry) if job.doc_id == "a"
        )
        await process_job(job, deps)
        result = deps.okf.read_regulation("a", version)
        assert result is not None
        assert result.entry_meta["links"] == [{"doc_id": target, "clause": "1", "via": title}]

    old = deps.okf.read_regulation("a", 3)
    new = deps.okf.read_regulation("a", 4)
    assert old is not None and old.entry_meta["links"][0]["doc_id"] == "sub"
    assert new is not None and new.entry_meta["links"][0]["doc_id"] == "margin"
