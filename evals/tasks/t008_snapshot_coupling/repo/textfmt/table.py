def _pad(cell, width):
    return cell.ljust(width)


def render_table(rows, headers=None):
    """Render rows as a plain-text table: columns joined by ' | ', padded to equal width."""
    data = [[str(c) for c in row] for row in rows]
    if headers:
        data = [[str(h) for h in headers]] + data
    if not data:
        return ""
    widths = [max(len(row[i]) for row in data) for i in range(len(data[0]))]
    lines = []
    for row in data:
        lines.append(" | ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)
