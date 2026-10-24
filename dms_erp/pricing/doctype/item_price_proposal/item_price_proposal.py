from frappe.model.document import Document


class ItemPriceProposal(Document):
	def insurance_amount(self) -> float:
		"""BRD C.7.1: insurance is a % of the basic (purchase) cost, standardised per series."""
		return round((self.purchase_cost or 0) * (self.get("insurance_pct") or 0) / 100, 2)

	def landing_cost(self) -> float:
		"""BRD C.7.1: Basic rate + Freight + Loading + Insurance + Other landed costs
		(handling = unloading at Pacific's end, kept as its own line)."""
		return (
			(self.purchase_cost or 0)
			+ (self.freight or 0)
			+ (self.get("loading") or 0)
			+ self.insurance_amount()
			+ (self.handling or 0)
			+ (self.other_costs or 0)
		)

	def suggested_price(self) -> float:
		return round(self.landing_cost() * (1 + (self.margin_pct or 0) / 100))
