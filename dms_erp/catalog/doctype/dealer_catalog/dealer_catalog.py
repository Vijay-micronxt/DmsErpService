from frappe.model.document import Document


class DealerCatalog(Document):
	def visible_item_set(self) -> set[str]:
		return {row.item for row in self.items}
