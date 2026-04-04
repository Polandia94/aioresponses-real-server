from multidict import MultiDict
from yarl import URL

def normalize_url(url: "URL | str") -> URL:
    """Normalize url to make comparisons."""
    url = URL(url)
    if url.fragment:
        url = url.with_fragment(None)
    return url.with_query(sorted(url.query.items()))


def merge_params(url: "URL | str", params: "dict | None" = None) -> URL:
    url = URL(url)
    if params:
        query_params = MultiDict(url.query)
        query_params.extend(url.with_query(params).query)
        return url.with_query(query_params)
    return url
