"""
Test script to verify markdown to HTML conversion in query responses.
"""

from markdown_it import MarkdownIt


def test_markdown_conversion():
    """Test that markdown is converted to HTML correctly."""

    # Sample markdown text that an LLM might generate
    markdown_text = """Based on the provided sources, here are the key findings:

## Main Points

1. **First point**: This is an important finding from the research.
2. **Second point**: Another critical observation.
3. **Third point**: A final consideration.

The evidence suggests that:

- Item one with *emphasis*
- Item two with **strong emphasis**
- Item three with `code`

According to Source 1, the methodology involved several steps.

You can find more information [here](https://example.com)."""

    # Convert to HTML
    md = MarkdownIt()
    html_output = md.render(markdown_text)

    print("Original Markdown:")
    print("=" * 60)
    print(markdown_text)
    print("\n")

    print("Converted HTML:")
    print("=" * 60)
    print(html_output)
    print("\n")

    # Verify key elements are present
    assert "<h2>" in html_output, "H2 headers should be converted"
    assert "<strong>" in html_output, "Bold text should be converted"
    assert "<em>" in html_output, "Italic text should be converted"
    assert "<code>" in html_output, "Code should be converted"
    assert "<ol>" in html_output or "<li>" in html_output, "Lists should be converted"
    assert "<a href=" in html_output, "Links should be converted"

    print("[PASS] All markdown elements converted correctly!")
    return html_output


if __name__ == "__main__":
    test_markdown_conversion()
