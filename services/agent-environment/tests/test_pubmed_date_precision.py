"""PubMed fields must retain actual precision and distinguish publication events."""
import json
import xml.etree.ElementTree as ET

import pytest

from agent_environment.pubmed_mcp_compat import _metadata, _publication_date


@pytest.mark.parametrize('xml,expected', [
    ('<Year>2020</Year><Month>Mar</Month><Day>11</Day>', ('2020-03-11', 'day')),
    ('<Year>2020</Year><Month>03</Month>', ('2020-03', 'month')),
    ('<Year>2020</Year>', ('2020', 'year')),
    ('<Year>2024</Year><Month>Feb</Month><Day>29</Day>', ('2024-02-29', 'day')),
    ('<Year>2023</Year><Month>Feb</Month><Day>29</Day>', ('2023 Feb 29', 'partial')),
    ('<Year>2024</Year><Season>Winter</Season>', ('2024 Winter', 'partial')),
    ('<MedlineDate>1998 Dec-1999 Jan</MedlineDate>', ('1998 Dec-1999 Jan', 'unstructured')),
    ('<MedlineDate>2024</MedlineDate>', ('2024', 'year')),
    ('<Month>Jan</Month><Day>2</Day>', ('Jan 2', 'partial')),
])
def test_real_precision_without_defaulting_missing_parts(xml, expected):
    assert _publication_date(ET.fromstring('<PubDate>' + xml + '</PubDate>')) == expected


def test_no_date_does_not_fabricate_year():
    assert _publication_date(None) == ('No publication date available', 'missing')


def test_issue_and_electronic_dates_remain_separate_with_original_parts():
    article = ET.fromstring('''<PubmedArticle><MedlineCitation><PMID>1</PMID><Article>
    <ArticleTitle>Example</ArticleTitle><Journal><Title>Journal</Title><JournalIssue>
    <PubDate><Year>2020</Year><Month>Apr</Month></PubDate></JournalIssue></Journal>
    <ArticleDate DateType="Electronic"><Year>2020</Year><Month>03</Month><Day>11</Day></ArticleDate>
    </Article></MedlineCitation></PubmedArticle>''')
    row = _metadata(article)
    assert row['Publication Date'] == '2020-04'
    assert row['Publication Date Precision'] == 'month'
    assert json.loads(row['Publication Date Raw']) == {'Year': '2020', 'Month': 'Apr'}
    assert json.loads(row['Article Dates']) == [{'type': 'Electronic', 'date': '2020-03-11', 'precision': 'day'}]
    assert all(isinstance(v, str) for v in row.values())
