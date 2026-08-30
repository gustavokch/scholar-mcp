from pathlib import Path

import httpx
import respx

from scholar_mcp.config import Settings
from scholar_mcp.medical.pubmed import MedicalPubMedClient, parse_pubmed_xml
from scholar_mcp.utils.http import AsyncHttpClient
from scholar_mcp.utils.sqlite_cache import SQLiteCacheManager

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

PUBMED_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>12345</PMID>
      <Article>
        <Journal><Title>New England Journal of Medicine</Title></Journal>
        <ArticleTitle>Metformin efficacy in type 2 diabetes</ArticleTitle>
        <Abstract><AbstractText>Biguanide therapy lowers HbA1c.</AbstractText></Abstract>
        <AuthorList>
          <Author><ForeName>John</ForeName><LastName>Smith</LastName></Author>
          <Author><CollectiveName>Diabetes Research Group</CollectiveName></Author>
        </AuthorList>
        <ArticleDate><Year>2021</Year></ArticleDate>
        <ELocationID EIdType="doi">10.1056/NEJMoa2021</ELocationID>
        <ArticleIdList><ArticleId IdType="pmc">PMC7123456</ArticleId></ArticleIdList>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""


async def test_parse_pubmed_xml():
    articles = parse_pubmed_xml(PUBMED_XML)
    assert len(articles) == 1
    a = articles[0]
    assert a.pmid == "12345"
    assert a.title == "Metformin efficacy in type 2 diabetes"
    assert a.abstract == "Biguanide therapy lowers HbA1c."
    assert a.authors == ["John Smith", "Diabetes Research Group"]
    assert a.journal == "New England Journal of Medicine"
    assert a.year == "2021"
    assert a.doi == "10.1056/NEJMoa2021"
    assert a.pmc_id == "7123456"


@respx.mock
async def test_search_articles_flow(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = MedicalPubMedClient(http_client=http_client, cache=cache, settings=settings)

    respx.get(ESEARCH_URL).respond(
        json={"esearchresult": {"idlist": ["12345"]}}
    )
    respx.get(EFETCH_URL).respond(content=PUBMED_XML.encode())

    articles, meta = await client.search_articles("metformin", max_results=5)
    assert len(articles) == 1
    assert articles[0].pmid == "12345"
    assert meta.cached is False

    # Second call hits the cache (no new HTTP traffic needed)
    articles2, meta2 = await client.search_articles("metformin", max_results=5)
    assert meta2.cached is True
    assert articles2[0].to_dict() == articles[0].to_dict()

    await cache.close()
    await http_client.aclose()


@respx.mock
async def test_search_articles_requests_relevance_sort(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = MedicalPubMedClient(http_client=http_client, cache=cache, settings=settings)
    try:
        esearch_route = respx.get(ESEARCH_URL).respond(
            json={"esearchresult": {"idlist": []}}
        )

        await client.search_articles("metformin", max_results=5)
        assert esearch_route.calls.last.request.url.params.get("sort") == "relevance"
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_articles_empty_id_list(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = MedicalPubMedClient(http_client=http_client, cache=cache, settings=settings)

    respx.get(ESEARCH_URL).respond(json={"esearchresult": {"idlist": []}})

    articles, meta = await client.search_articles("nothing")
    assert articles == []
    await cache.close()
    await http_client.aclose()


TWO_ARTICLE_XML = """<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>11111</PMID>
      <Article>
        <Journal><Title>Journal A</Title></Journal>
        <ArticleTitle>Metformin trial one</ArticleTitle>
        <Abstract><AbstractText>First cohort.</AbstractText></Abstract>
        <ArticleDate><Year>2020</Year></ArticleDate>
        <ELocationID EIdType="doi">10.1000/one</ELocationID>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>22222</PMID>
      <Article>
        <Journal><Title>Journal B</Title></Journal>
        <ArticleTitle>Metformin trial two</ArticleTitle>
        <Abstract><AbstractText>Second cohort.</AbstractText></Abstract>
        <ArticleDate><Year>2020</Year></ArticleDate>
        <ELocationID EIdType="doi">10.1000/two</ELocationID>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""


@respx.mock
async def test_search_articles_blends_ncbi_best_match_position(tmp_path: Path):
    """Two articles with identical lexical relevance and year must not score
    equally: the esearch Best Match position is part of the relevance signal."""
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = MedicalPubMedClient(http_client=http_client, cache=cache, settings=settings)
    try:
        respx.get(ESEARCH_URL).respond(
            json={"esearchresult": {"idlist": ["11111", "22222"]}}
        )
        respx.get(EFETCH_URL).respond(content=TWO_ARTICLE_XML.encode())

        articles, _ = await client.search_articles("metformin", max_results=5)
        assert [a.pmid for a in articles] == ["11111", "22222"]
        assert articles[0].score > articles[1].score
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_articles_marks_error_on_esearch_failure(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = MedicalPubMedClient(http_client=http_client, cache=cache, settings=settings)
    try:
        route = respx.get(ESEARCH_URL).mock(side_effect=httpx.ConnectError("boom"))

        articles, meta = await client.search_articles("metformin", max_results=5)
        assert articles == []
        assert meta.error is True

        # The failure must not be cached: a second call re-issues the request.
        # Exact counts are retry-dependent, so only require fresh traffic.
        after_first = route.call_count
        await client.search_articles("metformin", max_results=5)
        assert route.call_count > after_first
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_articles_marks_error_on_efetch_failure(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = MedicalPubMedClient(http_client=http_client, cache=cache, settings=settings)
    try:
        respx.get(ESEARCH_URL).respond(json={"esearchresult": {"idlist": ["12345"]}})
        respx.get(EFETCH_URL).mock(side_effect=httpx.ConnectError("boom"))

        articles, meta = await client.search_articles("metformin", max_results=5)
        assert articles == []
        assert meta.error is True
    finally:
        await cache.close()
        await http_client.aclose()


@respx.mock
async def test_search_articles_leaves_error_false_on_success(tmp_path: Path):
    settings = Settings.load()
    http_client = AsyncHttpClient(settings)
    cache = SQLiteCacheManager(db_path=tmp_path / "cache.db", settings=settings)
    client = MedicalPubMedClient(http_client=http_client, cache=cache, settings=settings)
    try:
        respx.get(ESEARCH_URL).respond(json={"esearchresult": {"idlist": ["12345"]}})
        respx.get(EFETCH_URL).respond(content=PUBMED_XML.encode())

        _, meta = await client.search_articles("metformin", max_results=5)
        assert meta.error is False
    finally:
        await cache.close()
        await http_client.aclose()
