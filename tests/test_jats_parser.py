import re

import pytest

from scholar_mcp.parsers.jats import jats_to_markdown, list_sections, select_sections

XML_TAG_RE = re.compile(r"</?[a-zA-Z][\w:-]*(\s[^<>]*)?/?>")

SAMPLE_JATS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front>
    <article-meta>
      <title-group>
        <article-title>Mechanisms of Cellular Respiration</article-title>
      </title-group>
      <contrib-group>
        <contrib contrib-type="author">
          <name><surname>Curie</surname><given-names>Marie</given-names></name>
        </contrib>
        <contrib contrib-type="author">
          <name><surname>Franklin</surname><given-names>Rosalind</given-names></name>
        </contrib>
      </contrib-group>
      <abstract>
        <p>This study analyzes oxidative phosphorylation in mitochondria.</p>
      </abstract>
      <permissions><license><p>Boilerplate licence text</p></license></permissions>
    </article-meta>
  </front>
  <body>
    <sec sec-type="intro">
      <title>Introduction</title>
      <p>Cellular respiration is vital <xref rid="bib1">[1]</xref>.</p>
      <boxed-text><p>Key insight callout.</p></boxed-text>
      <fig id="f1">
        <label>Figure 1</label>
        <caption><p>Diagram of electron transport chain.</p></caption>
      </fig>
      <table-wrap id="t1">
        <label>Table 1</label>
        <caption><p>Reaction rates.</p></caption>
        <table>
          <tr><th>Complex</th><th>Rate</th></tr>
          <tr><td>Complex I</td><td>12.5</td></tr>
        </table>
      </table-wrap>
      <sec>
        <title>Sub Background</title>
        <p>Nested section body.</p>
      </sec>
    </sec>
    <sec>
      <title>Methods</title>
      <p>We measured flux with <inline-formula><mml:math><mml:mi>x</mml:mi></mml:math></inline-formula> assays.</p>
      <list list-type="bullet"><list-item><p>First item</p></list-item></list>
    </sec>
  </body>
  <back>
    <ref-list>
      <ref id="bib1"><element-citation><article-title>Old Reference</article-title></element-citation></ref>
    </ref-list>
    <fn-group><fn><p>Footnote noise</p></fn></fn-group>
  </back>
</article>
"""


def test_jats_to_markdown_structure():
    md = jats_to_markdown(SAMPLE_JATS_XML)
    assert "# Mechanisms of Cellular Respiration" in md
    assert "Marie Curie" in md and "Rosalind Franklin" in md
    assert "## Abstract" in md
    assert "oxidative phosphorylation" in md
    assert "## Introduction" in md
    assert "Cellular respiration is vital [1]." in md
    assert "[Figure 1] Diagram of electron transport chain." in md
    assert "| Complex | Rate |" in md
    assert "| Complex I | 12.5 |" in md
    assert "> Key insight callout." in md
    assert "- First item" in md


def test_jats_nested_section_depth():
    md = jats_to_markdown(SAMPLE_JATS_XML)
    assert "### Sub Background" in md  # nested one level below "## Introduction"


def test_jats_strips_noise():
    md = jats_to_markdown(SAMPLE_JATS_XML)
    assert "Old Reference" not in md      # <ref-list>
    assert "Footnote noise" not in md     # <fn-group>
    assert "Boilerplate licence text" not in md  # <permissions>


def test_jats_leaves_no_xml_tags():
    """Blockquote '>' is legal Markdown; assert on real XML tags, not bare angle brackets."""
    md = jats_to_markdown(SAMPLE_JATS_XML)
    assert XML_TAG_RE.search(md) is None
    assert "mml:" not in md


def test_jats_handles_malformed_xml():
    assert jats_to_markdown("<article><body><p>unclosed") is not None
    assert jats_to_markdown(b"") == ""


def test_list_and_select_sections():
    md = jats_to_markdown(SAMPLE_JATS_XML)
    sections = list_sections(md)
    assert "Abstract" in sections and "Introduction" in sections and "Methods" in sections

    only_methods = select_sections(md, ["methods"])
    assert "We measured flux" in only_methods
    assert "Cellular respiration is vital" not in only_methods

    # Unknown section names yield an empty selection rather than raising
    assert select_sections(md, ["Nonexistent"]).strip() == ""


def test_select_sections_preserves_nested_subsections():
    md = jats_to_markdown(SAMPLE_JATS_XML)
    intro_section = select_sections(md, ["Introduction"])
    assert "## Introduction" in intro_section
    assert "Cellular respiration is vital" in intro_section
    assert "### Sub Background" in intro_section
    assert "Nested section body." in intro_section
    assert "## Methods" not in intro_section
    assert "We measured flux" not in intro_section

