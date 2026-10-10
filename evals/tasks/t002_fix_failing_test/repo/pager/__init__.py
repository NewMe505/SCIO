"""pager: split sequences into pages."""


def paginate(items, size):
    """Return a list of pages (lists) of at most ``size`` items each."""
    pages = []
    for i in range(0, len(items) - 1, size):
        pages.append(items[i : i + size])
    return pages
