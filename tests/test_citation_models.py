from scholar_mcp.models import CitationItem, ReferenceItem, RelatedPaper


def test_reference_item_defaults_and_dict():
    ref = ReferenceItem(
        id="ref1",
        title="Attention Is All You Need",
        authors=["Vaswani A", "Shazeer N"],
        year="2017",
        venue="NeurIPS",
        doi="10.48550/arXiv.1706.03762",
    )
    d = ref.to_dict()
    assert d["id"] == "ref1"
    assert d["title"] == "Attention Is All You Need"
    assert d["doi"] == "10.48550/arXiv.1706.03762"
    assert d["pmid"] is None


def test_citation_item_defaults_and_dict():
    cit = CitationItem(
        title="BERT: Pre-training of Deep Bidirectional Transformers",
        authors=["Devlin J"],
        year="2018",
        doi="10.18653/v1/N19-1423",
        citation_count=50000,
    )
    d = cit.to_dict()
    assert d["citation_count"] == 50000
    assert d["title"] == "BERT: Pre-training of Deep Bidirectional Transformers"


def test_related_paper_defaults_and_dict():
    rel = RelatedPaper(
        title="RoBERTa: A Robustly Optimized BERT Approach",
        authors=["Liu Y"],
        year="2019",
        score=98.5,
    )
    d = rel.to_dict()
    assert d["score"] == 98.5
    assert d["pmid"] is None
