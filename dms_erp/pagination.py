"""Shared limit/offset clamping for list_* API endpoints -- every one of them takes
the same pair and needs the same guard against a bad or abusive value (zero/negative
limit, a limit large enough to defeat the point of paginating, a negative offset), so
this is one place instead of five.
"""

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


def clamp(limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> tuple[int, int]:
	return min(max(int(limit), 1), MAX_PAGE_SIZE), max(int(offset), 0)


def date_range_filter(filters: dict, field: str, from_date: str | None, to_date: str | None) -> None:
	"""Adds `field BETWEEN from AND to` to a get_all filters dict; either end may be left out."""
	if from_date and to_date:
		filters[field] = ["between", [from_date, to_date]]
	elif from_date:
		filters[field] = [">=", from_date]
	elif to_date:
		filters[field] = ["<=", to_date]
