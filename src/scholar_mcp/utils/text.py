def truncate_content(content: str, max_chars: int) -> tuple[str, bool]:
    """Truncate at a paragraph boundary when one is close to the cutoff."""
    if len(content) <= max_chars:
        return content, False

    cutoff = max_chars
    # Try finding paragraph boundary before cutoff
    para_break = content.rfind("\n\n", 0, cutoff)
    if para_break > int(cutoff * 0.7):
        truncated_text = content[:para_break].rstrip()
    else:
        truncated_text = content[:cutoff].rstrip()

    marker = "\n\n[... Truncated due to max_chars limit ...]"
    return truncated_text + marker, True
