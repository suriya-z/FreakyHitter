import random, re

# ==================== BIN CARD GENERATOR ====================
def generate_bin_cards(bin_pattern: str, count: int = 10) -> list:
    """
    Generates Luhn-valid cards matching bin_pattern.
    Supports formats:
    - 453590
    - 453590xxxxxxxxxx
    - 453590xxxxxxxxxx|mm|yy|cvc
    - 453590|05|28|xxx
    """
    parts = bin_pattern.split('|')
    card_pat = parts[0].strip()
    mm_pat = parts[1].strip() if len(parts) > 1 else None
    yy_pat = parts[2].strip() if len(parts) > 2 else None
    cvc_pat = parts[3].strip() if len(parts) > 3 else None

    prefix = re.sub(r'[^0-9xX]', '', card_pat)
    cards = []
    
    for _ in range(count):
        full_prefix = ''
        for char in prefix:
            if char.lower() == 'x':
                full_prefix += str(random.randint(0, 9))
            else:
                full_prefix += char
        
        while len(full_prefix) < 15:
            full_prefix += str(random.randint(0, 9))
            
        full_prefix = full_prefix[:15]
        
        checksum = 0
        for idx, digit_char in enumerate(reversed(full_prefix)):
            d = int(digit_char)
            if idx % 2 == 0:
                d *= 2
                if d > 9:
                    d -= 9
            checksum += d
            
        check_digit = (10 - (checksum % 10)) % 10
        card_number = full_prefix + str(check_digit)
        
        if mm_pat and mm_pat.isdigit():
            month = f"{int(mm_pat):02d}"
        else:
            month = f"{random.randint(1, 12):02d}"
            
        if yy_pat and yy_pat.isdigit():
            year = yy_pat if len(yy_pat) == 2 else yy_pat[-2:]
        else:
            year = f"{random.randint(25, 30):02d}"
            
        if cvc_pat and cvc_pat.isdigit():
            cvc = cvc_pat
        else:
            cvc = f"{random.randint(100, 999):03d}"
            
        cards.append(f"{card_number}|{month}|20{year}|{cvc}")
        
    return cards


# ==================== MOD-97 IBAN ENGINE ====================
IBAN_COUNTRIES = {
    # Europe (44)
    "DE": {"name": "GERMANY", "flag": "🇩🇪", "len": 22, "bban_fmt": "8n10n", "bank": "Landesbank Berlin / Berliner Sparkasse", "blz": "10050000", "bic": "BELADEBEXXX", "region": "Europe"},
    "FR": {"name": "FRANCE", "flag": "🇫🇷", "len": 27, "bban_fmt": "5n5n11c2n", "bank": "BNP Paribas", "blz": "30004", "bic": "BNPAFRPPXXX", "region": "Europe"},
    "GB": {"name": "UNITED KINGDOM", "flag": "🇬🇧", "len": 22, "bban_fmt": "4a6n8n", "bank": "Barclays Bank UK PLC", "blz": "200000", "bic": "BARCGB22XXX", "region": "Europe"},
    "IT": {"name": "ITALY", "flag": "🇮🇹", "len": 27, "bban_fmt": "1a5n5n12c", "bank": "Intesa Sanpaolo S.p.A.", "blz": "03069", "bic": "BCITITMMXXX", "region": "Europe"},
    "ES": {"name": "SPAIN", "flag": "🇪🇸", "len": 24, "bban_fmt": "4n4n2n10n", "bank": "Banco Santander S.A.", "blz": "0049", "bic": "BSCHESMMXXX", "region": "Europe"},
    "NL": {"name": "NETHERLANDS", "flag": "🇳🇱", "len": 18, "bban_fmt": "4a10n", "bank": "ING Bank N.V.", "blz": "INGB", "bic": "INGBNL2AXXX", "region": "Europe"},
    "AT": {"name": "AUSTRIA", "flag": "🇦🇹", "len": 20, "bban_fmt": "5n11n", "bank": "Erste Group Bank AG", "blz": "20111", "bic": "GIBAATWWXXX", "region": "Europe"},
    "BE": {"name": "BELGIUM", "flag": "🇧🇪", "len": 16, "bban_fmt": "3n7n2n", "bank": "KBC Bank NV", "blz": "435", "bic": "KREDBEBBXXX", "region": "Europe"},
    "CH": {"name": "SWITZERLAND", "flag": "🇨🇭", "len": 21, "bban_fmt": "5n12c", "bank": "UBS Switzerland AG", "blz": "00230", "bic": "UBSWCHZH80A", "region": "Europe"},
    "SE": {"name": "SWEDEN", "flag": "🇸🇪", "len": 24, "bban_fmt": "3n16n1n", "bank": "Swedbank AB", "blz": "8327", "bic": "SWEDSESSXXX", "region": "Europe"},
    "NO": {"name": "NORWAY", "flag": "🇳🇴", "len": 15, "bban_fmt": "4n6n1n", "bank": "DNB Bank ASA", "blz": "1200", "bic": "DNBNNOKKXXX", "region": "Europe"},
    "DK": {"name": "DENMARK", "flag": "🇩🇰", "len": 18, "bban_fmt": "4n9n1n", "bank": "Danske Bank A/S", "blz": "3000", "bic": "DABADKKKXXX", "region": "Europe"},
    "FI": {"name": "FINLAND", "flag": "🇫🇮", "len": 18, "bban_fmt": "6n7n1n", "bank": "Nordea Bank Abp", "blz": "123456", "bic": "NDEAFHHHXXX", "region": "Europe"},
    "PT": {"name": "PORTUGAL", "flag": "🇵🇹", "len": 25, "bban_fmt": "4n4n11n2n", "bank": "Caixa Geral de Depositos", "blz": "0035", "bic": "CGDIPTPLXXX", "region": "Europe"},
    "IE": {"name": "IRELAND", "flag": "🇮🇪", "len": 22, "bban_fmt": "4a6n8n", "bank": "Bank of Ireland", "blz": "900017", "bic": "BOFIIE2DXXX", "region": "Europe"},
    "GR": {"name": "GREECE", "flag": "🇬🇷", "len": 27, "bban_fmt": "3n4n16c", "bank": "National Bank of Greece", "blz": "011", "bic": "ETHNGRAAXXX", "region": "Europe"},
    "PL": {"name": "POLAND", "flag": "🇵🇱", "len": 28, "bban_fmt": "8n16n", "bank": "PKO Bank Polski", "blz": "10200000", "bic": "BPKOPLPWXXX", "region": "Europe"},
    "CZ": {"name": "CZECHIA", "flag": "🇨🇿", "len": 24, "bban_fmt": "4n6n10n", "bank": "Ceska Sporitelna a.s.", "blz": "0800", "bic": "GIBACO22XXX", "region": "Europe"},
    "HU": {"name": "HUNGARY", "flag": "🇭🇺", "len": 28, "bban_fmt": "3n4n1n15n1n", "bank": "OTP Bank Nyrt.", "blz": "117", "bic": "OTPVHUHBXXX", "region": "Europe"},
    "RO": {"name": "ROMANIA", "flag": "🇷🇴", "len": 24, "bban_fmt": "4a16c", "bank": "Banca Transilvania", "blz": "BTRL", "bic": "BTRLRO22XXX", "region": "Europe"},
    "BG": {"name": "BULGARIA", "flag": "🇧🇬", "len": 22, "bban_fmt": "4a4n2n8c", "bank": "UniCredit Bulbank", "blz": "UNCR", "bic": "UNCRBGSFXXX", "region": "Europe"},
    "SK": {"name": "SLOVAKIA", "flag": "🇸🇰", "len": 24, "bban_fmt": "4n6n10n", "bank": "Slovenska Sporitelna a.s.", "blz": "0900", "bic": "GIBASKBXXXX", "region": "Europe"},
    "HR": {"name": "CROATIA", "flag": "🇭🇷", "len": 21, "bban_fmt": "7n10n", "bank": "Zagrebacka Banka d.d.", "blz": "2360000", "bic": "ZABAHR2XXXX", "region": "Europe"},
    "SI": {"name": "SLOVENIA", "flag": "🇸🇮", "len": 19, "bban_fmt": "5n8n2n", "bank": "NLB d.d. Ljubljana", "blz": "02010", "bic": "LJBASI2XXXX", "region": "Europe"},
    "EE": {"name": "ESTONIA", "flag": "🇪🇪", "len": 20, "bban_fmt": "2n2n11n1n", "bank": "Swedbank AS", "blz": "22", "bic": "HABAEE2XXXX", "region": "Europe"},
    "LV": {"name": "LATVIA", "flag": "🇱🇻", "len": 21, "bban_fmt": "4a13c", "bank": "SEB banka AS", "blz": "UNLA", "bic": "UNLALV2XXXX", "region": "Europe"},
    "LT": {"name": "LITHUANIA", "flag": "🇱🇹", "len": 20, "bban_fmt": "5n11n", "bank": "SEB bankas AB", "blz": "70440", "bic": "CBVILT2XXXX", "region": "Europe"},
    "LU": {"name": "LUXEMBOURG", "flag": "🇱🇺", "len": 20, "bban_fmt": "3n13c", "bank": "BGL BNP Paribas", "blz": "001", "bic": "BGLULLLUXXX", "region": "Europe"},
    "CY": {"name": "CYPRUS", "flag": "🇨🇾", "len": 28, "bban_fmt": "3n5n16c", "bank": "Bank of Cyprus Public Company", "blz": "002", "bic": "BCYPCY21XXX", "region": "Europe"},
    "MT": {"name": "MALTA", "flag": "🇲🇹", "len": 31, "bban_fmt": "4a5n18c", "bank": "Bank of Valletta p.l.c.", "blz": "BOVM", "bic": "BOVMMTMTXXX", "region": "Europe"},
    "IS": {"name": "ICELAND", "flag": "🇮🇸", "len": 26, "bban_fmt": "4n2n6n10n", "bank": "Landsbankinn hf.", "blz": "0154", "bic": "LAISISREXXX", "region": "Europe"},
    "AL": {"name": "ALBANIA", "flag": "🇦🇱", "len": 28, "bban_fmt": "8n16c", "bank": "Banka Kombetare Tregtare", "blz": "201", "bic": "BKTALTIRXXX", "region": "Europe"},
    "AD": {"name": "ANDORRA", "flag": "🇦🇩", "len": 24, "bban_fmt": "4n4n12c", "bank": "Mora Banc Grup SA", "blz": "0008", "bic": "MORAAD22XXX", "region": "Europe"},
    "BA": {"name": "BOSNIA", "flag": "🇧🇦", "len": 20, "bban_fmt": "3n3n8n2n", "bank": "Raiffeisen Bank d.d.", "blz": "161", "bic": "RZBABA2SXXX", "region": "Europe"},
    "FO": {"name": "FAROE ISLANDS", "flag": "🇫🇴", "len": 18, "bban_fmt": "4n9n1n", "bank": "Betri Banki P/F", "blz": "9181", "bic": "BETRFO22XXX", "region": "Europe"},
    "GI": {"name": "GIBRALTAR", "flag": "🇬🇮", "len": 23, "bban_fmt": "4a15c", "bank": "Gibraltar International Bank", "blz": "GIBK", "bic": "GIBKGIGXXXX", "region": "Europe"},
    "GL": {"name": "GREENLAND", "flag": "🇬🇱", "len": 18, "bban_fmt": "4n9n1n", "bank": "BankNordik A/S", "blz": "6471", "bic": "FAROGL22XXX", "region": "Europe"},
    "LI": {"name": "LIECHTENSTEIN", "flag": "🇱🇮", "len": 21, "bban_fmt": "5n12c", "bank": "LGT Bank AG", "blz": "08801", "bic": "LGTBLI22XXX", "region": "Europe"},
    "MD": {"name": "MOLDOVA", "flag": "🇲🇩", "len": 24, "bban_fmt": "2c18c", "bank": "MAIB SA", "blz": "AG", "bic": "AGRNMD2XXXX", "region": "Europe"},
    "MC": {"name": "MONACO", "flag": "🇲🇨", "len": 27, "bban_fmt": "5n5n11c2n", "bank": "CFM Indosuez Wealth", "blz": "10057", "bic": "CFMMMCMMXXX", "region": "Europe"},
    "ME": {"name": "MONTENEGRO", "flag": "🇲🇪", "len": 22, "bban_fmt": "3n13n2n", "bank": "CKB Banka AD", "blz": "510", "bic": "CKBAMEM2XXX", "region": "Europe"},
    "MK": {"name": "NORTH MACEDONIA", "flag": "🇲🇰", "len": 19, "bban_fmt": "3n10c2n", "bank": "Komercijalna Banka AD", "blz": "300", "bic": "KOBAMK2XXXX", "region": "Europe"},
    "SM": {"name": "SAN MARINO", "flag": "🇸🇲", "len": 27, "bban_fmt": "1a5n5n12c", "bank": "Banca di San Marino", "blz": "08530", "bic": "BSMMSM2SXXX", "region": "Europe"},
    "RS": {"name": "SERBIA", "flag": "🇷🇸", "len": 22, "bban_fmt": "3n13n2n", "bank": "Banca Intesa a.d.", "blz": "160", "bic": "DBSSRSBGXXX", "region": "Europe"},

    # Middle East & Africa (8)
    "AE": {"name": "UAE", "flag": "🇦🇪", "len": 23, "bban_fmt": "3n16n", "bank": "Emirates NBD Bank PJSC", "blz": "033", "bic": "EBBDAEADXXX", "region": "Middle East & Africa"},
    "BH": {"name": "BAHRAIN", "flag": "🇧🇭", "len": 22, "bban_fmt": "4a14c", "bank": "National Bank of Bahrain", "blz": "NBBH", "bic": "NBBHBHBMXXX", "region": "Middle East & Africa"},
    "IL": {"name": "ISRAEL", "flag": "🇮🇱", "len": 23, "bban_fmt": "3n3n13n", "bank": "Bank Leumi le-Israel B.M.", "blz": "010", "bic": "LUMIILLVXXX", "region": "Middle East & Africa"},
    "KW": {"name": "KUWAIT", "flag": "🇰🇼", "len": 30, "bban_fmt": "4a22c", "bank": "National Bank of Kuwait S.A.K.", "blz": "NBOK", "bic": "NBOKKWKWXXX", "region": "Middle East & Africa"},
    "LB": {"name": "LEBANON", "flag": "🇱🇧", "len": 28, "bban_fmt": "4n20c", "bank": "BLOM Bank S.A.L.", "blz": "0014", "bic": "BLOMBEYBXXX", "region": "Middle East & Africa"},
    "QA": {"name": "QATAR", "flag": "🇶🇦", "len": 29, "bban_fmt": "4a21c", "bank": "Qatar National Bank Q.P.S.C.", "blz": "QNBA", "bic": "QNBAQAQAXXX", "region": "Middle East & Africa"},
    "SA": {"name": "SAUDI ARABIA", "flag": "🇸🇦", "len": 24, "bban_fmt": "2n18c", "bank": "Al Rajhi Banking & Investment Corp", "blz": "80", "bic": "RJBISARIXXX", "region": "Middle East & Africa"},
    "TN": {"name": "TUNISIA", "flag": "🇹🇳", "len": 24, "bban_fmt": "2n2n16n2n", "bank": "Banque de Tunisie", "blz": "05", "bic": "BTUNNTTTXXX", "region": "Middle East & Africa"},

    # Asia & Other (4)
    "PK": {"name": "PAKISTAN", "flag": "🇵🇰", "len": 24, "bban_fmt": "4a16c", "bank": "Habib Bank Limited", "blz": "HABB", "bic": "HABBPKKAXXX", "region": "Asia & Other"},
    "GE": {"name": "GEORGIA", "flag": "🇬🇪", "len": 22, "bban_fmt": "2a16n", "bank": "Bank of Georgia JSC", "blz": "BG", "bic": "BAGAGE22XXX", "region": "Asia & Other"},
    "AZ": {"name": "AZERBAIJAN", "flag": "🇦🇿", "len": 28, "bban_fmt": "4a20c", "bank": "International Bank of Azerbaijan", "blz": "IBAZ", "bic": "IBAZAZ2XXXX", "region": "Asia & Other"},
    "KZ": {"name": "KAZAKHSTAN", "flag": "🇰🇿", "len": 20, "bban_fmt": "3n13c", "bank": "Halyk Savings Bank", "blz": "601", "bic": "HSBKKZKXXXX", "region": "Asia & Other"},
}

def calculate_mod97_iban(country_code, bban):
    """Calculates valid MOD-97 check digits for IBAN"""
    temp = bban.upper() + country_code.upper() + "00"
    numeric_str = "".join(str(ord(c) - 55) if c.isalpha() else c for c in temp)
    remainder = int(numeric_str) % 97
    check_digits = 98 - remainder
    return f"{check_digits:02d}"

def generate_valid_iban(country_code="DE"):
    cc = country_code.upper().strip()
    if cc not in IBAN_COUNTRIES:
        cc = "DE"
    
    info = IBAN_COUNTRIES[cc]
    bban_len = info["len"] - 4
    
    if cc == "DE":
        blz = info["blz"]
        acc = "".join(str(random.randint(0, 9)) for _ in range(10))
        bban = blz + acc
    elif cc == "FR":
        bban = f"{info['blz']}{random.randint(10000, 99999):05d}{''.join(str(random.randint(0,9)) for _ in range(11))}76"
    elif cc == "GB":
        bban = f"BARC{info['blz']}{random.randint(10000000, 99999999)}"
    elif cc == "ES":
        bban = f"{info['blz']}{random.randint(1000,9999):04d}00{''.join(str(random.randint(0,9)) for _ in range(10))}"
    elif cc == "IT":
        bban = f"X{info['blz']}{random.randint(10000,99999):05d}{''.join(str(random.randint(0,9)) for _ in range(12))}"
    elif cc == "AE":
        bban = f"{info['blz']}{''.join(str(random.randint(0,9)) for _ in range(16))}"
    else:
        blz = info.get("blz", "1000")
        rem = bban_len - len(blz)
        if rem < 0:
            bban = "".join(str(random.randint(0, 9)) for _ in range(bban_len))
        else:
            bban = blz + "".join(str(random.randint(0, 9)) for _ in range(rem))
            
    check_digits = calculate_mod97_iban(cc, bban)
    full_iban = f"{cc}{check_digits}{bban}"
    formatted_iban = " ".join(full_iban[i:i+4] for i in range(0, len(full_iban), 4))
    account_no = bban[-10:] if len(bban) >= 10 else bban
    
    return {
        "iban": formatted_iban,
        "raw_iban": full_iban,
        "country": info["name"],
        "flag": info["flag"],
        "bank_name": info["bank"],
        "bank_code": info["blz"],
        "bic": info["bic"],
        "account_no": account_no
    }

# ==================== FAKE IDENTITY GENERATOR ====================
FAKER_DATA = {
    "India": {
        "flag": "🇮🇳", "code": "+91",
        "first_male": ["Aditya", "Rohan", "Vikram", "Rajesh", "Arjun", "Dev", "Karan", "Siddharth", "Aarav", "Kabir"],
        "first_female": ["Adhira", "Ananya", "Priya", "Sneha", "Kavya", "Pooja", "Riya", "Diya", "Isha", "Tara"],
        "last": ["Sullad", "Sharma", "Verma", "Patel", "Gupta", "Rao", "Nair", "Singh", "Kumar", "Deshmukh"],
        "cities": [("Gangtok", "Jharkhand", "14932"), ("Mumbai", "Maharashtra", "400001"), ("Delhi", "Delhi", "110001"), ("Bangalore", "Karnataka", "560001"), ("Jaipur", "Rajasthan", "302001")],
        "streets": ["1907 Ashoka Rd", "42 MG Road", "108 Park Street", "15 GT Road", "88 Ring Road"],
        "id_name": "Aadhaar",
        "id_fmt": lambda: f"{random.randint(1000,9999)}-{random.randint(1000,9999)}-{random.randint(1000,9999)}"
    },
    "United States": {
        "flag": "🇺🇸", "code": "+1",
        "first_male": ["James", "John", "Robert", "Michael", "William", "David", "Richard", "Joseph", "Thomas", "Charles"],
        "first_female": ["Mary", "Patricia", "Jennifer", "Linda", "Elizabeth", "Barbara", "Susan", "Jessica", "Sarah", "Karen"],
        "last": ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez"],
        "cities": [("New York", "NY", "10001"), ("Los Angeles", "CA", "90001"), ("Chicago", "IL", "60601"), ("Houston", "TX", "77001"), ("Miami", "FL", "33101")],
        "streets": ["742 Evergreen Terrace", "1060 West Addison St", "1600 Pennsylvania Ave", "221B Baker St", "5th Avenue"],
        "id_name": "SSN",
        "id_fmt": lambda: f"{random.randint(100,999)}-{random.randint(10,99)}-{random.randint(1000,9999)}"
    },
    "Germany": {
        "flag": "🇩🇪", "code": "+49",
        "first_male": ["Lukas", "Maximilian", "Paul", "Felix", "Jonas", "Leon", "Finn", "Noah", "Elias", "Ben"],
        "first_female": ["Emma", "Mia", "Hannah", "Sophia", "Anna", "Lea", "Emilia", "Marie", "Lena", "Luisa"],
        "last": ["Müller", "Schmidt", "Schneider", "Fischer", "Weber", "Meyer", "Wagner", "Becker", "Schulz", "Hoffmann"],
        "cities": [("Berlin", "Berlin", "10115"), ("Munich", "Bavaria", "80331"), ("Hamburg", "Hamburg", "20095"), ("Frankfurt", "Hesse", "60311")],
        "streets": ["Hauptstraße 12", "Bahnhofstraße 45", "Schillerstraße 8", "Goethestraße 23", "Berliner Straße 101"],
        "id_name": "Steuer-ID",
        "id_fmt": lambda: f"{random.randint(10000000000, 99999999999)}"
    },
    "United Kingdom": {
        "flag": "🇬🇧", "code": "+44",
        "first_male": ["Oliver", "George", "Harry", "Noah", "Jack", "Leo", "Arthur", "Muhammad", "Oscar", "Charlie"],
        "first_female": ["Olivia", "Amelia", "Isla", "Ava", "Mia", "Ivy", "Lily", "Isabella", "Sophia", "Grace"],
        "last": ["Smith", "Jones", "Taylor", "Brown", "Williams", "Wilson", "Johnson", "Davies", "Robinson", "Wright"],
        "cities": [("London", "Greater London", "EC1A 1BB"), ("Manchester", "Greater Manchester", "M1 1AE"), ("Birmingham", "West Midlands", "B1 1AA")],
        "streets": ["10 Downing Street", "221B Baker Street", "4 Privet Drive", "High Street 14", "Church Road 8"],
        "id_name": "NIN",
        "id_fmt": lambda: f"QQ{random.randint(10,99)}{random.randint(10,99)}{random.randint(10,99)}A"
    }
}

def generate_fake_identity(country_query="United States"):
    country_match = "United States"
    for name in FAKER_DATA.keys():
        if country_query.lower() in name.lower() or name.lower() in country_query.lower():
            country_match = name
            break
            
    info = FAKER_DATA[country_match]
    gender = random.choice(["Male", "Female"])
    first_name = random.choice(info["first_male"]) if gender == "Male" else random.choice(info["first_female"])
    last_name = random.choice(info["last"])
    
    birth_year = random.randint(1965, 2004)
    birth_month = random.randint(1, 12)
    birth_day = random.randint(1, 28)
    age = 2026 - birth_year
    bday_str = f"{birth_year}-{birth_month:02d}-{birth_day:02d} ({age}y)"
    
    city, state, zip_code = random.choice(info["cities"])
    street = random.choice(info["streets"])
    phone = f"{info['code']} {random.randint(70000, 99999)} {random.randint(10000, 99999)}"
    id_num = info["id_fmt"]()
    email_user = f"{first_name.lower()}{last_name.lower()}{random.randint(10,99)}"
    email_domain = random.choice(["gmail.com", "yahoo.com", "outlook.com", "icloud.com"])
    
    return {
        "name": f"{first_name} {last_name}",
        "gender": gender,
        "birthday": bday_str,
        "street": street,
        "city": city,
        "state": state,
        "zip": zip_code,
        "address": f"{street}, {city}, {state} {zip_code}",
        "country": country_match,
        "flag": info["flag"],
        "phone": phone,
        "email": f"{email_user}@{email_domain}",
        "id_name": info["id_name"],
        "id_val": id_num
    }
