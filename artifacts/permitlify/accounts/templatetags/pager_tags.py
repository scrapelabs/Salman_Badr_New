from django import template

register = template.Library()


@register.filter
def elided_pages(page_obj):
    """Windowed page numbers (with ``Paginator.ELLIPSIS`` gaps) for a compact,
    numbered pager. Falls back to the full range if the helper is unavailable."""
    paginator = page_obj.paginator
    try:
        return paginator.get_elided_page_range(
            page_obj.number, on_each_side=1, on_ends=1
        )
    except Exception:
        return paginator.page_range
