from frappe.model.document import Document


class ItemPriceProposal(Document):
	def landing_cost(self) -> float:
		return (self.purchase_cost or 0) + (self.freight or 0) + (self.handling or 0) + (self.other_costs or 0)

	def suggested_price(self) -> float:
		return round(self.landing_cost() * (1 + (self.margin_pct or 0) / 100))
