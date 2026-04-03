import re
import polars as pl

#  PRE-COMPILE REGEX FOR MASSIVE SPEED INCREASE
REGEX_4_RANGE = re.compile(r'(\d{4})\s*-\s*(\d{4})')
REGEX_2_RANGE = re.compile(r'\b(\d{2})\s*-\s*(\d{2})\b')
REGEX_SINGLE = re.compile(r'\b(\d{4})\b')

def _extract_year_python(title: str) -> int:
    """Internal fast-exit regex logic."""
    if not title: 
        return 9999
    
    # 1. Match YYYY-YYYY
    match_4 = REGEX_4_RANGE.search(title)
    if match_4:
        return max(int(match_4.group(1)), int(match_4.group(2)))
        
    # 2. Match YY-YY
    match_2 = REGEX_2_RANGE.search(title)
    if match_2:
        y1, y2 = int(match_2.group(1)), int(match_2.group(2))
        return max(1900 + y1 if y1 > 50 else 2000 + y1, 
                   1900 + y2 if y2 > 50 else 2000 + y2)
        
    # 3. Match single YYYY
    match_single = REGEX_SINGLE.search(title)
    if match_single:
        return int(match_single.group(1))
        
    return 9999

def get_fitment_expr() -> pl.Expr:
    """
    Returns the mapped expression to be injected seamlessly 
    into the main with_columns block.
    """
    return pl.col("Title").map_elements(_extract_year_python, return_dtype=pl.Int32)