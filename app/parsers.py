def parse_unstract_json(data: dict) -> dict:
    # Case 1: Unstract full API response
    if "message" in data and "result" in data["message"]:
        if data["message"]["result"]:
            data = data["message"]["result"][0].get("result", {}).get("output", {})
    
    # Case 2: Array response
    if isinstance(data, list) and data:
        data = data[0]

    # Flatten line items if wrapped
    if "items_table" in data:
        if isinstance(data["items_table"], dict):
            if "line_items" in data["items_table"]:
                data["line_items"] = data["items_table"]["line_items"]
        elif isinstance(data["items_table"], list):
            data["line_items"] = data["items_table"]
        del data["items_table"]
    
    # Ensure numeric values
    def parse_number(val):
        try:
            return float(val) if val is not None else 0.0
        except ValueError:
            return 0.0

    # Clean GSTIN fields
    for field in ["buyer_gstin", "vendor_gstin"]:
        if data.get(field) and isinstance(data[field], str):
            data[field] = data[field].replace("GSTIN:", "").replace("GSTIN", "").strip()

    data["subtotal"] = parse_number(data.get("subtotal"))
    data["cgst"] = parse_number(data.get("cgst"))
    data["sgst"] = parse_number(data.get("sgst"))
    data["igst"] = parse_number(data.get("igst"))
    data["tax_total"] = parse_number(data.get("tax_total"))
    data["total_amount"] = parse_number(data.get("total_amount"))

    # Normalize line items
    raw_line_items = data.get("line_items") or []
    line_items = []
    for item in raw_line_items:
        line_items.append({
            "item_number": float(item.get("item_number", 0)) if item.get("item_number") else 0.0,
            "description": item.get("description", ""),
            "hsn_sac": str(item.get("hsn_sac", "")),
            "quantity": float(item.get("quantity", 0)) if item.get("quantity") else 0.0,
            "unit": item.get("unit", ""),
            "unit_price": float(item.get("unit_price", 0)) if item.get("unit_price") else 0.0,
            "amount": float(item.get("amount", 0)) if item.get("amount") else 0.0,
        })
    data["line_items"] = line_items

    return data
