from fastapi_lens.utils.patterns import RouteFilter


def test_route_filter_applies_excludes_before_includes() -> None:
    route_filter = RouteFilter(
        include=("/api/*",),
        exclude=("/api/private/*",),
    )

    assert route_filter.allows_raw_path("/api/items") is True
    assert route_filter.allows_raw_path("/api/private/items") is False


def test_star_matches_one_or_more_path_characters() -> None:
    route_filter = RouteFilter(include=("/api/*",))

    assert route_filter.allows_route("/api/items", "/api/items") is True
    assert route_filter.allows_route("/api/", "/api/") is False


def test_route_parameter_patterns_make_raw_prefilter_conservative() -> None:
    route_filter = RouteFilter(include=("/items/{item_id}",))

    assert route_filter.allows_raw_path("/items/42") is True
    assert route_filter.allows_route("/items/{item_id}", "/items/42") is True
    assert route_filter.allows_route(None, "/items/42") is False


def test_empty_include_list_disables_route_capture() -> None:
    route_filter = RouteFilter(include=())

    assert route_filter.allows_raw_path("/items") is False
    assert route_filter.allows_route("/items", "/items") is False
