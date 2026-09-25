"""Shared QR-code image generation — a top-level module (not under warehouse/ or
catalog/) so both can import it without creating a cycle, same reasoning as
phone_utils.py. Originally lived only in warehouse.allocation_api (box/inward
stickers); catalog.sample_api's dealer sample sticker is the second real
caller, extracted rather than duplicated.
"""

import base64
from io import BytesIO


def qr_data_uri(payload: str) -> str:
	import qrcode

	img = qrcode.make(payload)
	buf = BytesIO()
	img.save(buf, format="PNG")
	return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
