"""Keep the pinned upstream year-only metadata contract (offline XML)."""
import xml.etree.ElementTree as ET

from agent_environment.pubmed_mcp_compat import _metadata


def test_metadata_keeps_upstream_year_only_date_contract():
    article = ET.fromstring("""<PubmedArticle><MedlineCitation><PMID>1</PMID><Article>
    <ArticleTitle>Example</ArticleTitle><Journal><Title>Journal</Title><JournalIssue>
    <PubDate><Year>2020</Year><Month>Apr</Month><Day>12</Day></PubDate>
    </JournalIssue></Journal>
    <ArticleDate DateType=\"Electronic\"><Year>2020</Year><Month>03</Month><Day>11</Day></ArticleDate>
    </Article></MedlineCitation></PubmedArticle>""")
    row = _metadata(article)
    assert row["Publication Date"] == "2020"
    assert set(row) == {"PMID", "Title", "Authors", "Journal", "Publication Date", "Abstract"}
