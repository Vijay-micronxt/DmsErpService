"""Shared limit/offset clamping for list_* API endpoints -- every one of them takes
the same pair and needs the same guard against a bad or abusive value (zero/negative
limit, a limit large enough to defeat the point of paginating, a negative offset), so
this is one place instead of five.
"""

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20


def clamp(limit: int = DEFAULT_PAGE_SIZE, offset: int = 0) -> tuple[int, int]:
	return min(max(int(limit), 1), MAX_PAGE_SIZE), max(int(offset), 0)
