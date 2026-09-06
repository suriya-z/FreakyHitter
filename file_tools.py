import re
import os
import asyncio
from datetime import datetime
from typing import List, Dict, Tuple, Optional
from hitter_core import BINLookup

# Card Brand Detector
def detect_card_brand(card_num: str) -> str:
    if card_num.startswith(('300', '305', '36', '38')):
        return 'Diners Club'
    elif card_num.startswith('4'):
        return 'Visa'
    elif any(card_num.startswith(p) for p in ('51', '52', '53', '54', '55')) or (len(card_num) >= 4 and 2221 <= int(card_num[:4]) <= 2720):
        return 'Mastercard'
    elif card_num.startswith(('34', '37')):
        return 'Amex'
    elif card_num.startswith(('6011', '65', '644', '645')):
        return 'Discover'
    elif card_num.startswith(('3528', '3589')) or (len(card_num) >= 4 and 3528 <= int(card_num[:4]) <= 3589):
        return 'JCB'
    return 'Other'

BRAND_ORDER = {'Visa': 1, 'Mastercard': 2, 'Amex': 3, 'Discover': 4, 'JCB': 5, 'Diners Club': 6, 'Other': 7}

# Card Validator
def is_valid_card(card: str, month: str, year: str, cvv: str) -> bool:
    if not (13 <= len(card) <= 19 and card.isdigit()):
        return False
    if not month.isdigit() or not (1 <= int(month) <= 12):
        return False
    
    # Check expiry year (not in past)
    curr_year = datetime.now().year
    curr_month = datetime.now().month
    exp_year = int(year) + 2000 if len(year) == 2 else int(year)
    
    if exp_year < curr_year:
        return False
    if exp_year == curr_year and int(month) < curr_month:
        return False
        
    if not (3 <= len(cvv) <= 4 and cvv.isdigit()):
        return False
        
    return True

# 1. Clean & Sort
def clean_and_sort_cards_text(raw_text: str) -> Tuple[str, Dict]:
    lines = raw_text.split('\n')
    seen = set()
    cleaned = []
    
    brand_counts = {'Visa': 0, 'Mastercard': 0, 'Amex': 0, 'Discover': 0, 'JCB': 0, 'Diners Club': 0, 'Other': 0}
    invalid_count = 0
    duplicate_count = 0

    card_entries = []

    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue
            
        # Recipe 2 Fix: Support pipe, slash, colon, semi-colon, space delimiters
        matches = re.findall(r'(\d{13,19})[|/:\s;]+(\d{1,2})[|/:\s;]+(\d{2,4})[|/:\s;]+(\d{3,4})', line_str)
        if not matches:
            invalid_count += 1
            continue
            
        card, month, year, cvv = matches[0]
        month = month.zfill(2)
        year_str = year[-2:] if len(year) >= 2 else year.zfill(2)
        
        # Recipe 6 Fix: Standardize 2-digit year representation for accurate deduplication
        formatted = f"{card}|{month}|{year_str}|{cvv}"
        
        if not is_valid_card(card, month, year_str, cvv):
            invalid_count += 1
            continue
            
        if formatted in seen:
            duplicate_count += 1
            continue
            
        seen.add(formatted)
        brand = detect_card_brand(card)
        brand_counts[brand] = brand_counts.get(brand, 0) + 1
        card_entries.append((brand, formatted))

    # Sort in specified order: Visa first, Mastercard, Amex, Discover, etc.
    card_entries.sort(key=lambda x: (BRAND_ORDER.get(x[0], 99), x[1]))
    
    sorted_lines = [entry[1] for entry in card_entries]
    output_text = "\n".join(sorted_lines)
    
    stats = {
        'total_input': len(lines),
        'valid_total': len(sorted_lines),
        'invalid_count': invalid_count,
        'duplicate_count': duplicate_count,
        'brand_counts': brand_counts
    }
    return output_text, stats

# 2. Split (Lines per file)
def split_text_lines_per_file(text: str, lines_per_file: int) -> List[str]:
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    if not lines:
        return []
    lines_per_file = max(1, lines_per_file)
    
    parts = []
    for i in range(0, len(lines), lines_per_file):
        chunk = lines[i:i + lines_per_file]
        if chunk:
            parts.append("\n".join(chunk))
    return parts

def split_text_n_parts(text: str, n_parts: int) -> List[str]:
    return split_text_lines_per_file(text, n_parts)

# 3. Find BIN
def filter_by_bin_prefix(text: str, bin_prefix: str) -> Tuple[str, int]:
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    matched = []
    clean_bin = re.sub(r'\D', '', bin_prefix)
    
    for line in lines:
        # Recipe 1 Fix: Target card pattern first so order IDs / prefixes aren't matched instead
        card_matches = re.findall(r'(\d{13,19})', line)
        if card_matches:
            target_bin = card_matches[0]
            if target_bin.startswith(clean_bin):
                matched.append(line)
            
    return "\n".join(matched), len(matched)

# 4. Group by Country
async def group_text_by_country(text: str) -> Tuple[Dict[str, List[str]], Dict[str, Dict]]:
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    country_groups: Dict[str, List[str]] = {}
    country_meta: Dict[str, Dict] = {}

    unique_bins = set()
    line_bin_map = []
    
    for line in lines:
        matches = re.findall(r'(\d{6,19})', line)
        if matches:
            bin6 = matches[0][:6]
            unique_bins.add(bin6)
            line_bin_map.append((bin6, line))

    # Perform async BIN lookups concurrently (cached inside BINLookup)
    bin_results = {}
    for b6 in list(unique_bins)[:150]:
        info = await BINLookup.lookup(b6)
        bin_results[b6] = info

    for bin6, line in line_bin_map:
        info = bin_results.get(bin6, {})
        country_name = info.get('country_name') or info.get('country') or 'Unknown'
        flag = info.get('flag') or '🌐'
        code = info.get('code') or info.get('country') or 'UNK'
        
        if country_name not in country_groups:
            country_groups[country_name] = []
            country_meta[country_name] = {'flag': flag, 'code': code}
            
        country_groups[country_name].append(line)

    return country_groups, country_meta
