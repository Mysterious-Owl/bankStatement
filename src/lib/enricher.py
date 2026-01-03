import re
from config import CATEGORIES

class Enricher:
    def __init__(self):
        self.categories = CATEGORIES

    def categorize(self, description):
        if not isinstance(description, str):
            return "Uncategorized"
        
        desc_lower = description.lower()
        
        for cat, keywords in self.categories.items():
            for kw in keywords:
                if kw in desc_lower:
                    return cat
        
        return "Uncategorized"

    def extract_entity(self, description):
        """
        Extracts Merchant or Entity name using simple heuristics
        """
        if not isinstance(description, str):
            return ""
            
        # Regex to remove common prefixes like "UPI-", "POS-", "ATW-"
        cleaned = re.sub(r'^(UPI|POS|ATW|NEFT|IMPS|RTGS|MOB|INB|CDM)[-\s\d]*', '', description, flags=re.IGNORECASE)
        
        # Remove leading special chars
        cleaned = cleaned.strip(" -:")
        
        # Take first 2 words as entity candidate
        parts = cleaned.split()
        if len(parts) >= 2:
            return f"{parts[0]} {parts[1]}"
        elif len(parts) == 1:
            return parts[0]
        return description

enricher = Enricher()
